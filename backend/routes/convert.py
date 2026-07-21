"""
Conversion routes.

POST /convert                   — upload EPUB(s) + settings, start background jobs
GET  /stream/{job_id}           — SSE stream of live progress logs
POST /stop/{job_id}             — cancel a running job
GET  /download/{job_id}/{file}  — download a completed audio file
"""
import asyncio
import json
import re
import threading
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

import backend.state as state
from backend.epub_parser import extract_chapters, get_book_metadata
from backend.pipeline import convert_book

router = APIRouter()


def _sanitize_folder_name(name: str) -> str:
    safe = re.sub(r"[^\w\s\-]", "_", name)
    safe = re.sub(r"[\s_]+", "_", safe).strip("_")
    return safe[:80] or "Untitled"


@router.post("/chapters")
async def list_chapters(
    file:       UploadFile = File(...),
    min_ch_len: int        = Form(200),
):
    """Parse an EPUB and return its chapter list (title + char count)."""
    import tempfile, os
    from ebooklib import epub as _epub

    data     = await file.read()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        book     = _epub.read_epub(tmp_path)
        chapters = extract_chapters(book, min_ch_len)
        return {
            "chapters": [
                {"index": index, "title": title, "chars": len(text)}
                for index, (title, text) in enumerate(chapters)
            ]
        }
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except OSError: pass


@router.post("/convert")
async def convert(
    files:          list[UploadFile] = File(...),
    voice:          str   = Form("af_heart"),
    lang_code:      str   = Form("a"),
    speed:          float = Form(1.0),
    device:         str   = Form("auto"),
    trf:            str   = Form("false"),
    merge:          str   = Form("true"),
    chunk_size:     int   = Form(500),
    silence:        float = Form(1.0),
    min_ch_len:     int   = Form(200),
    output_format:   str   = Form("wav"),
    bitrate:         int   = Form(192),
    custom_out_dir:  str   = Form(""),
    chapter_indices: str   = Form(""),   # comma-separated; empty = all chapters
    enhance:         str   = Form("false"),  # broadcast-style ffmpeg post-processing
    multi_voice:     str   = Form("false"),  # LLM speaker attribution
    ollama_url:      str   = Form("http://localhost:11434"),
    ollama_model:    str   = Form("phi3:mini"),
):
    batch_id = str(uuid.uuid4())
    job_ids  = []
    titles   = []

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

        # Persist the uploaded EPUB
        up_dir  = state.UPLOAD_DIR / job_id
        up_dir.mkdir(parents=True, exist_ok=True)
        up_path = up_dir / file.filename
        up_path.write_bytes(await file.read())

        # If user uploaded a .zip containing an .epub, extract it automatically
        if up_path.suffix.lower() != ".epub" and zipfile.is_zipfile(up_path):
            with zipfile.ZipFile(up_path) as zip_file:
                epub_entries = [name for name in zip_file.namelist() if name.lower().endswith(".epub")]
            if epub_entries:
                inner_name = Path(epub_entries[0]).name
                with zipfile.ZipFile(up_path) as zip_file:
                    (up_dir / inner_name).write_bytes(zip_file.read(epub_entries[0]))
                up_path.unlink()
                up_path = up_dir / inner_name

        # Derive book title from EPUB metadata for the subfolder name
        try:
            from ebooklib import epub as _epub
            book       = _epub.read_epub(str(up_path))
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
            "epub":          str(up_path),
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
            "ollama_url":   ollama_url.strip() or "http://localhost:11434",
            "ollama_model": ollama_model.strip() or "phi3:mini",
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

    return {"batch_id": batch_id, "job_ids": job_ids, "titles": titles}


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
