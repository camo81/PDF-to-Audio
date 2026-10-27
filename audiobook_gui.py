#!/usr/bin/env python3
"""
audiobook_gui.py - interfaccia grafica (Tkinter) per pdf_to_audiobook.py

Avvio:
    python audiobook_gui.py

Tutta la logica sta in pdf_to_audiobook.py: qui c'e' solo la finestra. Il lavoro
pesante gira in un thread separato, cosi' la finestra resta reattiva e si puo'
interrompere; i messaggi tornano alla finestra tramite una coda.
"""

import asyncio
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import pdf_to_audiobook as core

SETTINGS_PATH = Path.home() / ".audiolibro_pdf_gui.json"
FALLBACK_VOICES = ["it-IT-IsabellaNeural", "it-IT-ElsaNeural", "it-IT-DiegoNeural"]
GENDER_IT = {"Female": "voce femminile", "Male": "voce maschile"}


# ---------------------------------------------------------------------------
# Funzioni di supporto (senza grafica)
# ---------------------------------------------------------------------------
def fmt_rate(v):
    return f"{int(v):+d}%"


def fmt_pitch(v):
    return f"{int(v):+d}Hz"


def to_int(var, default):
    try:
        return int(float(var.get()))
    except (tk.TclError, ValueError):
        return default


def open_path(path):
    """Apre un file o una cartella con il programma predefinito del sistema."""
    path = str(path)
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 (solo Windows)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        raise RuntimeError(f"Non riesco ad aprire {path}: {e}")


def load_settings():
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass  # le impostazioni sono solo una comodita'


class Task(threading.Thread):
    """Esegue una funzione in un thread e manda l'esito alla coda della finestra."""

    def __init__(self, kind, q, fn):
        super().__init__(daemon=True)
        self.kind, self.q, self.fn = kind, q, fn

    def run(self):
        try:
            result = self.fn()
            self.q.put(("done", self.kind, result))
        except core.ConversionCancelled:
            self.q.put(("cancelled", self.kind, None))
        except core.JobError as e:
            self.q.put(("error", self.kind, str(e)))
        except RuntimeError as e:
            self.q.put(("error", self.kind,
                        f"{e}\n\nI pezzi già generati restano salvati: controlla la connessione e premi di nuovo "
                        "«Avvia conversione» per riprendere da dove eri arrivato."))
        except Exception as e:  # imprevisto: dettaglio nel registro, messaggio breve nella finestra
            self.q.put(("log", self.kind, traceback.format_exc()))
            self.q.put(("error", self.kind, f"Errore imprevisto: {type(e).__name__}: {e}"))


