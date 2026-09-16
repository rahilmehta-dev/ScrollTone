FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/root/.cache/huggingface

RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# PyTorch CPU-only (~500 MB vs ~2 GB for GPU build)
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

# requirements.txt (repo root) also lists the optional Higgs/Chatterbox engine
# deps in its "optional" sections — those are deliberately NOT installed here
# so the image stays small and keeps the CPU-only torch build above. Install
# the core list by name instead of `pip install -r requirements.txt` for that
# reason; keep this list in sync with requirements.txt's "Core" section.
RUN pip install \
    fastapi psutil pydub mutagen "uvicorn[standard]" python-multipart \
    jinja2 kokoro espeakng-loader soundfile ebooklib beautifulsoup4 numpy

# ── Pre-download models into the image layer ──────────────────────────────────
# This runs once at build time so the container never downloads at runtime.
# The spaCy small model is needed by Kokoro for English G2P.
RUN python -m spacy download en_core_web_sm

# Warm up KPipeline — downloads & caches Kokoro-82M weights (~330 MB)
RUN python -c "\
from kokoro import KPipeline; \
p = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M'); \
print('Kokoro model cached OK'); \
del p"

# ── Copy application ──────────────────────────────────────────────────────────
# Voice preview clips are no longer pre-baked at build time (that step —
# scripts/generate_previews.py — took ~5-10 min re-synthesizing all 20
# voices on every build). Previews now generate lazily on first request per
# voice instead, cached to PREVIEW_DIR for the rest of that container's
# lifetime — see the fallback in backend/routes/preview.py's GET
# /preview/{voice}. Run scripts/generate_previews.py manually inside the
# container if you want instant first-click playback for every voice.
COPY app.py .
COPY backend/ backend/
COPY frontend/ frontend/
COPY scripts/ scripts/

RUN mkdir -p /tmp/tts_uploads /tmp/tts_outputs

EXPOSE 7860

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1"]
