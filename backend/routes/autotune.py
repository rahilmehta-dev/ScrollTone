"""
Auto-tune voice routes.

POST /autotune-job       — samples a handful of Chatterbox/Higgs synthesis
                            parameter combos (Latin Hypercube Sampling — see
                            backend/voice_tuning.py), synthesizes the same
                            short sample text with each against ONE uploaded
                            reference clip, scores every candidate with an
                            open-source audio-quality model, and reports the
                            ranked results.
POST /autotune-batch-job — same search, run independently for SEVERAL
                            uploaded reference clips in one job (the
                            standalone Voice Lab tool page, frontend/
                            voice-lab.html) — for comparing candidate voice
                            clips against each other, not just tuning one.

Both return a job_id that behaves like Preview & Tweak's: routes/convert.py's
/stream/{id}, /stop/{id}, and /download/{id}/{file} all work unchanged.
"""
import asyncio
import tempfile
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

import backend.state as state
from backend.voice_tuning import (
    run_autotune_job, run_batch_autotune_job,
    MIN_CANDIDATES, MAX_CANDIDATES, DEFAULT_CANDIDATES, DEFAULT_SEED, MIN_VOICES, MAX_VOICES,
)

router = APIRouter()


def _resolve_device(device: str) -> str:
    if device != "auto":
        return device
    import torch
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _save_reference(job_dir: Path, upload: UploadFile, data: bytes) -> Path:
    safe_name = Path(upload.filename or "reference_audio").name or "reference_audio"
    path = job_dir / safe_name
    path.write_bytes(data)
    return path


async def _validate_reference(path: Path) -> None:
    try:
        import soundfile as sf
        info = sf.info(str(path))
        if info.duration < 3.0:
            raise HTTPException(
                400, f"{path.name}: reference clip is too short ({info.duration:.1f}s) — use at least 5s.")
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(400, f"Couldn't read reference audio file {path.name!r}: {error}")


def _new_job(job_id: str, out_root: str) -> tuple[dict, Path]:
    """Register a job in state.jobs and create its output directory.

    Takes an already-minted job_id rather than minting one, so callers can
    save and validate the uploaded reference clips FIRST and only register
    the job once those succeed. Registering up front meant a 400 from
    _validate_reference left a job entry in state.jobs (and a temp dir)
    that nothing would ever run, complete, or clean up.
    """
    out_dir = Path(tempfile.gettempdir()) / out_root / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    async_queue = asyncio.Queue()
    stop_event  = threading.Event()
    job_state = {
        "id": job_id, "status": "queued", "queue": async_queue,
        "stop_event": stop_event, "out_dir": str(out_dir), "files": [],
    }
    state.jobs[job_id] = job_state
    return job_state, out_dir


@router.post("/autotune-job")
async def start_autotune_job(
    engine:           str            = Form(...),          # chatterbox | higgs
    device:           str            = Form("auto"),
    reference_audio:  UploadFile     = File(...),
    num_candidates:   int            = Form(DEFAULT_CANDIDATES),
    seed:             int            = Form(DEFAULT_SEED),
    chatterbox_workers: int          = Form(1),
    higgs_workers:      int          = Form(1),
):
    if engine not in ("chatterbox", "higgs"):
        raise HTTPException(400, f"Auto-tune isn't available for engine={engine!r} — only chatterbox/higgs have tunable synthesis parameters.")

    num_candidates      = max(MIN_CANDIDATES, min(num_candidates, MAX_CANDIDATES))
    chatterbox_workers  = max(1, min(chatterbox_workers, 16))
    higgs_workers       = max(1, min(higgs_workers, 4))
    resolved_device      = _resolve_device(device)

    job_id = str(uuid.uuid4())
    ref_dir = state.UPLOAD_DIR / job_id
    ref_dir.mkdir(parents=True, exist_ok=True)
    ref_path = _save_reference(ref_dir, reference_audio, await reference_audio.read())
    await _validate_reference(ref_path)

    # Only now that the upload is known good — see _new_job's docstring.
    # Deliberately NOT under state.OUTPUT_DIR — same reasoning as Preview &
    # Tweak (routes/preview.py): these candidate clips have no business
    # cluttering the user's real audiobook_output/ folder.
    job_state, out_dir = _new_job(job_id, "scrolltone_autotune")

    settings = {
        "engine":          engine,
        "device":          resolved_device,
        "reference_wav":   str(ref_path),
        "out_dir":         str(out_dir),
        "num_candidates":  num_candidates,
        "seed":            seed,
        "chatterbox_workers": chatterbox_workers,
        "higgs_workers":      higgs_workers,
    }

    loop = asyncio.get_running_loop()
    job_state["status"] = "running"
    threading.Thread(target=run_autotune_job, args=(job_state, settings, loop), daemon=True).start()

    return {"job_id": job_id}


@router.post("/autotune-batch-job")
async def start_autotune_batch_job(
    engine:           str                 = Form(...),          # chatterbox | higgs
    device:           str                 = Form("auto"),
    reference_audio:  list[UploadFile]    = File(...),
    num_candidates:   int                 = Form(DEFAULT_CANDIDATES),
    seed:             int                 = Form(DEFAULT_SEED),
    chatterbox_workers: int               = Form(1),
    higgs_workers:      int               = Form(1),
):
    if engine not in ("chatterbox", "higgs"):
        raise HTTPException(400, f"Auto-tune isn't available for engine={engine!r} — only chatterbox/higgs have tunable synthesis parameters.")
    if not (MIN_VOICES <= len(reference_audio) <= MAX_VOICES):
        raise HTTPException(400, f"Upload between {MIN_VOICES} and {MAX_VOICES} reference clips (got {len(reference_audio)}).")

    num_candidates      = max(MIN_CANDIDATES, min(num_candidates, MAX_CANDIDATES))
    chatterbox_workers  = max(1, min(chatterbox_workers, 16))
    higgs_workers       = max(1, min(higgs_workers, 4))
    resolved_device      = _resolve_device(device)

    job_id = str(uuid.uuid4())
    ref_dir = state.UPLOAD_DIR / job_id
    ref_dir.mkdir(parents=True, exist_ok=True)

    voices = []
    for i, upload in enumerate(reference_audio):
        # Sub-directory per upload — two different clips can share a
        # filename ("clip.wav") and would otherwise overwrite each other.
        voice_dir = ref_dir / f"v{i:02d}"
        voice_dir.mkdir(parents=True, exist_ok=True)
        path = _save_reference(voice_dir, upload, await upload.read())
        await _validate_reference(path)
        voices.append({"filename": path.name, "path": str(path)})

    # Only now that every upload is known good — see _new_job's docstring.
    job_state, out_dir = _new_job(job_id, "scrolltone_autotune_batch")

    settings = {
        "engine":          engine,
        "device":          resolved_device,
        "voices":          voices,
        "out_dir":         str(out_dir),
        "num_candidates":  num_candidates,
        "seed":            seed,
        "chatterbox_workers": chatterbox_workers,
        "higgs_workers":      higgs_workers,
    }

    loop = asyncio.get_running_loop()
    job_state["status"] = "running"
    threading.Thread(target=run_batch_autotune_job, args=(job_state, settings, loop), daemon=True).start()

    return {"job_id": job_id, "num_voices": len(voices)}