# ---------------------------------------------------------------------------
# Finestra
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("Audiolibro da PDF")
        root.minsize(880, 560)

        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.busy_kind = None
        self.last_outdir = None
        self.voices = {}
        self._auto_outdir = ""
        self.settings = load_settings()
        self.inputs = []

        self._build_vars()
        self._build_ui()
        self._apply_settings()
        self._refresh_labels()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)
        self.load_voices()

    # ----------------------------------------------------------------- variabili
    def _build_vars(self):
        self.pdf_var = tk.StringVar()
        self.outdir_var = tk.StringVar()
        self.voice_var = tk.StringVar(value=core.DEFAULT_VOICE)
        self.voice_info_var = tk.StringVar()
        self.rate_var = tk.IntVar(value=0)
        self.pitch_var = tk.IntVar(value=0)
        self.pp_var = tk.IntVar(value=600)
        self.ph_var = tk.IntVar(value=1200)
        self.pages_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="single")
        self.ppf_var = tk.IntVar(value=20)
        self.keep_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Scegli un PDF per cominciare.")
        self.rate_text = tk.StringVar()
        self.pitch_text = tk.StringVar()
        self.pp_text = tk.StringVar()
        self.ph_text = tk.StringVar()

    # ---------------------------------------------------------------------- UI
    def _scale(self, parent, row, label, var, lo, hi, step, text_var, length=230):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        sc = tk.Scale(parent, from_=lo, to=hi, resolution=step, orient="horizontal", showvalue=False,
                      variable=var, length=length, command=self._on_scale)
        sc.grid(row=row, column=1, sticky="w", padx=6)
        ttk.Label(parent, textvariable=text_var, width=9).grid(row=row, column=2, sticky="w")
        self.inputs.append(sc)
        return sc

    def _build_ui(self):
        # Layout su due colonne: l'altezza resta sotto i 700 pixel (portatili 1366x768).
        root = self.root
        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, weight=1)
        pad = {"padx": 8, "pady": 4}

        # 1. Libro (tutta la larghezza)
        f1 = ttk.LabelFrame(root, text=" 1. Libro ", padding=8)
        f1.grid(row=0, column=0, columnspan=2, sticky="ew", **pad)
        f1.columnconfigure(1, weight=1)
        ttk.Label(f1, text="File PDF:").grid(row=0, column=0, sticky="w")
        e_pdf = ttk.Entry(f1, textvariable=self.pdf_var)
        e_pdf.grid(row=0, column=1, sticky="ew", padx=6)
        b_pdf = ttk.Button(f1, text="Sfoglia…", command=self.browse_pdf)
        b_pdf.grid(row=0, column=2)
        ttk.Label(f1, text="Salva in:").grid(row=1, column=0, sticky="w", pady=(5, 0))
        e_out = ttk.Entry(f1, textvariable=self.outdir_var)
        e_out.grid(row=1, column=1, sticky="ew", padx=6, pady=(5, 0))
        b_out = ttk.Button(f1, text="Sfoglia…", command=self.browse_outdir)
        b_out.grid(row=1, column=2, pady=(5, 0))
        self.inputs += [e_pdf, b_pdf, e_out, b_out]

        # Colonna sinistra: voce + pulsanti
        left = ttk.Frame(root)
        left.grid(row=1, column=0, sticky="nsew")
        left.columnconfigure(0, weight=1)

        f2 = ttk.LabelFrame(left, text=" 2. Voce ", padding=8)
        f2.grid(row=0, column=0, sticky="ew", **pad)
        ttk.Label(f2, text="Voce:").grid(row=0, column=0, sticky="w")
        self.voice_box = ttk.Combobox(f2, textvariable=self.voice_var, values=FALLBACK_VOICES, width=28)
        self.voice_box.grid(row=0, column=1, sticky="w", padx=6)
        self.voice_box.bind("<<ComboboxSelected>>", lambda _e=None: self._refresh_voice_info())
        self.voice_btn = ttk.Button(f2, text="Prova la voce", command=self.test_voice)
        self.voice_btn.grid(row=0, column=2)
        ttk.Label(f2, textvariable=self.voice_info_var, foreground="gray").grid(row=1, column=1, sticky="w", padx=6)
        self.inputs += [self.voice_box]
        self._scale(f2, 2, "Velocità:", self.rate_var, -30, 30, 1, self.rate_text)
        self._scale(f2, 3, "Tono:", self.pitch_var, -20, 20, 1, self.pitch_text)
        ttk.Label(f2, text="Suggerimento: prova velocità −7% e tono −2 Hz per una\n"
                           "voce meno sintetica. «Prova la voce» legge un brano\n"
                           "del tuo libro e lo apre nel lettore audio.",
                  foreground="gray", justify="left").grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        bar = ttk.Frame(left)
        bar.grid(row=1, column=0, sticky="ew", **pad)
        self.preview_btn = ttk.Button(bar, text="Anteprima testo", command=self.preview_text)
        self.preview_btn.grid(row=0, column=0, padx=(0, 4))
        self.start_btn = ttk.Button(bar, text="Avvia conversione", command=self.start)
        self.start_btn.grid(row=0, column=1, padx=4)
        self.stop_btn = ttk.Button(bar, text="Interrompi", command=self.stop, state="disabled")
        self.stop_btn.grid(row=1, column=0, padx=(0, 4), pady=(4, 0))
        self.open_btn = ttk.Button(bar, text="Apri cartella", command=self.open_folder, state="disabled")
        self.open_btn.grid(row=1, column=1, padx=4, pady=(4, 0))

        # Colonna destra: pause + cosa convertire
        right = ttk.Frame(root)
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)

        f3 = ttk.LabelFrame(right, text=" 3. Pause ", padding=8)
        f3.grid(row=0, column=0, sticky="ew", **pad)
        self._scale(f3, 0, "Tra i paragrafi:", self.pp_var, 0, 2000, 50, self.pp_text)
        self._scale(f3, 1, "Attorno ai titoli:", self.ph_var, 0, 4000, 100, self.ph_text)

        f4 = ttk.LabelFrame(right, text=" 4. Cosa convertire ", padding=8)
        f4.grid(row=1, column=0, sticky="ew", **pad)
        ttk.Label(f4, text="Pagine:").grid(row=0, column=0, sticky="w")
        e_pages = ttk.Entry(f4, textvariable=self.pages_var, width=12)
        e_pages.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(f4, text="es. 9-310 · vuoto = tutto", foreground="gray").grid(row=0, column=2, sticky="w")
        r1 = ttk.Radiobutton(f4, text="Un unico file", variable=self.mode_var, value="single")
        r1.grid(row=1, column=0, columnspan=3, sticky="w", pady=(5, 0))
        r2 = ttk.Radiobutton(f4, text="Un file per capitolo (segnalibri del PDF)",
                             variable=self.mode_var, value="chapters")
        r2.grid(row=2, column=0, columnspan=3, sticky="w")
        row3 = ttk.Frame(f4)
        row3.grid(row=3, column=0, columnspan=3, sticky="w")
        r3 = ttk.Radiobutton(row3, text="Un file ogni", variable=self.mode_var, value="pages")
        r3.grid(row=0, column=0)
        spin = tk.Spinbox(row3, from_=1, to=1000, width=5, textvariable=self.ppf_var)
        spin.grid(row=0, column=1, padx=4)
        ttk.Label(row3, text="pagine").grid(row=0, column=2)
        keep = ttk.Checkbutton(f4, text="Conserva i pezzi audio\n(per ritoccare le pause senza rigenerare la voce)",
                               variable=self.keep_var)
        keep.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.inputs += [e_pages, r1, r2, r3, spin, keep]

        # Avanzamento e registro (tutta la larghezza)
        self.pbar = ttk.Progressbar(root, mode="determinate", maximum=100, value=0)
        self.pbar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(4, 0))
        ttk.Label(root, textvariable=self.status_var).grid(row=3, column=0, columnspan=2, sticky="w", padx=8)
        self.log_text = ScrolledText(root, height=7, wrap="word", state="disabled")
        self.log_text.grid(row=4, column=0, columnspan=2, sticky="nsew", padx=8, pady=(4, 8))
        root.rowconfigure(4, weight=1)

    # ---------------------------------------------------------------- impostazioni
    def _apply_settings(self):
        s = self.settings
        self.voice_var.set(s.get("voice", core.DEFAULT_VOICE))
        self.rate_var.set(s.get("rate", 0))
        self.pitch_var.set(s.get("pitch", 0))
        self.pp_var.set(s.get("pause_paragraph", 600))
        self.ph_var.set(s.get("pause_heading", 1200))
        self.mode_var.set(s.get("mode", "single"))
        self.ppf_var.set(s.get("pages_per_file", 20))
        self.keep_var.set(bool(s.get("keep_chunks", False)))

    def _store_settings(self):
        self.settings.update({
            "voice": self.voice_var.get(),
            "rate": to_int(self.rate_var, 0),
            "pitch": to_int(self.pitch_var, 0),
            "pause_paragraph": to_int(self.pp_var, 600),
            "pause_heading": to_int(self.ph_var, 1200),
            "mode": self.mode_var.get(),
            "pages_per_file": to_int(self.ppf_var, 20),
            "keep_chunks": bool(self.keep_var.get()),
        })
        save_settings(self.settings)

    # ------------------------------------------------------------------- etichette
    def _on_scale(self, _value=None):
        self._refresh_labels()

    def _refresh_labels(self):
        self.rate_text.set(fmt_rate(to_int(self.rate_var, 0)))
        self.pitch_text.set(f"{to_int(self.pitch_var, 0):+d} Hz")
        self.pp_text.set(f"{to_int(self.pp_var, 0)} ms")
        self.ph_text.set(f"{to_int(self.ph_var, 0)} ms")
        self._refresh_voice_info()

    def _refresh_voice_info(self):
        info = self.voices.get(self.voice_var.get().strip())
        self.voice_info_var.set(GENDER_IT.get(info[0], "") if info else "")

    # -------------------------------------------------------------- scelta dei file
    def browse_pdf(self):
        initial = self.settings.get("pdf_dir") or str(Path.home())
        path = filedialog.askopenfilename(title="Scegli il PDF", initialdir=initial,
                                          filetypes=[("PDF", "*.pdf"), ("Tutti i file", "*.*")])
        if not path:
            return
        p = Path(path)
        self.pdf_var.set(str(p))
        self.settings["pdf_dir"] = str(p.parent)
        if not self.outdir_var.get().strip() or self.outdir_var.get() == self._auto_outdir:
            self._auto_outdir = str(p.with_name(p.stem + "_audiolibro"))
            self.outdir_var.set(self._auto_outdir)
        self.status_var.set("PDF scelto. Ora puoi guardare l'anteprima del testo o avviare la conversione.")

    def browse_outdir(self):
        initial = self.outdir_var.get().strip() or self.settings.get("pdf_dir") or str(Path.home())
        d = filedialog.askdirectory(title="Scegli la cartella di destinazione", initialdir=initial)
        if d:
            self.outdir_var.set(d)

    # ----------------------------------------------------------------- il lavoro
    def collect_job(self):
        outdir = self.outdir_var.get().strip()
        return core.Job(
            pdf=self.pdf_var.get().strip(),
            outdir=Path(outdir) if outdir else None,
            voice=self.voice_var.get().strip() or core.DEFAULT_VOICE,
            rate=fmt_rate(to_int(self.rate_var, 0)),
            pitch=fmt_pitch(to_int(self.pitch_var, 0)),
            pause_paragraph=to_int(self.pp_var, 600),
            pause_heading=to_int(self.ph_var, 1200),
            pages=self.pages_var.get().strip() or None,
            mode=self.mode_var.get(),
            pages_per_file=to_int(self.ppf_var, 20),
            concurrency=3,
            keep_chunks=bool(self.keep_var.get()),
        )

    def _log(self, msg):
        self.q.put(("log", None, msg))

    def _progress(self, done, total, label):
        self.q.put(("progress", None, (done, total, label)))

    def run_task(self, kind, fn, status):
        if self.busy:
            return
        self.set_busy(True, kind)
        self.status_var.set(status)
        if kind == "convert":
            self.pbar.configure(value=0, maximum=100)
        Task(kind, self.q, fn).start()

    def start(self):
        job = self.collect_job()
        self._store_settings()
        self.cancel.clear()
        self.open_btn.configure(state="disabled")

        def work():
            prepared = core.prepare(job, self._log)
            self._log(prepared.summary())
            return core.convert(job, prepared, self._log, self._progress, self.cancel)

        self.run_task("convert", work, "Conversione in corso…")

    def stop(self):
        if self.busy and self.busy_kind == "convert":
            self.cancel.set()
            self.status_var.set("Interruzione in corso…")
            self.stop_btn.configure(state="disabled")

    def preview_text(self):
        job = self.collect_job()

        def work():
            prepared = core.prepare(job, self._log)
            paths = core.write_text_preview(job, prepared, self._log)
            return prepared.summary(), paths

        self.run_task("preview", work, "Preparo l'anteprima del testo…")

    def test_voice(self):
        job = self.collect_job()

        def work():
            text = core.DEFAULT_SAMPLE_TEXT
            if job.pdf:
                try:
                    text = core.sample_text(core.prepare(job, self._log))
                except Exception as e:  # PDF non leggibile ecc.: il provino si fa con una frase d'esempio
                    self._log(f"Uso una frase di esempio (il PDF non e' utilizzabile: {e}).")
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in job.voice)
            out = Path(tempfile.gettempdir()) / f"audiolibro_prova_{safe}.mp3"
            return core.make_voice_sample(job.voice, text, job.rate, job.pitch, out)

        self.run_task("voice", work, "Genero il provino della voce…")

    def load_voices(self):
        Task("voices", self.q, core.list_italian_voices).start()

    def open_folder(self):
        if self.last_outdir:
            self._open(self.last_outdir)

    def _open(self, path):
        try:
            open_path(path)
        except RuntimeError as e:
            messagebox.showwarning("Apertura non riuscita", str(e))

    # --------------------------------------------------------------- stato e coda
    def set_busy(self, flag, kind=None):
        self.busy = flag
        self.busy_kind = kind if flag else None
        state = "disabled" if flag else "normal"
        for w in self.inputs:
            w.configure(state=state)
        for b in (self.start_btn, self.preview_btn, self.voice_btn):
            b.configure(state=state)
        self.stop_btn.configure(state="normal" if (flag and kind == "convert") else "disabled")

    def _append_log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip("\n") + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def drain(self):
        """Elabora tutti i messaggi arrivati dai thread di lavoro."""
        try:
            while True:
                tag, kind, payload = self.q.get_nowait()
                self.handle(tag, kind, payload)
        except queue.Empty:
            pass

    def poll(self):
        self.drain()
        self.root.after(100, self.poll)

    def handle(self, tag, kind, payload):
        if tag == "log":
            self._append_log(payload)
        elif tag == "progress":
            done, total, label = payload
            self.pbar.configure(maximum=max(total, 1), value=done)
            self.status_var.set(f"{label}  ·  {done}/{total} pezzi" if label else f"{done}/{total} pezzi già pronti")
        elif tag == "done":
            self._on_done(kind, payload)
        elif tag == "cancelled":
            self.set_busy(False)
            self.status_var.set("Interrotto. I pezzi già generati sono salvati: premi «Avvia conversione» per riprendere.")
        elif tag == "error":
            if kind == "voices":
                self._append_log("Non ho potuto caricare l'elenco delle voci (serve internet): uso quelle predefinite.")
                return
            self.set_busy(False)
            self.status_var.set("Errore.")
            self._append_log(f"ERRORE: {payload}")
            messagebox.showerror("Errore", payload)

    def _on_done(self, kind, result):
        if kind == "voices":
            self.voices = {name: (gender, friendly) for name, gender, friendly in result}
            if self.voices:
                self.voice_box.configure(values=list(self.voices))
            self._refresh_voice_info()
            return
        self.set_busy(False)
        if kind == "convert":
            self.pbar.configure(value=self.pbar.cget("maximum"))
            self.last_outdir = result[0].parent if result else None
            self.open_btn.configure(state="normal" if self.last_outdir else "disabled")
            self.status_var.set(f"Fatto: {len(result)} file in {self.last_outdir}")
            messagebox.showinfo("Fatto", f"Audiolibro creato: {len(result)} file.\n\nCartella:\n{self.last_outdir}")
        elif kind == "preview":
            summary, paths = result
            self.status_var.set(summary)
            self._append_log(summary)
            self._open(paths[0] if len(paths) == 1 else paths[0].parent)
        elif kind == "voice":
            self.status_var.set("Provino pronto: si è aperto nel lettore audio.")
            self._open(result)

    # ---------------------------------------------------------------- chiusura
    def on_close(self):
        if self.busy and self.busy_kind == "convert":
            if not messagebox.askokcancel("Uscire?", "Una conversione è in corso.\n"
                                          "I pezzi già generati restano salvati e potrai riprendere.\n\nUscire lo stesso?"):
                return
            self.cancel.set()
        self._store_settings()
        self.root.destroy()


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        try:  # schermi ad alta risoluzione: testo nitido invece che sfocato
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    # un eseguibile senza console non ha stdout/stderr: evita errori se qualcosa prova a stampare
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
