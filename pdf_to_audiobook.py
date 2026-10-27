#!/usr/bin/env python3
"""
pdf_to_audiobook.py - converte un PDF con testo selezionabile in audiolibro MP3.

Usa le voci neurali di Microsoft Edge tramite la libreria edge-tts (gratuita,
serve la connessione a internet): sono le piu' naturali tra le opzioni
gratuite.

Esempi:
    python pdf_to_audiobook.py --list-voices
    python pdf_to_audiobook.py libro.pdf --sample            # provini di tutte le voci italiane
    python pdf_to_audiobook.py libro.pdf --dry-run           # salva solo il testo che verrebbe letto
    python pdf_to_audiobook.py libro.pdf                     # un unico MP3
    python pdf_to_audiobook.py libro.pdf --mode chapters     # un MP3 per capitolo (usa i segnalibri del PDF)
    python pdf_to_audiobook.py libro.pdf --mode pages --pages-per-file 25
    python pdf_to_audiobook.py libro.pdf --pages 9-310 --voice it-IT-ElsaNeural --rate -5%
    python pdf_to_audiobook.py libro.pdf --pause-paragraph 800 --pause-heading 1500 --keep-chunks

Se si interrompe (rete, chiusura del programma) basta rilanciare lo stesso
comando: riparte dai pezzi audio gia' generati.
"""

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_VOICE = "it-IT-IsabellaNeural"
MAX_CHUNK_CHARS = 3000
SPEECH_CHARS_PER_SECOND = 15  # stima grossolana per l'italiano a velocita' normale

SENT_END = (".", "!", "?", "…", ":", ";", '"', "”", "»", ")")
DIALOGUE_START = ("—", "–", "«", "“", "- ")  # una battuta di dialogo apre un nuovo paragrafo
class JobError(Exception):
    """Problema con i dati inseriti o con il PDF: il messaggio e' pensato per l'utente."""


class ConversionCancelled(Exception):
    """L'utente ha interrotto la conversione."""


TRANSLATE = str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "\u00ad": None})


# ---------------------------------------------------------------------------
# Lettura del PDF
# ---------------------------------------------------------------------------
class PdfData:
    def __init__(self, units, grouped, toc):
        self.units = units      # per ogni pagina: lista di "unita'" di testo
        self.grouped = grouped  # True = le unita' sono gia' paragrafi (PyMuPDF); False = sono righe (pypdf)
        self.toc = toc          # lista di (titolo, pagina_1based) di primo livello


def _load_with_pymupdf(path):
    try:
        import fitz  # PyMuPDF
    except ImportError:
        try:
            import pymupdf as fitz
        except ImportError:
            return None

    doc = fitz.open(str(path))
    units = []
    for page in doc:
        blocks = page.get_text("blocks")  # (x0, y0, x1, y1, testo, n_blocco, tipo)
        units.append([b[4] for b in blocks if b[6] == 0 and b[4].strip()])

    toc_raw = [(lvl, title, pg) for lvl, title, pg in doc.get_toc() if pg >= 1]
    toc = []
    if toc_raw:
        top = min(l for l, _, _ in toc_raw)
        toc = [(t.strip(), p) for l, t, p in toc_raw if l == top]
    return PdfData(units, grouped=True, toc=toc)


