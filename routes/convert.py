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
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

import core.state as state
from core.job_runner import _worker

router = APIRouter()


def _sanitize_folder_name(name: str) -> str:
    safe = re.sub(r"[^\w\s\-]", "_", name)
    safe = re.sub(r"[\s_]+", "_", safe).strip("_")
    return safe[:80] or "Untitled"


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
    num_workers:    int   = Form(1),
    output_format:  str   = Form("wav"),
    bitrate:        int   = Form(192),
    custom_out_dir: str   = Form(""),
):
    batch_id = str(uuid.uuid4())
    job_ids  = []
    titles   = []

    resolved_device = None if device == "auto" else device
    # CUDA serialises inference anyway — cap at 1 worker to avoid VRAM waste
    eff_workers = 1 if resolved_device == "cuda" else max(1, min(num_workers, 4))

    loop = asyncio.get_running_loop()

    for file in files:
        job_id = str(uuid.uuid4())

        # Persist the uploaded EPUB
        up_dir  = state.UPLOAD_DIR / job_id
        up_dir.mkdir(parents=True, exist_ok=True)
        up_path = up_dir / file.filename
        up_path.write_bytes(await file.read())

        # Derive book title from EPUB metadata for the subfolder name
        try:
            from ebooklib import epub as _epub
            from core.epub_parser import get_book_metadata
            book       = _epub.read_epub(str(up_path))
            meta_title = get_book_metadata(book).get("title", "").strip()
        except Exception:
            meta_title = ""

        folder_name = _sanitize_folder_name(meta_title or Path(file.filename).stem)

        # Resolve output directory: batch_id / book_name /
        if custom_out_dir.strip():
            out_dir = (
                Path(custom_out_dir.strip()).expanduser().resolve()
                / batch_id / folder_name
            )
        else:
            out_dir = state.OUTPUT_DIR / batch_id / folder_name
        out_dir.mkdir(parents=True, exist_ok=True)

        async_queue = asyncio.Queue()
        stop_event  = threading.Event()

        job = {
            "id":         job_id,
            "batch_id":   batch_id,
            "book_title": meta_title or Path(file.filename).stem,
            "status":     "running",
            "queue":      async_queue,
            "stop_event": stop_event,
            "out_dir":    str(out_dir),
            "files":      [],
        }
        state.jobs[job_id] = job

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
            "num_workers":   eff_workers,
            "output_format": output_format.lower(),
            "bitrate":       bitrate,
        }

        threading.Thread(
            target=_worker, args=(job, settings, loop), daemon=True
        ).start()

        job_ids.append(job_id)
        titles.append(job["book_title"])

    return {"batch_id": batch_id, "job_ids": job_ids, "titles": titles}


@router.get("/stream/{job_id}")
async def stream(job_id: str):
    job = state.jobs.get(job_id)
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


@router.post("/stop/{job_id}")
async def stop_job(job_id: str):
    job = state.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job["stop_event"].set()
    return {"status": "stopping"}


@router.get("/download/{job_id}/{filename}")
async def download(job_id: str, filename: str):
    job = state.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    path = Path(job["out_dir"]) / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    media_type = "audio/mpeg" if filename.endswith(".mp3") else "audio/wav"
    return FileResponse(str(path), filename=filename, media_type=media_type)
