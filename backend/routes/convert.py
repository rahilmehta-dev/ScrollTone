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
import shutil
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse

import backend.state as state
from backend.epub_parser import extract_chapters, extract_chapters_from_text, get_book_metadata
from backend.pipeline import convert_book
from backend.engines.runner import EngineNotInstalled, synthesize_chapter

router = APIRouter()

ALLOWED_SUFFIXES = {".epub", ".txt", ".zip"}


def _sanitize_folder_name(name: str) -> str:
    safe = re.sub(r"[^\w\s\-]", "_", name)
    safe = re.sub(r"[\s_]+", "_", safe).strip("_")
    return safe[:80] or "Untitled"


@router.post("/chapters")
async def list_chapters(
    file:       UploadFile = File(...),
    min_ch_len: int        = Form(200),
):
    """Parse an EPUB or .txt upload and return its chapter list (title + char count)."""
    import tempfile, os
    from ebooklib import epub

    data = await file.read()
    is_txt = Path(file.filename or "").suffix.lower() == ".txt"

    if is_txt:
        try:
            text     = data.decode("utf-8", errors="ignore")
            chapters = extract_chapters_from_text(text, min_ch_len)
            return {
                "chapters": [
                    {"index": index, "title": title, "chars": len(text)}
                    for index, (title, text) in enumerate(chapters)
                ]
            }
        except Exception as error:
            raise HTTPException(status_code=500, detail=str(error))

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        book     = epub.read_epub(tmp_path)
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


@router.post("/clone-test")
async def clone_test(
    file:            UploadFile = File(...),   # the book (.epub or .txt) — text source for the sample
    reference_audio: UploadFile = File(...),
    engine:          str        = Form(...),   # higgs | chatterbox
    device:          str        = Form("cpu"),
    word_count:      int        = Form(100),
    chatterbox_speed: float     = Form(1.0),   # ffmpeg atempo — Chatterbox has no native rate control
    chatterbox_cfg_weight:   float = Form(0.3),
    chatterbox_exaggeration: float = Form(0.7),
    chatterbox_temperature:  float = Form(0.8),
):
    """Synthesize just the first ~N words of the book with the cloned voice.

    Lets a user check whether a reference clip actually clones well before
    committing to a full (often multi-hour) higgs/chatterbox conversion.
    """
    if engine not in ("higgs", "chatterbox"):
        raise HTTPException(400, f"Voice-clone testing is only for higgs/chatterbox, not {engine!r}.")

    word_count = max(10, min(word_count, 300))
    chatterbox_cfg_weight   = max(0.0, min(chatterbox_cfg_weight, 1.0))
    chatterbox_exaggeration = max(0.1, min(chatterbox_exaggeration, 2.0))
    chatterbox_temperature  = max(0.05, min(chatterbox_temperature, 1.5))

    data   = await file.read()
    is_txt = Path(file.filename or "").suffix.lower() == ".txt"

    tmp_path = None
    try:
        if is_txt:
            chapters = extract_chapters_from_text(data.decode("utf-8", errors="ignore"), min_len=1)
        else:
            from ebooklib import epub
            with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            book     = epub.read_epub(tmp_path)
            chapters = extract_chapters(book, min_len=1)
    except Exception as error:
        raise HTTPException(400, f"Couldn't read the uploaded book: {error}")
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except OSError: pass

    if not chapters:
        raise HTTPException(400, "No readable text found in the uploaded file.")

    _, chapter_text = chapters[0]
    sample_text = " ".join(chapter_text.split()[:word_count]).strip()
    if not sample_text:
        raise HTTPException(400, "The first chapter has no text to test with.")

    ref_dir = Path(tempfile.mkdtemp(prefix="scrolltone_clonetest_"))
    try:
        safe_name = Path(reference_audio.filename or "reference_audio").name or "reference_audio"
        ref_path  = ref_dir / safe_name
        ref_path.write_bytes(await reference_audio.read())

        try:
            import soundfile as sf
            info = sf.info(str(ref_path))
            if info.duration < 3.0:
                raise HTTPException(
                    400, f"Reference clip is too short ({info.duration:.1f}s) — use at least 5s."
                )
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(400, f"Couldn't read reference audio file: {error}")

        from backend.audio import analyze_reference_quality
        reference_warnings = analyze_reference_quality(str(ref_path))["warnings"]

        extra_config = None
        if engine == "chatterbox":
            extra_config = {
                "cfg_weight":   chatterbox_cfg_weight,
                "exaggeration": chatterbox_exaggeration,
                "temperature":  chatterbox_temperature,
            }
        try:
            audio_arrays, results = synthesize_chapter(
                engine, [sample_text], str(ref_path), device,
                on_progress=lambda i, n: None, stop_check=lambda: False,
                extra_config=extra_config,
            )
        except EngineNotInstalled as error:
            raise HTTPException(400, str(error))
    finally:
        shutil.rmtree(ref_dir, ignore_errors=True)

    if not audio_arrays:
        detail = "; ".join(results.get("errors", [])) or "Synthesis produced no audio."
        raise HTTPException(500, f"Clone test failed: {detail}")

    import numpy as np
    import soundfile as sf
    combined = np.concatenate(audio_arrays)

    tmp_wav_dir = Path(tempfile.mkdtemp(prefix="scrolltone_clonetest_out_"))
    try:
        tmp_wav_path = tmp_wav_dir / "sample.wav"
        sf.write(str(tmp_wav_path), combined, 24000)

        if engine == "chatterbox" and chatterbox_speed != 1.0:
            from backend.audio import change_tempo
            try:
                change_tempo(str(tmp_wav_path), chatterbox_speed)
            except Exception:
                pass  # fall back to the untouched-speed sample rather than failing the whole test

        wav_bytes = tmp_wav_path.read_bytes()
    finally:
        shutil.rmtree(tmp_wav_dir, ignore_errors=True)

    return Response(
        content=wav_bytes, media_type="audio/wav",
        headers={
            "X-Sample-Words": str(len(sample_text.split())),
            "X-Reference-Warnings": json.dumps(reference_warnings),
        },
    )


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
    chatterbox_workers:  int   = Form(1),  # concurrent Chatterbox subprocesses per chapter
    chatterbox_speed:    float = Form(1.0),  # ffmpeg atempo — Chatterbox has no native rate control
    chatterbox_cfg_weight:   float = Form(0.3),
    chatterbox_exaggeration: float = Form(0.7),
    chatterbox_temperature:  float = Form(0.8),
    chatterbox_breaths:      str   = Form("true"),  # synthetic breath sounds between chunks
):
    if engine not in ("kokoro", "higgs", "chatterbox"):
        raise HTTPException(400, f"Unknown engine: {engine}")

    chatterbox_workers = max(1, min(chatterbox_workers, 16))
    chatterbox_speed   = max(0.5, min(chatterbox_speed, 1.5))
    chatterbox_cfg_weight   = max(0.0, min(chatterbox_cfg_weight, 1.0))
    chatterbox_exaggeration = max(0.1, min(chatterbox_exaggeration, 2.0))
    chatterbox_temperature  = max(0.05, min(chatterbox_temperature, 1.5))

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
            "chatterbox_workers":  chatterbox_workers,
            "chatterbox_speed":    chatterbox_speed,
            "chatterbox_cfg_weight":   chatterbox_cfg_weight,
            "chatterbox_exaggeration": chatterbox_exaggeration,
            "chatterbox_temperature":  chatterbox_temperature,
            "chatterbox_breaths":      chatterbox_breaths.lower() == "true",
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
