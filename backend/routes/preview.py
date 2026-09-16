"""
Voice preview routes.

GET  /preview/{voice}            — quick audition: stream the cached
                                    (speed=1.0) sample clip, used by the small
                                    ▶ button next to the Narrator Voice
                                    dropdown while browsing the voice list.
GET  /preview/{voice}?speed=1.25 — same, live-synthesized at a custom speed
                                    (not cached — see synthesize_sample in
                                    backend/voices.py).
POST /preview-job                — the Preview & Tweak card's real synthesis:
                                    starts a background job on the full
                                    ChapterProcessor pipeline (Kokoro, Higgs,
                                    or Chatterbox; Multi-voice/Ambient sound
                                    included when checked) for one synthetic
                                    "chapter" — PREVIEW_JOB_TEXT. Returns a job_id
                                    that behaves exactly like a real
                                    conversion's: routes/convert.py's
                                    /stream/{id} (chunk-by-chunk progress +
                                    logs), /stop/{id}, and
                                    /download/{id}/{file} all work unchanged.
"""
import asyncio
import io
import os
import tempfile
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response

import backend.state as state
from backend.pipeline import run_preview_job
from backend.voices import KNOWN_VOICES, _generate_preview, synthesize_sample

router = APIRouter()


@router.get("/preview/{voice}")
async def preview_voice(voice: str, speed: float = Query(1.0, ge=0.5, le=2.5)):
    if voice not in KNOWN_VOICES:
        raise HTTPException(400, f"Unknown voice: {voice}")

    loop = asyncio.get_running_loop()

    if speed == 1.0:
        # Common case — served from the pre-baked previews/ directory;
        # generated once on first request if the cached file is missing.
        cache = state.PREVIEW_DIR / f"{voice}.wav"
        if not cache.exists():
            await loop.run_in_executor(None, _generate_preview, voice, cache)
        if not cache.exists():
            raise HTTPException(500, "Preview generation failed")
        return FileResponse(str(cache), media_type="audio/wav",
                            headers={"Cache-Control": "public, max-age=86400"})

    audio = await loop.run_in_executor(None, synthesize_sample, voice, speed)
    if audio is None:
        raise HTTPException(500, "Preview generation failed")

    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, audio, 24000, format="WAV")
    return Response(content=buf.getvalue(), media_type="audio/wav")


@router.post("/preview-job")
async def start_preview_job(
    engine:          str   = Form("kokoro"),        # kokoro | higgs | chatterbox
    voice:           str   = Form("af_heart"),       # kokoro only
    speed:           float = Form(1.0),              # kokoro only
    device:          str   = Form("auto"),
    multi_voice:     str   = Form("false"),          # kokoro only
    ambience:        str   = Form("false"),
    ollama_url:      str   = Form("http://localhost:11434"),
    ollama_model:    str   = Form("phi3:mini"),
    reference_audio: UploadFile | None = File(None),  # required for higgs/chatterbox
    kokoro_workers:           int   = Form(1),
    chatterbox_workers:       int   = Form(1),
    chatterbox_speed:        float = Form(1.0),
    chatterbox_cfg_weight:   float = Form(0.3),
    chatterbox_exaggeration: float = Form(0.7),
    chatterbox_temperature:  float = Form(0.8),
    higgs_temperature:       float = Form(0.15),
    higgs_top_p:             float = Form(0.75),
    higgs_top_k:             int   = Form(25),
    higgs_workers:           int   = Form(1),
):
    if engine not in ("kokoro", "higgs", "chatterbox"):
        raise HTTPException(400, f"Unknown engine: {engine}")

    multi_voice_bool = multi_voice.lower() == "true"
    ambience_bool    = ambience.lower() == "true"
    kokoro_workers      = max(1, min(kokoro_workers, os.cpu_count() or 1))
    chatterbox_workers  = max(1, min(chatterbox_workers, 16))
    higgs_workers       = max(1, min(higgs_workers, 4))  # see cap reasoning in routes/convert.py

    if engine != "kokoro":
        if multi_voice_bool:
            raise HTTPException(400, "Multi-voice is Kokoro-only for now.")
        if reference_audio is None:
            raise HTTPException(
                400, f"The {engine} engine requires a reference voice clip (reference_audio) to clone."
            )
    elif voice not in KNOWN_VOICES:
        raise HTTPException(400, f"Unknown voice: {voice}")

    job_id = str(uuid.uuid4())

    reference_wav_path = None
    reference_warnings: list[str] = []
    if reference_audio is not None:
        ref_dir = state.UPLOAD_DIR / job_id
        ref_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(reference_audio.filename or "reference_audio").name or "reference_audio"
        ref_path = ref_dir / safe_name
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

        reference_wav_path = str(ref_path)
        from backend.audio import analyze_reference_quality
        reference_warnings = analyze_reference_quality(reference_wav_path)["warnings"]

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

    # Deliberately NOT under state.OUTPUT_DIR — that's the user's real
    # audiobook_output/ folder (bind-mounted in Docker), and a preview sample
    # has no business cluttering it.
    out_dir = Path(tempfile.gettempdir()) / "scrolltone_previews" / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

    async_queue = asyncio.Queue()
    stop_event  = threading.Event()
    job_state = {
        "id": job_id, "status": "queued", "queue": async_queue,
        "stop_event": stop_event, "out_dir": str(out_dir), "files": [],
    }
    state.jobs[job_id] = job_state

    settings = {
        "voice":         voice,
        "lang_code":     "b" if voice[:2] in ("bf", "bm") else "a",
        "speed":         speed,
        "device":        resolved_device,
        "out_dir":       str(out_dir),
        "engine":        engine,
        "reference_wav": reference_wav_path,
        "reference_warnings": reference_warnings,
        "multi_voice":   multi_voice_bool,
        "ambience":      ambience_bool,
        "ollama_url":    ollama_url.strip() or "http://localhost:11434",
        "ollama_model":  ollama_model.strip() or "phi3:mini",
        "chunk_size":    500,
        "kokoro_workers":         kokoro_workers,
        "chatterbox_workers":     chatterbox_workers,
        "chatterbox_speed":       chatterbox_speed,
        "chatterbox_cfg_weight":  chatterbox_cfg_weight,
        "chatterbox_exaggeration": chatterbox_exaggeration,
        "chatterbox_temperature": chatterbox_temperature,
        "chatterbox_breaths":     False,
        "higgs_temperature": higgs_temperature,
        "higgs_top_p":       higgs_top_p,
        "higgs_top_k":       higgs_top_k,
        "higgs_workers":     higgs_workers,
        "enhance":       False,
        "output_format": "wav",
        "bitrate":       192,
    }

    loop = asyncio.get_running_loop()
    job_state["status"] = "running"
    threading.Thread(target=run_preview_job, args=(job_state, settings, loop), daemon=True).start()

    return {"job_id": job_id, "reference_warnings": reference_warnings}
