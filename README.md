# pdf_to_audiobook

Converte un PDF con testo selezionabile in audiolibro MP3, con le voci neurali
italiane di Microsoft Edge (via `edge-tts`): le più naturali tra le opzioni
gratuite. Serve la connessione a internet.

## Installazione

```
pip install -r requirements.txt
python -m pip install -r requirements.txt
```

Opzionale ma consigliato: **ffmpeg** nel PATH. Se c'è, i pezzi audio vengono
uniti con un file finale "pulito"; se non c'è, vengono uniti byte per byte
(funziona lo stesso).

Se `edge-tts` dà errori strani tipo 403, aggiornalo: `pip install -U edge-tts`
(Microsoft cambia ogni tanto il servizio e la libreria si adegua).

## Interfaccia grafica (il modo più comodo)

Dopo l'installazione:

```
python audiobook_gui.py
```

oppure doppio clic su `Avvia_audiolibro.bat` (apre la finestra senza la console nera).

La finestra ha quattro blocchi:

1. **Libro**: il PDF e la cartella dove salvare (proposta in automatico accanto al PDF).
2. **Voce**: menu delle voci italiane (l'elenco si carica da internet all'avvio), pulsante
   **Prova la voce** che legge un brano del *tuo* libro e lo apre nel lettore audio, cursori di
   velocità e tono.
3. **Pause**: tra i paragrafi e attorno ai titoli.
4. **Cosa convertire**: intervallo di pagine, un file solo / uno per capitolo / uno ogni N pagine,
   e «Conserva i pezzi audio».

Pulsanti: **Anteprima testo** (apre il `.txt` di ciò che verrà letto), **Avvia conversione**,
**Interrompi** (i pezzi già fatti restano salvati: premendo di nuovo Avvia riparte da lì) e
**Apri cartella**.

Flusso consigliato: scegli il PDF → *Prova la voce* (ripeti con velocità e tono diversi) →
*Anteprima testo* (togli copertina e indice con «Pagine») → *Avvia conversione*.

La finestra ricorda le ultime impostazioni (voce, velocità, tono, pause, modalità) in
`.audiolibro_pdf_gui.json` nella tua cartella utente.

Se la finestra non si apre e compare un errore su `tkinter`: su Windows reinstalla Python
lasciando spuntata la voce «tcl/tk and IDLE»; su Linux `sudo apt install python3-tk`.

### Un eseguibile con doppio clic (facoltativo)

Su Windows `build_exe.bat` installa PyInstaller e crea `dist\AudiolibroPDF.exe`, che si apre
senza Python installato. Windows SmartScreen o l'antivirus possono segnalare eseguibili non
firmati creati da te: se succede, usa `Avvia_audiolibro.bat`.

## Da riga di comando: come procedere (consigliato)

**1. Scegli la voce.** Genera un provino con tutte le voci italiane:
```
python pdf_to_audiobook.py libro.pdf --sample
```
Ascolta i file `provino_*.mp3` nella cartella `libro_audiolibro` e scegli.
(`--list-voices` mostra l'elenco aggiornato delle voci disponibili.)

**2. Controlla il testo.** Questo non genera audio: salva in `.txt` esattamente
quello che verrebbe letto (un paragrafo per riga vuota: è lì che ci sarà una pausa), con una stima della durata:
```
python pdf_to_audiobook.py libro.pdf --dry-run
```
Se all'inizio ci sono copertina, indice, ringraziamenti ecc., salta quelle
pagine con `--pages 9-310`.

**3. Genera l'audiolibro.**
```
python pdf_to_audiobook.py libro.pdf --pages 9-310 --voice it-IT-ElsaNeural --mode chapters
```

## Messa a punto della voce e delle pause

La voce sembra "letta da una macchina" soprattutto per due motivi: il ritmo è
troppo uniforme e non ci sono pause tra i paragrafi. Per questo il programma
sintetizza **paragrafo per paragrafo** e inserisce del silenzio vero tra l'uno
e l'altro (600 ms), più lungo attorno ai titoli (1200 ms). Le battute brevi di
dialogo vengono accorpate, per non spezzettare troppo.

Per mettere a punto il risultato su poche pagine, **prima di lanciare tutto il libro**:

```
python pdf_to_audiobook.py libro.pdf --pages 9-14 --keep-chunks --rate -7% --pitch -2Hz
```

- **Le pause** si regolano senza rigenerare la voce (è istantaneo), perché
  `--keep-chunks` conserva i pezzi audio:
  ```
  python pdf_to_audiobook.py libro.pdf --pages 9-14 --keep-chunks --rate -7% --pitch -2Hz --pause-paragraph 900 --pause-heading 1800
  ```
- **Velocità, tono e voce** invece richiedono di rigenerare l'audio (il
  programma se ne accorge e lo fa da solo).
