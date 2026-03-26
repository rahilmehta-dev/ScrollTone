#!/usr/bin/env python3
"""
ScrollTone — EPUB to Audiobook
FastAPI + SSE backend
"""

import asyncio
import json
import os
import queue as thread_queue
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

BASE_DIR    = Path(__file__).parent
UPLOAD_DIR  = Path("/tmp/tts_uploads")
OUTPUT_DIR  = Path("/tmp/tts_outputs")
PREVIEW_DIR = Path("/app/previews")          # pre-baked at build time
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

app       = FastAPI(title="ScrollTone")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
jobs: Dict[str, dict] = {}

# ── Preview pipeline (lazy, shared, protected by lock) ────────────────────────
_preview_pipeline: dict = {}   # lang -> KPipeline
_preview_lock = threading.Lock()

KNOWN_VOICES = {
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky",
    "am_adam",  "am_echo",  "am_eric",   "am_fenrir",
    "am_liam",  "am_michael", "am_onyx",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
}

PREVIEW_TEXT = (
    "Hello! I'll be your narrator for this audiobook. "
    "Whether the story is long or short, I'm here to bring every page to life."
)


# ── EPUB cover finder ─────────────────────────────────────────────────────────

def _find_epub_cover(book):
    """Return (cover_bytes, mime_type) using 4 fallback strategies.

    1. EPUB2 OPF  : <meta name="cover" content="item-id"/>
    2. EPUB3 OPF  : manifest item with properties="cover-image"
    3. Name/ID    : any image whose file name or id contains "cover"
    4. First image: first image found anywhere in the book
    """
    import ebooklib

    # ── Strategy 1: EPUB2 OPF meta name="cover" ──────────────────────────────
    cover_id = None
    try:
        meta = book.get_metadata('OPF', 'cover')
        if meta:
            cover_id = (meta[0][1] or {}).get('content', '')
    except Exception:
        pass

    if cover_id:
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_IMAGE and item.get_id() == cover_id:
                data = item.get_content()
                if data:
                    return data, item.media_type or "image/jpeg"

    # ── Strategy 2: EPUB3 manifest properties="cover-image" ──────────────────
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE:
            props = getattr(item, 'properties', '') or ''
            if isinstance(props, (list, tuple)):
                props = ' '.join(props)
            if 'cover-image' in props:
                data = item.get_content()
                if data:
                    return data, item.media_type or "image/jpeg"

    # ── Strategy 3: "cover" in file name or item id ───────────────────────────
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE:
            name = (getattr(item, 'file_name', '') or item.get_name() or '').lower()
            iid  = (item.get_id() or '').lower()
            if 'cover' in name or 'cover' in iid:
                data = item.get_content()
                if data:
                    return data, item.media_type or "image/jpeg"

    # ── Strategy 4: first image in the book ───────────────────────────────────
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE:
            data = item.get_content()
            if data:
                return data, item.media_type or "image/jpeg"

    return None, "image/jpeg"


# ── MP3 conversion helper ─────────────────────────────────────────────────────

def _to_mp3(wav_path: str, mp3_path: str, bitrate: int, *,
            title: str = "", album: str = "", artist: str = "",
            track: int = 0, cover_data: bytes = None,
            cover_mime: str = "image/jpeg"):
    """Convert WAV → MP3 and embed ID3 metadata/cover art."""
    from pydub import AudioSegment
    from mutagen.id3 import (ID3, TIT2, TPE1, TALB, TRCK, APIC, TCON,
                              ID3NoHeaderError)

    seg = AudioSegment.from_wav(wav_path)
    seg.export(mp3_path, format="mp3", bitrate=f"{bitrate}k")

    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()

    if title:     tags["TIT2"] = TIT2(encoding=3, text=title)
    if artist:    tags["TPE1"] = TPE1(encoding=3, text=artist)
    if album:     tags["TALB"] = TALB(encoding=3, text=album)
    if track > 0: tags["TRCK"] = TRCK(encoding=3, text=str(track))
    tags["TCON"] = TCON(encoding=3, text="Audiobook")
    if cover_data:
        tags["APIC"] = APIC(encoding=3, mime=cover_mime, type=3,
                            desc="Cover", data=cover_data)
    tags.save(mp3_path)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return (BASE_DIR / "templates" / "index.html").read_text()


@app.get("/preview/{voice}")
async def preview_voice(voice: str):
    """Return a short WAV sample for the requested voice.
    Served from the pre-baked /app/previews/ directory.
    Falls back to on-demand generation if the file is missing."""
    if voice not in KNOWN_VOICES:
        raise HTTPException(400, f"Unknown voice: {voice}")

    cache = PREVIEW_DIR / f"{voice}.wav"
    if not cache.exists():
        # Generate on demand (first request only, ~10 s)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _generate_preview, voice, cache)

    if not cache.exists():
        raise HTTPException(500, "Preview generation failed")

    return FileResponse(str(cache), media_type="audio/wav",
                        headers={"Cache-Control": "public, max-age=86400"})


