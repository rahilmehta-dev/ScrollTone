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
from backend.routes import convert, preview, ui

app = FastAPI(title="ScrollTone")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

api_router = APIRouter(prefix="/api")
api_router.include_router(ui.router)
api_router.include_router(preview.router)
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