- Punto di partenza che consiglio di provare: `--rate -7% --pitch -2Hz`: una voce un
  filo più lenta e meno "squillante" suona in genere meno sintetica. Sono valori da
  regolare a orecchio, non una regola.
- `--no-pauses` torna al comportamento senza pause aggiuntive.

Avvertenza: con tante richieste piccole l'elaborazione è un po' più lenta
di prima. `--dry-run` ti dice in anticipo quante richieste vocali serviranno.

## Opzioni utili

| Opzione | Cosa fa |
|---|---|
| `--voice NOME` | voce (default `it-IT-IsabellaNeural`) |
| `--rate -5%` | più lenta (`+10%` più veloce) |
| `--pitch -2Hz` | tono più grave/acuto |
| `--pause-paragraph 600` | pausa tra paragrafi, in millisecondi |
| `--pause-heading 1200` | pausa prima e dopo i titoli |
| `--no-pauses` | nessuna pausa aggiuntiva |
| `--keep-chunks` | conserva i pezzi audio (per ritoccare le pause senza rigenerare) |
| `--pages 9-310` | solo queste pagine (anche `9-` o `-80`) |
| `--mode single` | un unico MP3 (default) |
| `--mode chapters` | un MP3 per capitolo, usando i segnalibri del PDF |
| `--mode pages --pages-per-file 25` | un MP3 ogni 25 pagine |
| `--outdir CARTELLA` | dove salvare (default `<nomepdf>_audiolibro`) |
| `--concurrency 3` | richieste in parallelo (alzalo con cautela) |

## Se si interrompe

Rilancia **lo stesso identico comando**: riparte dai pezzi già generati. Se
cambi voce, velocità o pagine, rigenera tutto da zero (per non mischiare voci
diverse nello stesso libro).

## Cosa fa al testo prima di leggerlo

- toglie intestazioni, piè di pagina e numeri di pagina ripetuti
- ricuce le parole spezzate a fine riga ("ulti-\nma" → "ultima")
- ricompone i paragrafi spezzati da un cambio pagina
- riconosce i titoli e li legge con una pausa prima e dopo
- tratta una battuta di dialogo (— / « / “) come l'inizio di un nuovo paragrafo
- sostituisce gli indirizzi web con la parola "link"

Con **PyMuPDF** (installato dai requisiti) l'estrazione distingue bene i
paragrafi; senza, il programma ripiega su `pypdf`, un po' meno preciso.

## Limiti

- Funziona solo con PDF di **testo**. Una scansione (immagini) richiede prima un OCR.
- Note a piè di pagina, tabelle, formule e didascalie vengono lette come testo
  normale, quindi in alcuni libri potresti volerle togliere a mano: per questo
  c'è `--dry-run`.
- Libri a due colonne: l'ordine di lettura dipende da come è stato costruito il
  PDF. Controlla sempre il `.txt` prima di lanciare una conversione lunga.
- Qualche ora di audio richiede qualche decina di minuti di elaborazione.