def _load_with_pypdf(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        return None

    reader = PdfReader(str(path))
    units = []
    for page in reader.pages:
        text = page.extract_text() or ""
        units.append([ln for ln in text.splitlines() if ln.strip()])

    toc_raw = []

    def walk(items, level):
        for it in items:
            if isinstance(it, list):
                walk(it, level + 1)
            else:
                try:
                    pg = reader.get_destination_page_number(it) + 1
                except Exception:
                    continue
                if pg >= 1:
                    toc_raw.append((level, str(it.title), pg))

    try:
        walk(reader.outline, 1)
    except Exception:
        toc_raw = []

    toc = []
    if toc_raw:
        top = min(l for l, _, _ in toc_raw)
        toc = [(t.strip(), p) for l, t, p in toc_raw if l == top]
    return PdfData(units, grouped=False, toc=toc)


def load_pdf(path, log=print):
    data = _load_with_pymupdf(path)
    if data is None:
        log("(PyMuPDF non installato: uso pypdf. Per un'estrazione migliore: pip install pymupdf)")
        data = _load_with_pypdf(path)
    if data is None:
        raise JobError("serve PyMuPDF oppure pypdf. Installa con: pip install pymupdf")
    return data


# ---------------------------------------------------------------------------
# Pulizia del testo
# ---------------------------------------------------------------------------
def _norm_for_repeat(s):
    return re.sub(r"\d+", "#", s.strip().lower())


def strip_repeated(units_per_page, threshold=0.4):
    """Toglie intestazioni, pie' di pagina e numeri di pagina: le righe/blocchi
    in cima o in fondo che si ripetono su una buona parte delle pagine."""
    n = len(units_per_page)
    repeated = set()
    if n >= 6:
        counter = Counter()
        for units in units_per_page:
            cands = set()
            for u in units[:2] + units[-2:]:
                if len(u) < 100 and u.strip():
                    cands.add(_norm_for_repeat(u))
            counter.update(cands)
        repeated = {k for k, v in counter.items() if v / n >= threshold}

    cleaned = []
    for units in units_per_page:
        keep = []
        last = len(units) - 1
        for i, u in enumerate(units):
            at_edge = i < 2 or i > last - 2
            if at_edge and len(u) < 100:
                if _norm_for_repeat(u) in repeated:
                    continue
                if re.fullmatch(r"\d{1,4}", u.strip()):  # numero di pagina isolato
                    continue
            keep.append(u)
        cleaned.append(keep)
    return cleaned


def lines_to_paragraphs(lines):
    """Raggruppa righe (da pypdf) in paragrafi: un paragrafo finisce quando una
    riga termina con punteggiatura ed e' sensibilmente piu' corta delle altre."""
    if not lines:
        return []
    lens = sorted(len(l) for l in lines)
    ref_len = lens[int(0.9 * (len(lens) - 1))]
    paras, cur = [], []
    for i, l in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        # una battuta di dialogo dopo una frase conclusa apre un nuovo paragrafo
        if cur and l.lstrip().startswith(DIALOGUE_START) and cur[-1].rstrip().endswith(SENT_END):
            paras.append("\n".join(cur))
            cur = []
        # titolo: riga corta, senza punteggiatura finale, a inizio paragrafo,
        # seguita da una riga che comincia con maiuscola
        is_heading = (
            not cur
            and len(l) < 0.5 * ref_len
            and not l.rstrip().endswith(SENT_END + (",", "-"))
            and nxt[:1].isupper()
        )
        if is_heading:
            paras.append(l)
            continue
        cur.append(l)
        if l.rstrip().endswith(SENT_END) and len(l) < 0.75 * ref_len:
            paras.append("\n".join(cur))
            cur = []
    if cur:
        paras.append("\n".join(cur))
    return paras


def normalize_paragraph(p):
    p = p.translate(TRANSLATE)
    # parola spezzata a fine riga: "paro-\nla" -> "parola"
    p = re.sub(r"([A-Za-zÀ-ÿ])-[ \t]*\n[ \t]*([a-zà-ÿ])", r"\1\2", p)
    p = re.sub(r"\s*\n\s*", " ", p)
    p = re.sub(r"https?://\S+|www\.\S+", "link", p)
    p = re.sub(r"[ \t]+", " ", p)
    return p.strip()


def assemble_paragraphs(paragraphs):
    """Prepara i paragrafi da leggere: ricuce quelli spezzati da un cambio pagina e
    riconosce i titoli (paragrafi brevi senza punteggiatura finale), a cui mette un
    punto cosi' la voce chiude bene la frase.
    Ritorna una lista di (testo, e_un_titolo)."""
    out = []
    for p in paragraphs:
        if not p:
            continue
        if out and not out[-1].endswith(SENT_END) and len(out[-1]) >= 80 and p[0].islower():
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    final = []
    for p in out:
        is_heading = not p.endswith(SENT_END) and len(p) < 80
        final.append((p + "." if is_heading else p, is_heading))
    return final


def prepare_pages(data):
    """Ritorna, per ogni pagina, la lista di paragrafi gia' puliti."""
    units = strip_repeated(data.units)
    pages = []
    for u in units:
        paras = u if data.grouped else lines_to_paragraphs(u)
        pages.append([x for x in (normalize_paragraph(p) for p in paras) if x])
    return pages


# ---------------------------------------------------------------------------
# Suddivisione in sezioni (file audio)
# ---------------------------------------------------------------------------
def parse_page_range(spec, total):
    if not spec or not spec.strip():
        return 1, total
    m = re.fullmatch(r"\s*(\d*)\s*-\s*(\d*)\s*", spec)
    if m:
        a = int(m.group(1)) if m.group(1) else 1
        b = int(m.group(2)) if m.group(2) else total
    elif spec.strip().isdigit():
        a = b = int(spec)
    else:
        raise JobError(f"intervallo pagine non valido: '{spec}' (esempi: 5-120, 5-, -80)")
    a0, b0 = a, b
    a, b = max(1, a), min(total, b)
    if a > b:
        raise JobError(f"le pagine {a0}-{b0} non esistono: il documento ha {total} pagine")
    return a, b


def build_sections(pages, toc, mode, pages_per_file, p_start, p_end, book_title, log=print):
    """Ritorna una lista di (titolo, [(paragrafo, e_un_titolo), ...])."""

    def text_for(a, b):
        paras = []
        for i in range(a - 1, b):
            paras.extend(pages[i])
        return assemble_paragraphs(paras)

    if mode == "chapters":
        entries = sorted({p: t for t, p in toc if p_start <= p <= p_end}.items())
        if not entries:
            log("ATTENZIONE: il PDF non ha segnalibri/capitoli utilizzabili, passo alla modalita' 'pages'.")
            mode = "pages"
        else:
            sections = []
            if entries[0][0] > p_start:
                txt = text_for(p_start, entries[0][0] - 1)
                if txt:
                    sections.append(("Inizio", txt))
            for idx, (page, title) in enumerate(entries):
                end = entries[idx + 1][0] - 1 if idx + 1 < len(entries) else p_end
                txt = text_for(page, end)
                if txt:
                    sections.append((title, txt))
            return sections

    if mode == "pages":
        sections = []
        a = p_start
        while a <= p_end:
            b = min(p_end, a + pages_per_file - 1)
            txt = text_for(a, b)
            if txt:
                sections.append((f"pagine {a}-{b}", txt))
            a = b + 1
        return sections

    txt = text_for(p_start, p_end)
    return [(book_title, txt)] if txt else []


def _split_long_sentence(s, max_chars):
    parts, cur = [], ""
    for piece in re.split(r"(?<=[,;:])\s+", s):
        if len(piece) > max_chars:  # caso estremo: spezzo per parole
            for w in piece.split(" "):
                if len(cur) + len(w) + 1 > max_chars and cur:
                    parts.append(cur)
                    cur = w
                else:
                    cur = f"{cur} {w}".strip()
            continue
        if len(cur) + len(piece) + 1 > max_chars and cur:
            parts.append(cur)
            cur = piece
        else:
            cur = f"{cur} {piece}".strip()
    if cur:
        parts.append(cur)
    return parts


def split_chunks(text, max_chars=MAX_CHUNK_CHARS):
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    chunks, cur = [], ""
    for s in sentences:
        pieces = _split_long_sentence(s, max_chars) if len(s) > max_chars else [s]
        for piece in pieces:
            if cur and len(cur) + len(piece) + 1 > max_chars:
                chunks.append(cur)
                cur = piece
            else:
                cur = f"{cur} {piece}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def slugify(s, maxlen=50):
    s = re.sub(r"[^\w\- ]+", "", s, flags=re.UNICODE).strip().replace(" ", "_")
    return (s or "sezione")[:maxlen]


# ---------------------------------------------------------------------------
# Unita' di sintesi e pause tra paragrafi
# ---------------------------------------------------------------------------
SHORT_PARA = 120   # paragrafi piu' corti di cosi' (tipicamente dialoghi) si accorpano...
MERGE_MAX = 600    # ...fino a questa lunghezza, in un'unica richiesta vocale


def make_units(paragraphs, max_chars=MAX_CHUNK_CHARS):
    """Trasforma i paragrafi in unita' da sintetizzare: [(testo, tipo)].
    tipo: 'head' = titolo, 'para' = fine paragrafo, 'cont' = il paragrafo
    prosegue nell'unita' successiva (nessuna pausa)."""
    units = []
    last_short = False  # l'ultima unita' e' fatta solo di paragrafi brevi (accorpabile)
    for text, is_heading in paragraphs:
        if is_heading:
            units.append((text, "head"))
            last_short = False
            continue
        if len(text) > max_chars:
            pieces = split_chunks(text, max_chars)
            for k, piece in enumerate(pieces):
                units.append((piece, "para" if k == len(pieces) - 1 else "cont"))
            last_short = False
            continue
        short = len(text) < SHORT_PARA
        if (short and last_short and units and units[-1][1] == "para"
                and len(units[-1][0]) + 1 + len(text) <= MERGE_MAX):
            units[-1] = (units[-1][0] + " " + text, "para")
        else:
            units.append((text, "para"))
            last_short = short
    return units


_BR_MPEG1 = [None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None]
_BR_MPEG2 = [None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None]
_SAMPLE_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _parse_mp3_header(b):
    if len(b) < 4 or b[0] != 0xFF or (b[1] & 0xE0) != 0xE0:
        return None
    version, layer = (b[1] >> 3) & 3, (b[1] >> 1) & 3
    if version == 1 or layer != 1:  # solo MPEG Layer III
        return None
    bi, si = (b[2] >> 4) & 15, (b[2] >> 2) & 3
    if bi in (0, 15) or si == 3:
        return None
    bitrate = (_BR_MPEG1 if version == 3 else _BR_MPEG2)[bi] * 1000
    sr = _SAMPLE_RATES[version][si]
    return {"b1": b[1], "b2": b[2], "b3": b[3], "version": version, "bitrate": bitrate, "sr": sr}


def _id3_size(data):
    if data[:3] == b"ID3" and len(data) >= 10:
        return 10 + ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F))
    return 0


