#!/usr/bin/env python3
"""
ScrollTone — EPUB to Audiobook
FastAPI application entry point.

Run locally:   python app.py
Docker:        CMD in Dockerfile points here
"""
import os
import warnings
# Suppress noisy but harmless warnings from PyTorch/Kokoro dependencies
warnings.filterwarnings("ignore", message="dropout option adds dropout after all but last")
warnings.filterwarnings("ignore", message=r"`torch\.nn\.utils\.weight_norm` is deprecated")
warnings.filterwarnings("ignore", message="An output with one or more elements was resized")
warnings.filterwarnings("ignore", message=r"`torch\.jit\.script` is deprecated")
warnings.filterwarnings("ignore", message="open_text is deprecated")

# espeakng-loader's bundled libespeak-ng was built with a data path baked in
# from its CI build machine. phonemizer's espeak_Initialize() call passes no
# path override, so the C library falls back to that (nonexistent) baked-in
# path and hard-exits the whole process. ESPEAK_DATA_PATH makes it use the
# data files actually shipped in the wheel instead.
import espeakng_loader
os.environ.setdefault("ESPEAK_DATA_PATH", espeakng_loader.get_data_path())

from pathlib import Path

from fastapi import FastAPI
from fastapi import APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn

import backend.state as state        # initialises dirs on import
from backend.routes import autotune, chapters, convert, preview, ui

app = FastAPI(title="ScrollTone")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_static_cache(request, call_next):
    """Force revalidation (ETag/If-None-Match) on every frontend asset
    request instead of letting browsers fall back to heuristic caching.
    StaticFiles sets Last-Modified/ETag but no Cache-Control, so without
    this a browser can silently keep serving index.html/app.js/style.css
    from a build several redesigns ago after a `git pull`."""
    response = await call_next(request)
    if request.method == "GET" and not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response

api_router = APIRouter(prefix="/api")
api_router.include_router(ui.router)
api_router.include_router(preview.router)
api_router.include_router(autotune.router)
api_router.include_router(chapters.router)
api_router.include_router(convert.router)
app.include_router(api_router)

# Serve the frontend (index.html, style.css, app.js) when running standalone
# via `python app.py` — Docker's nginx container handles this in production.
app.mount(
    "/",
    StaticFiles(directory=str(Path(__file__).parent / "frontend"), html=True),
    name="frontend",
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860, reload=False)
