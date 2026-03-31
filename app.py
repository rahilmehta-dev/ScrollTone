#!/usr/bin/env python3
"""
ScrollTone — EPUB to Audiobook
FastAPI application entry point.

Run locally:   python app.py
Docker:        CMD in Dockerfile points here
"""
import warnings
# Suppress noisy but harmless warnings from PyTorch/Kokoro dependencies
warnings.filterwarnings("ignore", message="dropout option adds dropout after all but last")
warnings.filterwarnings("ignore", message=r"`torch\.nn\.utils\.weight_norm` is deprecated")
warnings.filterwarnings("ignore", message="An output with one or more elements was resized")
warnings.filterwarnings("ignore", message=r"`torch\.jit\.script` is deprecated")
warnings.filterwarnings("ignore", message="open_text is deprecated")

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import uvicorn

import core.state as state        # initialises dirs on import
from routes import convert, preview, ui

app = FastAPI(title="ScrollTone")

# Serve static assets (CSS, JS) from /static
app.mount(
    "/static",
    StaticFiles(directory=str(state.BASE_DIR / "static")),
    name="static",
)

app.include_router(ui.router)
app.include_router(preview.router)
app.include_router(convert.router)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860, reload=False)