def _first_mp3_frame(data):
    """Ritorna (offset, header) del primo frame MP3, oppure None."""
    i = _id3_size(data)
    end = min(len(data) - 4, i + 8192)
    while i <= end:
        h = _parse_mp3_header(data[i:i + 4])
        if h:
            return i, h
        i += 1
    return None


def _frame_length(h):
    return (144 if h["version"] == 3 else 72) * h["bitrate"] // h["sr"]  # senza padding


def strip_mp3_tags(data):
    """Toglie l'ID3 iniziale e l'eventuale frame Xing/Info (non e' audio): serve per
    concatenare MP3 byte per byte senza glitch ai punti di giunzione."""
    found = _first_mp3_frame(data)
    if not found:
        return data
    off, h = found
    if b"Xing" in data[off:off + 48] or b"Info" in data[off:off + 48]:
        return data[off + _frame_length(h):]
    return data[off:]


def make_silence(ref_audio, ms):
    """Genera 'ms' millisecondi di silenzio come frame MP3 vuoti, con gli stessi
    parametri (frequenza, canali) dell'audio di riferimento: nessuna dipendenza da ffmpeg."""
    found = _first_mp3_frame(strip_mp3_tags(ref_audio))
    h = found[1] if found else _parse_mp3_header(bytes([0xFF, 0xF3, 0x64, 0xC0]))  # 24 kHz mono 48 kbps
    mpeg1 = h["version"] == 3
    samples = 1152 if mpeg1 else 576
    header = bytes([0xFF, h["b1"] | 0x01, h["b2"] & 0xFD, h["b3"] & 0xF0])  # niente CRC, niente padding
    frame = header + bytes(_frame_length(h) - 4)
    n = max(1, round(ms / 1000 / (samples / h["sr"])))
    return frame * n


