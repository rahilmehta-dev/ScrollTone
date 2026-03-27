"""
Voice preview route.

GET /preview/{voice} — stream a short WAV clip for the requested voice.
Served from the pre-baked previews/ directory; generated on first request
if the cached file is missing.
"""
import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

import core.state as state
from core.tts_engine import KNOWN_VOICES, _generate_preview

router = APIRouter()


@router.get("/preview/{voice}")
async def preview_voice(voice: str):
    if voice not in KNOWN_VOICES:
        raise HTTPException(400, f"Unknown voice: {voice}")

    cache = state.PREVIEW_DIR / f"{voice}.wav"
    if not cache.exists():
        # First request for this voice — generate on demand (~10 s)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _generate_preview, voice, cache)

    if not cache.exists():
        raise HTTPException(500, "Preview generation failed")

    return FileResponse(str(cache), media_type="audio/wav",
                        headers={"Cache-Control": "public, max-age=86400"})