def _generate_preview(voice: str, out_path: Path):
    lang = "b" if voice[:2] in ("bf", "bm") else "a"
    with _preview_lock:
        if lang not in _preview_pipeline:
            from kokoro import KPipeline
            _preview_pipeline[lang] = KPipeline(
                lang_code=lang, repo_id="hexgrad/Kokoro-82M")
        pipeline = _preview_pipeline[lang]
        try:
            import numpy as np
            import soundfile as sf
            chunks = [a for _, _, a in pipeline(PREVIEW_TEXT, voice=voice, speed=1.0)]
            if chunks:
                sf.write(str(out_path), np.concatenate(chunks), 24000)
        except Exception as e:
            print(f"Preview generation failed for {voice}: {e}", flush=True)


@app.post("/convert")
async def convert(
    file:        UploadFile = File(...),
    voice:       str   = Form("af_heart"),
    lang_code:   str   = Form("a"),
    speed:       float = Form(1.0),
    device:      str   = Form("auto"),
    trf:         str   = Form("false"),
    merge:         str   = Form("true"),
    chunk_size:    int   = Form(500),
    silence:       float = Form(1.0),
    min_ch_len:    int   = Form(200),
    num_workers:   int   = Form(1),
    output_format: str   = Form("wav"),
    bitrate:       int   = Form(192),
):
    job_id = str(uuid.uuid4())

    # Persist uploaded file
    up_dir = UPLOAD_DIR / job_id
    up_dir.mkdir(parents=True, exist_ok=True)
    up_path = up_dir / file.filename
    up_path.write_bytes(await file.read())

    out_dir = OUTPUT_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

    loop        = asyncio.get_running_loop()
    async_queue: asyncio.Queue = asyncio.Queue()
    stop_event  = threading.Event()

    job = {
        "id":         job_id,
        "status":     "running",
        "queue":      async_queue,
        "stop_event": stop_event,
        "out_dir":    str(out_dir),
        "files":      [],
    }
    jobs[job_id] = job

    resolved_device = None if device == "auto" else device
    # GPU serializes inference anyway — force 1 worker to avoid VRAM waste
    eff_workers = 1 if resolved_device == "cuda" else max(1, min(num_workers, 4))

    settings = {
        "epub":        str(up_path),
        "filename":    file.filename,
        "out_dir":     str(out_dir),
        "voice":       voice,
        "lang_code":   lang_code,
        "speed":       speed,
        "device":      resolved_device,
        "trf":         trf.lower() == "true",
        "merge":       merge.lower() == "true",
        "chunk_size":  chunk_size,
        "silence":     silence,
        "min_ch_len":  min_ch_len,
        "num_workers":   eff_workers,
        "output_format": output_format.lower(),
        "bitrate":       bitrate,
    }

    threading.Thread(
        target=_worker, args=(job, settings, loop), daemon=True
    ).start()

    return {"job_id": job_id}


@app.get("/stream/{job_id}")
async def stream(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    async def generator():
        q: asyncio.Queue = job["queue"]
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30)
                    if msg is None:
                        yield (
                            "data: "
                            + json.dumps({"type": "done", "files": job["files"]})
                            + "\n\n"
                        )
                        return
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield 'data: {"type":"ping"}\n\n'
        except GeneratorExit:
            pass

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/stop/{job_id}")
async def stop_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job["stop_event"].set()
    return {"status": "stopping"}