def interleave_with_silence(units, paths, chunks_dir, pause_para_ms, pause_head_ms):
    """Ritorna l'elenco di file da unire: audio delle unita' con il silenzio in mezzo."""
    if pause_para_ms <= 0 and pause_head_ms <= 0:
        return list(paths)
    with open(paths[0], "rb") as f:
        ref = f.read(16384)
    cache = {}

    def silence(ms):
        if ms not in cache:
            sp = chunks_dir / f"silence_{ms}.mp3"
            sp.write_bytes(make_silence(ref, ms))
            cache[ms] = sp
        return cache[ms]

    parts = []
    for i, path in enumerate(paths):
        parts.append(path)
        if i == len(units) - 1:
            break
        kind, next_kind = units[i][1], units[i + 1][1]
        if kind == "cont":
            continue  # lo stesso paragrafo prosegue: nessuna pausa
        ms = pause_head_ms if kind == "head" else pause_para_ms
        if next_kind == "head":
            ms = max(ms, pause_head_ms)  # una pausa piu' lunga anche prima di un titolo
        if ms > 0:
            parts.append(silence(ms))
    return parts


# ---------------------------------------------------------------------------
# Sintesi vocale
# ---------------------------------------------------------------------------
def _import_edge_tts():
    try:
        import edge_tts
        return edge_tts
    except ImportError:
        raise JobError("manca edge-tts. Installa con: pip install -U edge-tts")


