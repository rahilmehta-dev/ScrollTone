"""
Conversion job lifecycle routes.

POST /convert                   — upload EPUB(s) + settings, start background jobs
GET  /stream/{job_id}           — SSE stream of live progress logs
POST /stop/{job_id}             — cancel a running job
GET  /download/{job_id}/{file}  — download a completed audio file
"""
import asyncio
import json
import os
import re
import threading
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

import backend.state as state
from backend.epub_parser import get_book_metadata
from backend.pipeline import convert_book

router = APIRouter()

ALLOWED_SUFFIXES = {".epub", ".txt", ".zip"}


def _sanitize_folder_name(name: str) -> str:
    safe = re.sub(r"[^\w\s\-]", "_", name)
    safe = re.sub(r"[\s_]+", "_", safe).strip("_")
    return safe[:80] or "Untitled"


@router.post("/convert")
async def convert(
    files:            list[UploadFile] = File(...),
    voice:            str   = Form("af_heart"),
    lang_code:        str   = Form("a"),
    speed:            float = Form(1.0),
    device:           str   = Form("auto"),
    trf:              str   = Form("false"),
    merge:            str   = Form("true"),
    chunk_size:       int   = Form(500),
    silence:          float = Form(1.0),
    min_ch_len:       int   = Form(200),
    output_format:    str   = Form("wav"),
    bitrate:          int   = Form(192),
    custom_out_dir:   str   = Form(""),
    chapter_indices:  str   = Form(""),   # comma-separated; empty = all chapters
    enhance:          str   = Form("false"),  # broadcast-style ffmpeg post-processing
    multi_voice:      str   = Form("false"),  # LLM speaker attribution
    ambience:         str   = Form("false"),  # LLM scene-cue detection + ambient mixing
    ollama_url:       str   = Form("http://localhost:11434"),
    ollama_model:     str   = Form("phi3:mini"),
    engine:              str   = Form("kokoro"),      # kokoro | higgs | chatterbox
    reference_audio:     UploadFile | None = File(None),  # required for higgs/chatterbox
    kokoro_workers:      int   = Form(1),  # concurrent Kokoro subprocesses per chapter
    chatterbox_workers:  int   = Form(1),  # concurrent Chatterbox subprocesses per chapter
    chatterbox_speed:    float = Form(1.0),  # ffmpeg atempo — Chatterbox has no native rate control
    chatterbox_cfg_weight:   float = Form(0.3),
    chatterbox_exaggeration: float = Form(0.7),
    chatterbox_temperature:  float = Form(0.8),
    chatterbox_breaths:      str   = Form("true"),  # synthetic breath sounds between chunks
    higgs_temperature:   float = Form(0.15),  # lower than Boson's own 1.0 default — see note below
    higgs_top_p:         float = Form(0.75),  # lower than Boson's own 0.95 default — same reasoning
    higgs_top_k:         int   = Form(25),    # lower than Boson's own 50 default — same reasoning
    higgs_workers:       int   = Form(1),     # concurrent Higgs subprocesses per chapter — see cap below
):
    if engine not in ("kokoro", "higgs", "chatterbox"):
        raise HTTPException(400, f"Unknown engine: {engine}")

    kokoro_workers      = max(1, min(kokoro_workers, os.cpu_count() or 1))
    chatterbox_workers = max(1, min(chatterbox_workers, 16))
    chatterbox_speed   = max(0.5, min(chatterbox_speed, 1.5))
    chatterbox_cfg_weight   = max(0.0, min(chatterbox_cfg_weight, 1.0))
    chatterbox_exaggeration = max(0.1, min(chatterbox_exaggeration, 2.0))
    chatterbox_temperature  = max(0.05, min(chatterbox_temperature, 1.5))
    # Every chunk is an independent sampling call (backend/engines/higgs_synth.py
    # has no cross-chunk state) — Boson's own default temperature=1.0 is tuned
    # for expressive one-off clips, not hundreds of chunks that all need to
    # sound like the same narrator. Lower is less randomness = a more
    # consistent-sounding voice across a whole book, at some cost to
    # per-line expressiveness.
    higgs_temperature = max(0.05, min(higgs_temperature, 1.5))
    higgs_top_p       = max(0.1, min(higgs_top_p, 1.0))
    higgs_top_k       = max(1, min(higgs_top_k, 200))
    # Capped far lower than chatterbox_workers (16): each Higgs instance is
    # ~12GB RAM (vs. Chatterbox's ~6.6GB, CPU-only) and, unlike Chatterbox's
    # well-tested CPU multiprocessing, these run on MPS/CUDA — multiple
    # processes contending for the same GPU is untested here and scales far
    # less predictably than independent CPU workers.
    higgs_workers = max(1, min(higgs_workers, 4))

    if engine != "kokoro":
        if multi_voice.lower() == "true":
            raise HTTPException(
                400, "Multi-voice is Kokoro-only for now — switch engine back to Kokoro to use it."
            )
        if reference_audio is None:
            raise HTTPException(
                400, f"The {engine} engine requires a reference voice clip (reference_audio) to clone."
            )

    batch_id = str(uuid.uuid4())
    job_ids  = []
    titles   = []

    reference_wav_path = None
    if reference_audio is not None:
        ref_dir = state.UPLOAD_DIR / batch_id
        ref_dir.mkdir(parents=True, exist_ok=True)
        # basename() strips any directory components from a client-supplied
        # filename (e.g. "../../etc/passwd") before it's used as a path.
        safe_name = Path(reference_audio.filename or "reference_audio").name or "reference_audio"
        ref_path = ref_dir / safe_name
        ref_path.write_bytes(await reference_audio.read())

        try:
            import soundfile as sf
            info = sf.info(str(ref_path))
            if info.duration < 3.0:
                raise HTTPException(
                    400,
                    f"Reference clip is too short ({info.duration:.1f}s) — use at least 5s, "
                    "8-20s of clean single-speaker audio works best.",
                )
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(400, f"Couldn't read reference audio file: {error}")

        reference_wav_path = str(ref_path)

    from backend.audio import analyze_reference_quality
    reference_warnings = (
        analyze_reference_quality(reference_wav_path)["warnings"]
        if reference_wav_path else []
    )

    if device == "auto":
        import torch
        if torch.backends.mps.is_available():
            resolved_device = "mps"
        elif torch.cuda.is_available():
            resolved_device = "cuda"
        else:
            resolved_device = "cpu"
    else:
        resolved_device = device

    loop = asyncio.get_running_loop()
    job_and_settings_pairs = []   # collected in upload order; processed sequentially below

    for file in files:
        job_id = str(uuid.uuid4())

        if Path(file.filename or "").suffix.lower() not in ALLOWED_SUFFIXES:
            raise HTTPException(
                400, f"Unsupported file type: {file.filename} — upload .epub or .txt files."
            )

        # Persist the uploaded book
        up_dir  = state.UPLOAD_DIR / job_id
        up_dir.mkdir(parents=True, exist_ok=True)
        up_path = up_dir / file.filename
        up_path.write_bytes(await file.read())

        # If user uploaded a .zip containing an .epub, extract it automatically
        if up_path.suffix.lower() not in (".epub", ".txt") and zipfile.is_zipfile(up_path):
            with zipfile.ZipFile(up_path) as zip_file:
                epub_entries = [name for name in zip_file.namelist() if name.lower().endswith(".epub")]
            if epub_entries:
                inner_name = Path(epub_entries[0]).name
                with zipfile.ZipFile(up_path) as zip_file:
                    (up_dir / inner_name).write_bytes(zip_file.read(epub_entries[0]))
                up_path.unlink()
                up_path = up_dir / inner_name

        is_txt = up_path.suffix.lower() == ".txt"

        # Derive book title from EPUB metadata for the subfolder name
        # (plain .txt files have no metadata — fall back to the filename)
        meta_title = ""
        if not is_txt:
            try:
                from ebooklib import epub
                book       = epub.read_epub(str(up_path))
                meta_title = get_book_metadata(book).get("title", "").strip()
            except Exception:
                meta_title = ""

        folder_name = _sanitize_folder_name(meta_title or Path(file.filename).stem)

        # Resolve output directory: book_name / (no batch UUID wrapper)
        if custom_out_dir.strip():
            out_dir = (
                Path(custom_out_dir.strip()).expanduser().resolve()
                / folder_name
            )
        else:
            out_dir = state.OUTPUT_DIR / folder_name
        out_dir.mkdir(parents=True, exist_ok=True)

        async_queue = asyncio.Queue()
        stop_event  = threading.Event()

        job_state = {
            "id":         job_id,
            "batch_id":   batch_id,
            "book_title": meta_title or Path(file.filename).stem,
            "status":     "queued",
            "queue":      async_queue,
            "stop_event": stop_event,
            "out_dir":    str(out_dir),
            "files":      [],
        }
        state.jobs[job_id] = job_state

        settings = {
            "source_path":   str(up_path),
            "filename":      file.filename,
            "out_dir":       str(out_dir),
            "voice":         voice,
            "lang_code":     lang_code,
            "speed":         speed,
            "device":        resolved_device,
            "trf":           trf.lower() == "true",
            "merge":         merge.lower() == "true",
            "chunk_size":    chunk_size,
            "silence":       silence,
            "min_ch_len":    min_ch_len,
            "output_format":   output_format.lower(),
            "bitrate":         bitrate,
            "chapter_indices": (
                [int(index_str) for index_str in chapter_indices.split(",") if index_str.strip()]
                if chapter_indices.strip() else None
            ),
            "enhance":      enhance.lower() == "true",
            "multi_voice":  multi_voice.lower() == "true",
            "ambience":     ambience.lower() == "true",
            "ollama_url":   ollama_url.strip() or "http://localhost:11434",
            "ollama_model": ollama_model.strip() or "phi3:mini",
            "engine":              engine,
            "reference_wav":       reference_wav_path,
            "kokoro_workers":      kokoro_workers,
            "chatterbox_workers":  chatterbox_workers,
            "chatterbox_speed":    chatterbox_speed,
            "chatterbox_cfg_weight":   chatterbox_cfg_weight,
            "chatterbox_exaggeration": chatterbox_exaggeration,
            "chatterbox_temperature":  chatterbox_temperature,
            "chatterbox_breaths":      chatterbox_breaths.lower() == "true",
            "higgs_temperature":   higgs_temperature,
            "higgs_top_p":         higgs_top_p,
            "higgs_top_k":         higgs_top_k,
            "higgs_workers":       higgs_workers,
            "reference_warnings":  reference_warnings,
        }

        job_and_settings_pairs.append((job_state, settings))
        job_ids.append(job_id)
        titles.append(job_state["book_title"])

    # Run all books one after another in a background thread — one book's
    # pipeline is fully unloaded before the next book loads, keeping peak RAM
    # predictable. The thread keeps this blocking work off the event loop.
    def _convert_all_books():
        for job_state, settings in job_and_settings_pairs:
            job_state["status"] = "running"
            convert_book(job_state, settings, loop)

    threading.Thread(target=_convert_all_books, daemon=True).start()

    return {
        "batch_id": batch_id, "job_ids": job_ids, "titles": titles,
        "reference_warnings": reference_warnings,
    }


@router.get("/stream/{job_id}")
async def stream(job_id: str):
    job_state = state.jobs.get(job_id)
    if not job_state:
        raise HTTPException(404, "Job not found")

    async def generator():
        queue: asyncio.Queue = job_state["queue"]
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=30)
                    if msg is None:
                        yield (
                            "data: "
                            + json.dumps({"type": "done", "files": job_state["files"]})
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


@router.post("/stop/{job_id}")
async def stop_job(job_id: str):
    job_state = state.jobs.get(job_id)
    if not job_state:
        raise HTTPException(404, "Job not found")
    job_state["stop_event"].set()
    return {"status": "stopping"}


@router.get("/download/{job_id}/{filename}")
async def download(job_id: str, filename: str):
    job_state = state.jobs.get(job_id)
    if not job_state:
        raise HTTPException(404, "Job not found")
    path = Path(job_state["out_dir"]) / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    media_type = "audio/mpeg" if filename.endswith(".mp3") else "audio/wav"
    return FileResponse(str(path), filename=filename, media_type=media_type)
