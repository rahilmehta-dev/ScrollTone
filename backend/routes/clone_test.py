"""
Voice-clone preview route.

POST /clone-test — synthesize just the first ~N words of an uploaded book
                    with a cloned voice (Higgs/Chatterbox), so a user can
                    check whether a reference clip actually clones well
                    before committing to a full, often multi-hour conversion.
"""
import json
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from backend.epub_parser import extract_chapters, extract_chapters_from_text
from backend.engines.runner import EngineNotInstalled, synthesize_chapter

router = APIRouter()


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

    import os

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