async def _sleep_unless_cancelled(seconds, cancel):
    waited = 0.0
    while waited < seconds:
        if cancel is not None and cancel.is_set():
            raise ConversionCancelled()
        await asyncio.sleep(0.25)
        waited += 0.25


async def synth_chunk(edge_tts, text, voice, rate, pitch, out_path, retries=5, cancel=None):
    part = out_path.with_suffix(".part")
    last_err = None
    for attempt in range(retries):
        if cancel is not None and cancel.is_set():
            raise ConversionCancelled()
        try:
            await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(str(part))
            if part.exists() and part.stat().st_size > 0:
                part.replace(out_path)
                return
            last_err = "file audio vuoto"
        except Exception as e:  # rete, throttling, ecc. (CancelledError non e' un Exception)
            last_err = e
        await _sleep_unless_cancelled(min(30, 2 ** attempt), cancel)
    raise RuntimeError(f"sintesi fallita dopo {retries} tentativi: {last_err}")


def merge_audio(parts, out_path, log=print):
    if len(parts) == 1:
        shutil.copyfile(parts[0], out_path)
        return
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        list_file = out_path.with_suffix(".concat.txt")
        lines = []
        for p in parts:
            safe = p.resolve().as_posix().replace("'", "'\\''")
            lines.append(f"file '{safe}'\n")
        list_file.write_text("".join(lines), encoding="utf-8")
        flags = {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # niente finestra nera
        r = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(list_file), "-c", "copy", str(out_path)],
            capture_output=True, text=True, **flags,
        )
        list_file.unlink(missing_ok=True)
        if r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return
        log(f"  (ffmpeg non e' riuscito a unire i pezzi: {r.stderr.strip()[:150]} - uso l'unione semplice)")
    with open(out_path, "wb") as out:  # gli MP3 si possono concatenare byte per byte
        for p in parts:
            out.write(strip_mp3_tags(p.read_bytes()))


async def synthesize_sections(edge_tts, sections, outdir, voice, rate, pitch, concurrency,
                              single_name, keep_chunks, pause_para_ms, pause_head_ms,
                              log=print, progress=None, cancel=None):
    chunks_dir = outdir / "_chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    all_units = [make_units(paras) for _, paras in sections]
    digest = hashlib.md5("\x00".join(t for units in all_units for t, _ in units).encode("utf-8")).hexdigest()
    # le pause NON sono nel meta: si applicano all'unione, quindi si possono
    # cambiare (con --keep-chunks) senza rigenerare la voce
    meta = {"voice": voice, "rate": rate, "pitch": pitch, "max_chunk": MAX_CHUNK_CHARS, "digest": digest}
    meta_path = chunks_dir / "meta.json"
    if meta_path.exists() and json.loads(meta_path.read_text(encoding="utf-8")) != meta:
        log("Impostazioni o contenuto cambiati rispetto alla volta scorsa: rigenero i pezzi audio da zero.")
        for f in chunks_dir.glob("*"):
            f.unlink()
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    def is_done(p):
        return p.exists() and p.stat().st_size > 0

    all_paths = [[chunks_dir / f"s{si:03d}_c{ci:05d}.mp3" for ci in range(len(units))]
                 for si, units in enumerate(all_units, start=1)]
    total_all = sum(len(u) for u in all_units)
    done_all = sum(1 for paths in all_paths for p in paths if is_done(p))
    if progress:
        progress(done_all, total_all, "")

    sem = asyncio.Semaphore(concurrency)
    outputs = []

    for si, ((title, _), units, paths) in enumerate(zip(sections, all_units, all_paths), start=1):
        if cancel is not None and cancel.is_set():
            raise ConversionCancelled()
        todo = [(u[0], p) for u, p in zip(units, paths) if not is_done(p)]
        label = f"[{si}/{len(sections)}] {title}"
        log(f"{label}: {len(units)} pezzi ({len(todo)} da generare)")

        async def worker(c, p):
            nonlocal done_all
            async with sem:
                await synth_chunk(edge_tts, c, voice, rate, pitch, p, cancel=cancel)
            done_all += 1
            if progress:
                progress(done_all, total_all, label)

        tasks = [asyncio.ensure_future(worker(c, p)) for c, p in todo]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        if len(sections) == 1 and single_name:
            out_path = outdir / f"{single_name}.mp3"
        else:
            out_path = outdir / f"{si:02d}_{slugify(title)}.mp3"
        parts = interleave_with_silence(units, paths, chunks_dir, pause_para_ms, pause_head_ms)
        merge_audio(parts, out_path, log)
        outputs.append(out_path)
        log(f"    -> {out_path.name}")

    if not keep_chunks:
        shutil.rmtree(chunks_dir, ignore_errors=True)
    return outputs