@app.get("/download/{job_id}/{filename}")
async def download(job_id: str, filename: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    path = Path(job["out_dir"]) / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    media_type = "audio/mpeg" if filename.endswith(".mp3") else "audio/wav"
    return FileResponse(str(path), filename=filename, media_type=media_type)


# ── Background worker ─────────────────────────────────────────────────────────

def _worker(job: dict, s: dict, loop: asyncio.AbstractEventLoop):

    def _push(data: dict):
        loop.call_soon_threadsafe(job["queue"].put_nowait, json.dumps(data))

    def log(msg: str):      _push({"type": "log",      "msg": msg})
    def status(msg: str):   _push({"type": "status",   "msg": msg})
    def prog(v, lbl=""):    _push({"type": "progress", "value": v, "label": lbl})
    def done():             loop.call_soon_threadsafe(job["queue"].put_nowait, None)

    try:
        import psutil, os as _os
        from kokoro import KPipeline
        import ebooklib
        from ebooklib import epub
        from bs4 import BeautifulSoup
        import soundfile as sf
        import numpy as np

        _proc = psutil.Process(_os.getpid())

        def memlog(label: str = ""):
            rss   = _proc.memory_info().rss / 1024**3
            vm    = psutil.virtual_memory()
            used  = vm.used  / 1024**3
            total = vm.total / 1024**3
            avail = vm.available / 1024**3
            msg   = (f"[MEM] {label}  "
                     f"process={rss:.2f}GB  "
                     f"system={used:.2f}/{total:.2f}GB  "
                     f"avail={avail:.2f}GB")
            log(msg)
            print(msg, flush=True)   # also appears in docker logs

        RATE     = 24000
        num_w    = s["num_workers"]

        memlog("startup")

        # ── Auto-cap workers based on available RAM ───────────────────────────
        # Each KPipeline ≈ 0.9 GB; reserve 2 GB for OS + inference headroom.
        avail_gb   = psutil.virtual_memory().available / 1024 ** 3
        max_safe   = max(1, int((avail_gb - 2.0) / 0.9))
        if num_w > max_safe:
            log(f"[MEM] Reducing workers {num_w} → {max_safe} "
                f"(only {avail_gb:.1f} GB available; need ~{max_safe*0.9+2:.1f} GB for {max_safe})")
            num_w = max_safe

        # ── Load pipeline pool ────────────────────────────────────────────────
        status(f"Loading {num_w} pipeline instance(s)…")
        log(f"Initializing {num_w} pipeline(s)  lang={s['lang_code']}  trf={s['trf']}")
        pool: thread_queue.Queue = thread_queue.Queue()
        for w in range(num_w):
            pool.put(KPipeline(lang_code=s["lang_code"], repo_id="hexgrad/Kokoro-82M",
                           trf=s["trf"], device=s["device"]))
            memlog(f"after pipeline {w+1}/{num_w} loaded")
        log(f"Model(s) ready  |  voice={s['voice']}  speed={s['speed']:.2f}×\n")

        # ── Read EPUB ─────────────────────────────────────────────────────────
        status("Reading EPUB…")
        log(f"Reading: {s['filename']}")
        book = epub.read_epub(s["epub"])

        # Extract metadata for MP3 tagging
        meta_t = book.get_metadata('DC', 'title')
        meta_c = book.get_metadata('DC', 'creator')
        s["book_title_meta"]  = meta_t[0][0] if meta_t else ""
        s["book_author_meta"] = meta_c[0][0] if meta_c else ""
        s["cover_data"], s["cover_mime"] = _find_epub_cover(book)
        if s["cover_data"]:
            log(f"Cover image found ({len(s['cover_data'])//1024} KB, {s['cover_mime']})")
        else:
            log("No cover image found in EPUB")

        chapters = []
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                soup = BeautifulSoup(item.get_content(), "html.parser")
                for tag in soup(["script", "style", "head"]):
                    tag.decompose()
                text = re.sub(r"\s+", " ", soup.get_text(separator=" ")).strip()
                if len(text) >= s["min_ch_len"]:
                    title_tag = soup.find(["h1", "h2", "h3"])
                    title = (
                        title_tag.get_text().strip()
                        if title_tag
                        else f"Section {len(chapters)+1}"
                    )
                    # "1." / "2" / "42." → "Chapter 1" / "Chapter 2" / "Chapter 42"
                    if re.match(r'^\d+\.?$', title.strip()):
                        title = "Chapter " + title.strip().rstrip(".")
                    chapters.append((title, text))

        if not chapters:
            log("No chapters found in EPUB.")
            job["status"] = "error"
            done(); return

        log(f"Found {len(chapters)} chapters\n")

        book_stem   = re.sub(r"[^\w\s-]", "", Path(s["filename"]).stem)[:50]
        silence_arr = np.zeros(int(RATE * s["silence"]), dtype=np.float32)

        # ── Per-chapter worker (runs inside ThreadPoolExecutor) ───────────────
        done_lock  = threading.Lock()
        done_count = [0]

        def process_chapter(ch_i, title, text):
            pipeline = pool.get()   # borrow from pool; blocks if all in use
            try:
                if job["stop_event"].is_set():
                    raise StopIteration

                ch_num = ch_i + 1
                n      = len(chapters)
                log(f"── Chapter {ch_num}/{n}: {title}")
                log(f"   {len(text):,} chars")

                sentences = re.split(r"(?<=[.!?])\s+", text)
                chunks, cur = [], ""
                for sent in sentences:
                    if len(cur) + len(sent) + 1 <= s["chunk_size"]:
                        cur = (cur + " " + sent).strip()
                    else:
                        if cur: chunks.append(cur)
                        cur = sent
                if cur: chunks.append(cur)
                log(f"   {len(chunks)} chunks")

                ch_audio = []
                for c_i, chunk in enumerate(chunks):
                    if job["stop_event"].is_set():
                        raise StopIteration
                    try:
                        for _, _, audio in pipeline(chunk, voice=s["voice"], speed=s["speed"]):
                            ch_audio.append(audio)
                    except StopIteration:
                        raise
                    except Exception as e:
                        log(f"   ! Ch{ch_num} chunk {c_i+1} skipped: {e}")

                if not ch_audio:
                    log(f"   (no audio generated)\n")
                    return (ch_i, None, None, 0.0)

                combined   = np.concatenate(ch_audio)
                safe_title = re.sub(r"[^\w\s-]", "", title)[:35].strip()
                fname_wav  = f"{book_stem}_{safe_title}.wav"
                out_wav    = os.path.join(s["out_dir"], fname_wav)
                sf.write(out_wav, combined, RATE)

                if s.get("output_format") == "mp3":
                    fname   = f"{book_stem}_{safe_title}.mp3"
                    _to_mp3(out_wav, os.path.join(s["out_dir"], fname),
                            s["bitrate"],
                            title=title,
                            album=s.get("book_title_meta") or book_stem,
                            artist=s.get("book_author_meta", ""),
                            track=ch_num,
                            cover_data=s.get("cover_data"),
                            cover_mime=s.get("cover_mime", "image/jpeg"))
                    os.remove(out_wav)
                else:
                    fname = fname_wav

                dur = len(combined) / RATE
                log(f"   Saved: {fname}  ({dur:.1f}s)\n")

                with done_lock:
                    done_count[0] += 1
                    frac = done_count[0] / n
                prog(frac, f"{done_count[0]}/{n} chapters done")

                _push({"type": "file", "filename": fname,
                       "duration": dur, "chapter": ch_num, "title": title})
                return (ch_i, fname, combined, dur)

            finally:
                pool.put(pipeline)   # always return — prevents pool starvation

        # ── Parallel execution ────────────────────────────────────────────────
        status(f"Processing {len(chapters)} chapter(s) with {num_w} worker(s)…")
        futures_map: dict = {}
        results:     dict = {}   # ch_i -> (ch_i, fname, audio, dur)

        with ThreadPoolExecutor(max_workers=num_w) as executor:
            for ch_i, (title, text) in enumerate(chapters):
                if job["stop_event"].is_set():
                    break
                futures_map[executor.submit(process_chapter, ch_i, title, text)] = (ch_i, title)

            for fut in as_completed(futures_map):
                ch_i, title = futures_map[fut]
                try:
                    r = fut.result()
                    results[r[0]] = r
                except StopIteration:
                    for pending in futures_map:
                        pending.cancel()
                    job["status"] = "cancelled"
                    log("\nStopped by user.")
                    done(); return
                except Exception as exc:
                    log(f"\nChapter {ch_i+1} ({title[:40]}) failed: {exc}")

        if job["stop_event"].is_set():
            log("\nStopped by user.")
            job["status"] = "cancelled"
            done(); return

        memlog("all chapters complete")

        # ── Collect results in chapter order, build merge list ────────────────
        all_audio = []
        for ch_i in sorted(results.keys()):
            _, fname, audio, _ = results[ch_i]
            if fname:
                job["files"].append(fname)
            if s["merge"] and audio is not None:
                all_audio.append(audio)
                if ch_i < len(chapters) - 1:
                    all_audio.append(silence_arr)

        # ── Merge ─────────────────────────────────────────────────────────────
        if s["merge"] and all_audio and not job["stop_event"].is_set():
            status("Merging chapters…")
            log(f"Merging {len(chapters)} chapters…")
            full     = np.concatenate(all_audio)
            fname_wav = f"{book_stem}_FULL.wav"
            out_wav  = os.path.join(s["out_dir"], fname_wav)
            sf.write(out_wav, full, RATE)

            if s.get("output_format") == "mp3":
                fname   = f"{book_stem}_FULL.mp3"
                _to_mp3(out_wav, os.path.join(s["out_dir"], fname),
                        s["bitrate"],
                        title="Full Audiobook",
                        album=s.get("book_title_meta") or book_stem,
                        artist=s.get("book_author_meta", ""),
                        cover_data=s.get("cover_data"),
                        cover_mime=s.get("cover_mime", "image/jpeg"))
                os.remove(out_wav)
            else:
                fname = fname_wav

            mins  = len(full) / RATE / 60
            log(f"Full audiobook saved — {mins:.1f} min")
            job["files"].append(fname)
            _push({
                "type": "file", "filename": fname,
                "duration": len(full) / RATE, "chapter": 0,
                "title": "Full Audiobook (Merged)",
            })

        memlog("done")
        log(f"\nDone! {len(job['files'])} file(s) created.")
        job["status"] = "done"
        done()

    except Exception as exc:
        import traceback
        log(f"\nError: {exc}")
        log(traceback.format_exc())
        job["status"] = "error"
        done()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860, reload=False)
