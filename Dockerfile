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

COPY requirements.txt .
RUN pip install -r requirements.txt psutil

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
COPY app.py .
COPY core/ core/
COPY routes/ routes/
COPY static/ static/
COPY templates/ templates/
COPY generate_previews.py .

# Pre-generate voice preview samples for all 20 voices (~10 MB, instant playback in UI)
RUN python generate_previews.py

RUN mkdir -p /tmp/tts_uploads /tmp/tts_outputs

EXPOSE 7860

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1"]