async def cmd_list_voices(edge_tts):
    voices = await edge_tts.list_voices()
    it = [v for v in voices if v.get("Locale", "").lower().startswith("it-")]
    print("Voci italiane disponibili:")
    for v in it:
        print(f"  {v['ShortName']:<36} {v.get('Gender', ''):<7} {v.get('FriendlyName', '')}")


async def cmd_samples(edge_tts, text, outdir, rate, pitch):
    voices = await edge_tts.list_voices()
    it = [v["ShortName"] for v in voices if v.get("Locale", "").lower().startswith("it-")]
    sample_text = " ".join(split_chunks(text, 500)[:1])
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"Genero un provino per {len(it)} voci italiane...")
    for name in it:
        out = outdir / f"provino_{name}.mp3"
        await synth_chunk(edge_tts, sample_text, name, rate, pitch, out)
        print(f"  {out.name}")
    print("Ascoltali e scegli la voce che preferisci, poi usa --voice NOME.")


# ---------------------------------------------------------------------------
# API per uso da programma (usata anche dall'interfaccia grafica)
# ---------------------------------------------------------------------------
DEFAULT_SAMPLE_TEXT = ("Era una sera d'autunno quando il vecchio faro si accese per l'ultima volta. "
                       "Il guardiano restò a guardare il mare scuro, senza dire nulla.")


@dataclass
class Job:
    pdf: Path
    outdir: Optional[Path] = None
    voice: str = DEFAULT_VOICE
    rate: str = "+0%"
    pitch: str = "+0Hz"
    pause_paragraph: int = 600
    pause_heading: int = 1200
    pages: Optional[str] = None
    mode: str = "single"
    pages_per_file: int = 20
    concurrency: int = 3
    keep_chunks: bool = False


@dataclass
class Prepared:
    sections: list
    p_start: int
    p_end: int
    total_pages: int
    text_len: int
    n_units: int
    hours: float

    def summary(self):
        return (f"Pagine {self.p_start}-{self.p_end} di {self.total_pages}: {self.text_len:,} caratteri, "
                f"circa {self.hours:.1f} ore di audio, {len(self.sections)} file, {self.n_units} richieste vocali.")


def validate_format(rate, pitch):
    if not re.fullmatch(r"[+-]\d+%", rate):
        raise JobError("la velocita' deve avere la forma +10% oppure -5%")
    if not re.fullmatch(r"[+-]\d+Hz", pitch):
        raise JobError("il tono deve avere la forma +2Hz oppure -2Hz")


def validate_job(job):
    if not job.pdf or not str(job.pdf).strip():
        raise JobError("scegli il file PDF da convertire")
    job.pdf = Path(job.pdf)
    if not job.pdf.is_file():
        raise JobError(f"file non trovato: {job.pdf}")
    validate_format(job.rate, job.pitch)
    if job.pause_paragraph < 0 or job.pause_heading < 0:
        raise JobError("le pause non possono essere negative")
    if job.mode not in ("single", "chapters", "pages"):
        raise JobError(f"modalità non valida: {job.mode}")
    if job.pages_per_file < 1:
        raise JobError("il numero di pagine per file deve essere almeno 1")
    if not 1 <= job.concurrency <= 10:
        raise JobError("le richieste in parallelo devono essere tra 1 e 10")


