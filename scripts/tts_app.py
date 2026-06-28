#!/usr/bin/env python3
"""
Kokoro TTS — EPUB to Audiobook
Native UI Application
"""

import subprocess
import sys
import os

# ── Auto-install dependencies ──────────────────────────────────────────────────
def _ensure(pkg, import_as=None):
    try:
        __import__(import_as or pkg.replace("-", "_"))
    except ImportError:
        print(f"Installing {pkg}…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

for _p, _i in [
    ("customtkinter", None),
    ("kokoro", None),
    ("soundfile", None),
    ("ebooklib", None),
    ("beautifulsoup4", "bs4"),
    ("numpy", None),
]:
    _ensure(_p, _i)

# ── Imports ────────────────────────────────────────────────────────────────────
import tkinter as tk
from tkinter import filedialog, messagebox
import threading
import queue
import re
from pathlib import Path

import customtkinter as ctk

ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")

# ── Voice catalogue ────────────────────────────────────────────────────────────
VOICES = {
    "American Female": ["af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky"],
    "American Male":   ["am_adam",  "am_echo",  "am_eric",  "am_fenrir",
                        "am_liam",  "am_michael", "am_onyx"],
    "British Female":  ["bf_alice", "bf_emma",  "bf_isabella", "bf_lily"],
    "British Male":    ["bm_daniel","bm_fable", "bm_george",   "bm_lewis"],
}
ALL_VOICES = [v for group in VOICES.values() for v in group]

VOICE_CATEGORY = {}
for cat, vs in VOICES.items():
    for v in vs:
        VOICE_CATEGORY[v] = cat

# ── Language catalogue ─────────────────────────────────────────────────────────
LANGUAGES = [
    ("a",  "American English (auto)"),
    ("b",  "British English"),
    ("e",  "Spanish"),
    ("f",  "French"),
    ("h",  "Hindi"),
    ("i",  "Italian"),
    ("j",  "Japanese"),
    ("p",  "Brazilian Portuguese"),
    ("z",  "Chinese (Mandarin)"),
    ("n",  "Korean"),
]
LANG_DISPLAY  = [f"{code}  —  {name}" for code, name in LANGUAGES]
LANG_CODE_MAP = {f"{code}  —  {name}": code for code, name in LANGUAGES}


# ══════════════════════════════════════════════════════════════════════════════
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Kokoro TTS — EPUB to Audiobook")
        self.geometry("1000x720")
        self.minsize(820, 580)

        self._processing   = False
        self._stop_flag    = False
        self._log_queue: queue.Queue = queue.Queue()

        self._build_ui()
        self._poll()

    # ── Layout ─────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Left panel
        self._left = ctk.CTkScrollableFrame(self, width=300, corner_radius=0)
        self._left.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)
        self._left.grid_columnconfigure(0, weight=1)

        # Right panel
        right = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        self._fill_left()
        self._fill_right(right)

    # ── Left panel ─────────────────────────────────────────────────────────────
    def _fill_left(self):
        p = self._left
        r = 0

        def H(text, pady=(12, 2)):
            nonlocal r
            ctk.CTkLabel(p, text=text,
                         font=ctk.CTkFont(size=12, weight="bold")).grid(
                row=r, column=0, sticky="w", pady=pady)
            r += 1

        def sep():
            nonlocal r
            ctk.CTkFrame(p, height=1, fg_color="gray30").grid(
                row=r, column=0, sticky="ew", pady=(8, 4))
            r += 1

        # ── Title ──────────────────────────────────────────────────────────────
        ctk.CTkLabel(p, text="Kokoro TTS",
                     font=ctk.CTkFont(size=22, weight="bold")).grid(
            row=r, column=0, pady=(6, 0), sticky="w"); r += 1
        ctk.CTkLabel(p, text="EPUB → Audiobook Converter",
                     font=ctk.CTkFont(size=12), text_color="gray").grid(
            row=r, column=0, pady=(0, 8), sticky="w"); r += 1
        sep()

        # ── Input file ─────────────────────────────────────────────────────────
        H("Input EPUB File", pady=(4, 2))
        self._epub_var = ctk.StringVar(value="No file selected")
        _row = ctk.CTkFrame(p, fg_color="transparent")
        _row.grid(row=r, column=0, sticky="ew"); r += 1
        _row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(_row, textvariable=self._epub_var,
                     anchor="w", wraplength=210,
                     font=ctk.CTkFont(size=11)).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(_row, text="Browse…", width=72,
                      command=self._pick_epub).grid(row=0, column=1, padx=(4, 0))

        # ── Output dir ─────────────────────────────────────────────────────────
        H("Output Directory")
        self._outdir_var = ctk.StringVar(value="audiobook_output")
        _row2 = ctk.CTkFrame(p, fg_color="transparent")
        _row2.grid(row=r, column=0, sticky="ew"); r += 1
        _row2.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(_row2, textvariable=self._outdir_var,
                     anchor="w", wraplength=210,
                     font=ctk.CTkFont(size=11)).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(_row2, text="Browse…", width=72,
                      command=self._pick_outdir).grid(row=0, column=1, padx=(4, 0))
        sep()

        # ── Voice ──────────────────────────────────────────────────────────────
        H("Voice", pady=(4, 2))
        self._voice_var = ctk.StringVar(value="af_heart")
        ctk.CTkOptionMenu(p, values=ALL_VOICES,
                          variable=self._voice_var,
                          dynamic_resizing=False,
                          command=self._on_voice_change).grid(
            row=r, column=0, sticky="ew"); r += 1
        self._voice_cat_var = ctk.StringVar(value="American Female")
        ctk.CTkLabel(p, textvariable=self._voice_cat_var,
                     text_color="gray", font=ctk.CTkFont(size=11)).grid(
            row=r, column=0, sticky="w", pady=(1, 0)); r += 1

        # ── Language ───────────────────────────────────────────────────────────
        H("Language")
        self._lang_var = ctk.StringVar(value=LANG_DISPLAY[0])
        ctk.CTkOptionMenu(p, values=LANG_DISPLAY,
                          variable=self._lang_var,
                          dynamic_resizing=False).grid(
            row=r, column=0, sticky="ew"); r += 1
        sep()

        # ── Speed ──────────────────────────────────────────────────────────────
        H("Speed", pady=(4, 2))
        speed_row = ctk.CTkFrame(p, fg_color="transparent")
        speed_row.grid(row=r, column=0, sticky="ew"); r += 1
        speed_row.grid_columnconfigure(0, weight=1)

        self._speed_var = ctk.DoubleVar(value=1.0)
        ctk.CTkSlider(speed_row, from_=0.5, to=2.5, number_of_steps=40,
                      variable=self._speed_var,
                      command=self._on_speed).grid(
            row=0, column=0, sticky="ew", padx=(0, 6))
        self._speed_lbl = ctk.StringVar(value="1.00×")
        ctk.CTkLabel(speed_row, textvariable=self._speed_lbl,
                     width=42, font=ctk.CTkFont(size=12)).grid(row=0, column=1)

        preset_row = ctk.CTkFrame(p, fg_color="transparent")
        preset_row.grid(row=r, column=0, sticky="ew", pady=(4, 0)); r += 1
        for i, (lbl, val) in enumerate([("0.75×", 0.75), ("1.0×", 1.0),
                                         ("1.25×", 1.25), ("1.5×", 1.5),
                                         ("2.0×", 2.0)]):
            ctk.CTkButton(preset_row, text=lbl, width=50, height=22,
                          font=ctk.CTkFont(size=11),
                          command=lambda v=val: self._set_speed(v)).grid(
                row=0, column=i, padx=1)
        sep()

        # ── Advanced ───────────────────────────────────────────────────────────
        H("Advanced Settings", pady=(4, 2))

        # Device
        ctk.CTkLabel(p, text="Processing Device",
                     font=ctk.CTkFont(size=12)).grid(
            row=r, column=0, sticky="w", pady=(2, 1)); r += 1
        self._device_var = ctk.StringVar(value="auto  —  Auto (GPU if available)")
        _devices = [
            "auto  —  Auto (GPU if available)",
            "cpu   —  CPU Only",
            "cuda  —  CUDA GPU",
        ]
        ctk.CTkOptionMenu(p, values=_devices,
                          variable=self._device_var,
                          dynamic_resizing=False).grid(
            row=r, column=0, sticky="ew", pady=(0, 6)); r += 1

        # Transformer G2P
        self._trf_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(p, text="Transformer G2P  (better quality, slower)",
                        variable=self._trf_var,
                        font=ctk.CTkFont(size=12)).grid(
            row=r, column=0, sticky="w", pady=(2, 2)); r += 1

        # Merge
        self._merge_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(p, text="Merge chapters into one file",
                        variable=self._merge_var,
                        font=ctk.CTkFont(size=12)).grid(
            row=r, column=0, sticky="w", pady=(2, 8)); r += 1

        # Numeric fields
        def num_field(label, var_name, default, pady=(2, 0)):
            nonlocal r
            ctk.CTkLabel(p, text=label,
                         font=ctk.CTkFont(size=12)).grid(
                row=r, column=0, sticky="w", pady=pady); r += 1
            sv = ctk.StringVar(value=str(default))
            setattr(self, var_name, sv)
            ctk.CTkEntry(p, textvariable=sv, height=30).grid(
                row=r, column=0, sticky="ew", pady=(0, 6)); r += 1

        num_field("Max Chunk Size (chars)",           "_chunk_var",   500)
        num_field("Silence Between Chapters (secs)",  "_silence_var", 1.0)
        num_field("Min Chapter Length (chars)",       "_minch_var",   200)
        sep()

        # ── Start / Stop ───────────────────────────────────────────────────────
        self._start_btn = ctk.CTkButton(
            p, text="Start Converting", height=44,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color="#2d7d46", hover_color="#245f37",
            command=self._toggle)
        self._start_btn.grid(row=r, column=0, sticky="ew", pady=(0, 8)); r += 1

    # ── Right panel ────────────────────────────────────────────────────────────
    def _fill_right(self, parent):
        # Progress area
        top = ctk.CTkFrame(parent, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=2, pady=(2, 4))
        top.grid_columnconfigure(0, weight=1)

        self._status_var = ctk.StringVar(value="Ready")
        ctk.CTkLabel(top, textvariable=self._status_var,
                     font=ctk.CTkFont(size=13, weight="bold"),
                     anchor="w").grid(row=0, column=0, sticky="w")

        self._prog_var = ctk.DoubleVar(value=0)
        ctk.CTkProgressBar(top, variable=self._prog_var,
                           mode="determinate").grid(
            row=1, column=0, sticky="ew", pady=(4, 2))

        self._prog_lbl_var = ctk.StringVar(value="")
        ctk.CTkLabel(top, textvariable=self._prog_lbl_var,
                     font=ctk.CTkFont(size=11), text_color="gray",
                     anchor="w").grid(row=2, column=0, sticky="w")

        # Log
        self._log_box = ctk.CTkTextbox(
            parent, wrap="word",
            font=ctk.CTkFont(family="Menlo", size=12))
        self._log_box.grid(row=1, column=0, sticky="nsew", padx=2, pady=(0, 4))

        # Bottom toolbar
        bot = ctk.CTkFrame(parent, fg_color="transparent")
        bot.grid(row=2, column=0, sticky="ew", padx=2)

        ctk.CTkButton(bot, text="Clear Log", width=90, height=28,
                      font=ctk.CTkFont(size=12),
                      command=lambda: self._log_box.delete("1.0", "end")
                      ).pack(side="left", padx=(0, 4))
        ctk.CTkButton(bot, text="Open Output Folder", width=145, height=28,
                      font=ctk.CTkFont(size=12),
                      command=self._open_output).pack(side="left")

        ctk.CTkLabel(bot, text="Theme:",
                     font=ctk.CTkFont(size=12)).pack(side="right", padx=(4, 2))
        ctk.CTkOptionMenu(bot, values=["System", "Light", "Dark"],
                          width=90, font=ctk.CTkFont(size=12),
                          command=lambda v: ctk.set_appearance_mode(v.lower())
                          ).pack(side="right")

    # ── Helpers ────────────────────────────────────────────────────────────────
    def _pick_epub(self):
        path = filedialog.askopenfilename(
            title="Select EPUB File",
            filetypes=[("EPUB Files", "*.epub"), ("All Files", "*.*")])
        if path:
            self._epub_var.set(path)
            stem = Path(path).stem
            self._outdir_var.set(str(Path(path).parent / f"{stem}_audiobook"))

    def _pick_outdir(self):
        path = filedialog.askdirectory(title="Select Output Directory")
        if path:
            self._outdir_var.set(path)

    def _on_voice_change(self, v):
        self._voice_cat_var.set(VOICE_CATEGORY.get(v, ""))

    def _on_speed(self, v):
        self._speed_lbl.set(f"{float(v):.2f}×")

    def _set_speed(self, v):
        self._speed_var.set(v)
        self._speed_lbl.set(f"{v:.2f}×")

    def _open_output(self):
        d = self._outdir_var.get()
        if os.path.isdir(d):
            subprocess.run(["open", d])
        else:
            messagebox.showinfo("Info", "Output folder does not exist yet.")

    # ── Start / Stop ───────────────────────────────────────────────────────────
    def _toggle(self):
        if self._processing:
            self._stop_flag = True
            self._start_btn.configure(text="Stopping…", state="disabled")
        else:
            self._start()

    def _start(self):
        epub = self._epub_var.get()
        if epub == "No file selected" or not os.path.isfile(epub):
            messagebox.showerror("Error", "Please select a valid EPUB file.")
            return

        try:
            chunk_size   = int(self._chunk_var.get())
            silence_secs = float(self._silence_var.get())
            min_ch_len   = int(self._minch_var.get())
        except ValueError as e:
            messagebox.showerror("Error", f"Invalid numeric setting: {e}")
            return

        self._processing = True
        self._stop_flag  = False
        self._start_btn.configure(text="Stop", state="normal",
                                   fg_color="#c0392b", hover_color="#a93226")
        self._prog_var.set(0)
        self._status_var.set("Starting…")

        device_raw = self._device_var.get().split("  —  ")[0].strip()
        device = None if device_raw == "auto" else device_raw

        settings = {
            "epub":        epub,
            "outdir":      self._outdir_var.get(),
            "voice":       self._voice_var.get(),
            "lang_code":   LANG_CODE_MAP[self._lang_var.get()],
            "speed":       self._speed_var.get(),
            "device":      device,
            "trf":         self._trf_var.get(),
            "merge":       self._merge_var.get(),
            "chunk_size":  chunk_size,
            "silence":     silence_secs,
            "min_ch_len":  min_ch_len,
        }
        threading.Thread(target=self._worker, args=(settings,), daemon=True).start()

    # ── Background worker ──────────────────────────────────────────────────────
    def _worker(self, s):
        def log(msg):        self._log_queue.put(("log",      msg))
        def status(msg):     self._log_queue.put(("status",   msg))
        def prog(v, lbl=""):  self._log_queue.put(("progress", (v, lbl)))

        try:
            from kokoro import KPipeline
            import ebooklib
            from ebooklib import epub
            from bs4 import BeautifulSoup
            import soundfile as sf
            import numpy as np

            RATE = 24000

            # ── Load model ────────────────────────────────────────────────────
            status("Loading Kokoro TTS model…")
            log("Initializing Kokoro TTS pipeline…")
            pipeline = KPipeline(
                lang_code=s["lang_code"],
                trf=s["trf"],
                device=s["device"],
            )
            log(f"Model ready  |  voice={s['voice']}  speed={s['speed']:.2f}×  "
                f"lang={s['lang_code']}\n")

            # ── Read EPUB ─────────────────────────────────────────────────────
            status("Reading EPUB…")
            log(f"Reading: {s['epub']}")
            book = epub.read_epub(s["epub"])

            chapters = []
            for item in book.get_items():
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    soup = BeautifulSoup(item.get_content(), "html.parser")
                    for tag in soup(["script", "style", "head"]):
                        tag.decompose()
                    text = re.sub(r'\s+', ' ', soup.get_text(separator=' ')).strip()
                    if len(text) >= s["min_ch_len"]:
                        title_tag = soup.find(["h1", "h2", "h3"])
                        title = (title_tag.get_text().strip()
                                 if title_tag
                                 else f"Section {len(chapters)+1}")
                        chapters.append((title, text))

            if not chapters:
                log("No chapters found — check the EPUB file.")
                self._log_queue.put(("done", False))
                return

            log(f"Found {len(chapters)} chapters\n")
            os.makedirs(s["outdir"], exist_ok=True)
            book_stem = Path(s["epub"]).stem
            all_audio = []
            silence   = np.zeros(int(RATE * s["silence"]), dtype=np.float32)

            # ── Process chapters ──────────────────────────────────────────────
            for ch_i, (title, text) in enumerate(chapters):
                if self._stop_flag:
                    log("\nStopped by user.")
                    self._log_queue.put(("done", False))
                    return

                ch_frac = ch_i / len(chapters)
                prog(ch_frac, f"Chapter {ch_i+1}/{len(chapters)}")
                status(f"Chapter {ch_i+1}/{len(chapters)}: {title[:50]}")
                log(f"── Chapter {ch_i+1}/{len(chapters)}: {title}")
                log(f"   {len(text):,} chars")

                # Chunk at sentence boundaries
                sentences = re.split(r'(?<=[.!?])\s+', text)
                chunks, cur = [], ""
                for sent in sentences:
                    if len(cur) + len(sent) + 1 <= s["chunk_size"]:
                        cur = (cur + " " + sent).strip()
                    else:
                        if cur:
                            chunks.append(cur)
                        cur = sent
                if cur:
                    chunks.append(cur)
                log(f"   {len(chunks)} chunks")

                ch_audio = []
                for c_i, chunk in enumerate(chunks):
                    if self._stop_flag:
                        break
                    try:
                        for _, _, audio in pipeline(chunk,
                                                     voice=s["voice"],
                                                     speed=s["speed"]):
                            ch_audio.append(audio)
                    except Exception as e:
                        log(f"   ! Chunk {c_i+1} skipped: {e}")
                        continue

                    inner = ch_frac + (c_i + 1) / len(chunks) / len(chapters)
                    prog(inner,
                         f"Ch {ch_i+1}/{len(chapters)}  "
                         f"chunk {c_i+1}/{len(chunks)}")

                if not ch_audio:
                    log("   (no audio generated)")
                    continue

                combined = np.concatenate(ch_audio)
                safe_title = re.sub(r'[^\w\s-]', '', title)[:35].strip()
                fname = f"{book_stem}_ch{ch_i+1:02d}_{safe_title}.wav"
                fpath = os.path.join(s["outdir"], fname)
                sf.write(fpath, combined, RATE)
                dur_s = len(combined) / RATE
                log(f"   Saved: {fname}  ({dur_s:.1f} s)\n")

                if s["merge"]:
                    all_audio.append(combined)
                    if ch_i < len(chapters) - 1:
                        all_audio.append(silence)

            # ── Merge ─────────────────────────────────────────────────────────
            if s["merge"] and all_audio and not self._stop_flag:
                status("Merging chapters…")
                log(f"Merging {len(chapters)} chapters…")
                full   = np.concatenate(all_audio)
                fpath2 = os.path.join(s["outdir"], f"{book_stem}_FULL.wav")
                sf.write(fpath2, full, RATE)
                mins = len(full) / RATE / 60
                log(f"Full audiobook: {fpath2}")
                log(f"Total duration: {mins:.1f} min")

            log(f"\nDone!  Output: {s['outdir']}")
            self._log_queue.put(("done", True))

        except Exception as exc:
            import traceback
            log(f"\nError: {exc}")
            log(traceback.format_exc())
            self._log_queue.put(("done", False))

    # ── UI update loop ─────────────────────────────────────────────────────────
    def _poll(self):
        try:
            while True:
                kind, data = self._log_queue.get_nowait()
                if kind == "log":
                    self._log_box.insert("end", data + "\n")
                    self._log_box.see("end")
                elif kind == "status":
                    self._status_var.set(data)
                elif kind == "progress":
                    v, lbl = data
                    self._prog_var.set(max(0.0, min(1.0, v)))
                    self._prog_lbl_var.set(lbl)
                elif kind == "done":
                    self._on_done(data)
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _on_done(self, ok: bool):
        self._processing = False
        self._start_btn.configure(
            text="Start Converting", state="normal",
            fg_color="#2d7d46", hover_color="#245f37")
        if ok:
            self._status_var.set("Done!")
            self._prog_var.set(1.0)
            messagebox.showinfo(
                "Done",
                f"Audiobook created!\n\nOutput folder:\n{self._outdir_var.get()}")
        elif self._stop_flag:
            self._status_var.set("Stopped")
        else:
            self._status_var.set("Error — see log")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    App().mainloop()