def resolve_outdir(job):
    return Path(job.outdir) if job.outdir else Path(job.pdf).with_name(Path(job.pdf).stem + "_audiolibro")


def prepare(job, log=print):
    """Legge il PDF e prepara le sezioni da leggere. Non genera audio."""
    validate_job(job)
    log(f"Leggo {job.pdf.name}...")
    data = load_pdf(job.pdf, log)
    total_pages = len(data.units)
    p_start, p_end = parse_page_range(job.pages, total_pages)

    pages = prepare_pages(data)
    total_chars = sum(len(p) for i in range(p_start - 1, p_end) for p in pages[i])
    n_sel = p_end - p_start + 1
    if total_chars == 0:
        raise JobError("non ho trovato testo nel PDF. Se è una scansione (immagini) serve prima un OCR.")
    if total_chars / n_sel < 200:
        log("ATTENZIONE: pochissimo testo per pagina: potrebbe essere una scansione o un PDF di sole immagini.")

    sections = build_sections(pages, data.toc, job.mode, job.pages_per_file, p_start, p_end, job.pdf.stem, log)
    if not sections:
        raise JobError("nessun testo da leggere nell'intervallo scelto")
    text_len = sum(len(t) for _, paras in sections for t, _ in paras)
    n_units = sum(len(make_units(paras)) for _, paras in sections)
    hours = text_len / SPEECH_CHARS_PER_SECOND / 3600
    return Prepared(sections, p_start, p_end, total_pages, text_len, n_units, hours)


def write_text_preview(job, prepared, log=print):
    """Salva in .txt il testo che verrebbe letto (un paragrafo per riga vuota). Ritorna i percorsi."""
    outdir = resolve_outdir(job)
    outdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, (title, paras) in enumerate(prepared.sections, start=1):
        out = outdir / f"{i:02d}_{slugify(title)}_testo.txt"
        out.write_text("\n\n".join(t for t, _ in paras), encoding="utf-8")
        log(f"  testo salvato: {out.name}")
        paths.append(out)
    return paths


def convert(job, prepared=None, log=print, progress=None, cancel=None):
    """Esegue la conversione. progress(fatti, totali, etichetta) viene chiamata a ogni pezzo;
    cancel è un oggetto con is_set() (es. threading.Event) per interrompere. Ritorna i file creati."""
    prepared = prepared or prepare(job, log)
    outdir = resolve_outdir(job)
    outdir.mkdir(parents=True, exist_ok=True)
    edge_tts = _import_edge_tts()
    log(f"Voce: {job.voice}, velocità {job.rate}, tono {job.pitch}, "
        f"pause {job.pause_paragraph}/{job.pause_heading} ms")
    return asyncio.run(synthesize_sections(
        edge_tts, prepared.sections, outdir, job.voice, job.rate, job.pitch, job.concurrency,
        job.pdf.stem, job.keep_chunks, job.pause_paragraph, job.pause_heading,
        log=log, progress=progress, cancel=cancel,
    ))


def list_italian_voices():
    """Ritorna [(nome, genere, descrizione)] delle voci italiane (serve internet)."""
    edge_tts = _import_edge_tts()
    voices = asyncio.run(edge_tts.list_voices())
    return [(v["ShortName"], v.get("Gender", ""), v.get("FriendlyName", ""))
            for v in voices if v.get("Locale", "").lower().startswith("it-")]


def sample_text(prepared, max_chars=500):
    first = " ".join(t for t, _ in prepared.sections[0][1]) if prepared and prepared.sections else ""
    chunks = split_chunks(first, max_chars) if first else []
    return chunks[0] if chunks else DEFAULT_SAMPLE_TEXT


def make_voice_sample(voice, text, rate, pitch, out_path):
    validate_format(rate, pitch)
    edge_tts = _import_edge_tts()
    asyncio.run(synth_chunk(edge_tts, text, voice, rate, pitch, Path(out_path)))
    return Path(out_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args():
    ap = argparse.ArgumentParser(description="Converte un PDF (testo selezionabile) in audiolibro MP3.")
    ap.add_argument("pdf", nargs="?", help="file PDF da convertire")
    ap.add_argument("--voice", default=DEFAULT_VOICE, help=f"voce edge-tts (default {DEFAULT_VOICE})")
    ap.add_argument("--rate", default="+0%", help="velocita', es. -10%% piu' lento, +10%% piu' veloce")
    ap.add_argument("--pitch", default="+0Hz", help="tono, es. -2Hz")
    ap.add_argument("--pause-paragraph", type=int, default=600, metavar="MS",
                    help="pausa tra un paragrafo e l'altro, in millisecondi (default 600)")
    ap.add_argument("--pause-heading", type=int, default=1200, metavar="MS",
                    help="pausa prima e dopo i titoli, in millisecondi (default 1200)")
    ap.add_argument("--no-pauses", action="store_true", help="non inserire pause extra tra i paragrafi")
    ap.add_argument("--pages", help="intervallo di pagine, es. 9-310 (utile per saltare indice/copertina)")
    ap.add_argument("--mode", choices=["single", "chapters", "pages"], default="single",
                    help="single=un solo MP3; chapters=uno per capitolo (segnalibri del PDF); pages=uno ogni N pagine")
    ap.add_argument("--pages-per-file", type=int, default=20)
    ap.add_argument("--outdir", help="cartella di output (default: <nomepdf>_audiolibro)")
    ap.add_argument("--concurrency", type=int, default=3, help="richieste vocali in parallelo (default 3)")
    ap.add_argument("--keep-chunks", action="store_true",
                    help="non cancellare i pezzi audio: permette di cambiare le pause senza rigenerare la voce")
    ap.add_argument("--dry-run", action="store_true", help="non genera audio: salva solo il testo che verrebbe letto")
    ap.add_argument("--sample", action="store_true", help="genera un breve provino con ciascuna voce italiana")
    ap.add_argument("--list-voices", action="store_true", help="elenca le voci italiane disponibili")
    return ap.parse_args()


class _CliOutput:
    """Messaggi e avanzamento sulla riga di comando."""

    def __init__(self):
        self._open = False

    def log(self, msg):
        if self._open:
            print()
            self._open = False
        print(msg)

    def progress(self, done, total, label):
        print(f"\r    {done}/{total}", end="", flush=True)
        self._open = True


def main():
    args = parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    out = _CliOutput()
    try:
        if args.list_voices:
            asyncio.run(cmd_list_voices(_import_edge_tts()))
            return
        if not args.pdf:
            raise JobError("indica il file PDF (oppure usa --list-voices).")

        job = Job(
            pdf=Path(args.pdf),
            outdir=Path(args.outdir) if args.outdir else None,
            voice=args.voice, rate=args.rate, pitch=args.pitch,
            pause_paragraph=0 if args.no_pauses else args.pause_paragraph,
            pause_heading=0 if args.no_pauses else args.pause_heading,
            pages=args.pages, mode=args.mode, pages_per_file=args.pages_per_file,
            concurrency=args.concurrency, keep_chunks=args.keep_chunks,
        )
        prepared = prepare(job, out.log)
        out.log(prepared.summary())

        if args.dry_run:
            write_text_preview(job, prepared, out.log)
            out.log("Controlla i file .txt: ogni paragrafo è separato da una riga vuota, ed è lì che la voce "
                    "farà una pausa. È esattamente quello che verrà letto. Poi rilancia senza --dry-run.")
            return

        if args.sample:
            edge_tts = _import_edge_tts()
            first = " ".join(t for t, _ in prepared.sections[0][1])
            asyncio.run(cmd_samples(edge_tts, first, resolve_outdir(job), job.rate, job.pitch))
            return

        outputs = convert(job, prepared, out.log, out.progress)
        out.log(f"\nFatto. {len(outputs)} file in: {resolve_outdir(job)}")
    except JobError as e:
        sys.exit(f"ERRORE: {e}")
    except KeyboardInterrupt:
        sys.exit("\nInterrotto. Rilancia lo stesso comando per riprendere da dove eri arrivato.")
    except RuntimeError as e:
        sys.exit(f"\nERRORE: {e}\nI pezzi già generati sono salvati: controlla la connessione, poi rilancia "
                 "lo stesso comando per riprendere. Se l'errore persiste (es. 403), aggiorna la libreria: "
                 "pip install -U edge-tts")


if __name__ == "__main__":
    main()
