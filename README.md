# ScrollTone — EPUB to Audiobook

A self-hosted web app that converts EPUB books to audiobooks using [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M). Runs in Docker and is accessible from any device on your network.

---

## Quick Start

```bash
docker compose up --build
```

Then open **http://localhost:7860** in your browser.

> The first build takes several minutes — it downloads PyTorch, Kokoro-82M weights (~330 MB), and the spaCy language model so they are baked into the image and never re-downloaded at runtime.

---

## Docker RAM Requirements

**This is the most important setting to get right.**

Each parallel worker loads a full copy of the Kokoro model (~0.9 GB each). You must allocate enough RAM to Docker Desktop (Settings → Resources → Memory) **before** running the container.

| Parallel Workers | Docker RAM (minimum) | Recommended |
|-----------------|----------------------|-------------|
| 1 (default)     | 4 GB                 | 4 GB        |
| 2               | 6 GB                 | 6 GB        |
| 3               | 8 GB                 | 10 GB       |
| **4**           | **18 GB**            | **22 GB**   |

> **If you want to use 4 parallel workers, you must set Docker's memory resource to at least 22 GB.**
> Go to **Docker Desktop → Settings → Resources → Memory slider → set to 22 GB → Apply & Restart**.

The `docker-compose.yml` sets `mem_limit: 20g` by default. The app also auto-reduces workers at runtime if there isn't enough free RAM, and logs the reason.

Exit code **137** in the container logs always means OOM — increase Docker RAM and restart.

---

## Features

- All 19 Kokoro voices (American/British, male/female)
- 10 languages (English, Spanish, French, Hindi, Japanese, Chinese, and more)
- Speed control (0.5× – 2.5×) with preset buttons
- Parallel chapter processing (1–4 workers)
- Real-time progress log streamed to the browser via SSE
- Per-chapter download links appear as each chapter finishes
- Output as WAV or MP3 (with embedded cover art, title, author, and track tags)
- Optional merged full-audiobook file
- Transformer G2P mode for higher-quality pronunciation (slower)

---

## All Settings

| Setting | Default | Description |
|---------|---------|-------------|
| Voice | `af_heart` | Speaker voice |
| Language | `a` (American English) | Kokoro language/model code |
| Speed | `1.0×` | Playback speed (0.5 – 2.5) |
| Output Format | WAV | WAV or MP3 (MP3 embeds cover art & metadata) |
| MP3 Bitrate | 192 kbps | 128 / 192 / 256 / 320 kbps |
| Merge chapters | On | Produce a single combined file |
| Parallel Workers | `1` | How many chapters to process at once |
| Device | Auto | CPU, CUDA GPU, or auto-detect |
| Transformer G2P | Off | Better pronunciation, much slower, downloads 457 MB extra |
| Max Chunk Size | `500` chars | Max text per TTS call |
| Chapter Silence | `1.0` s | Gap between chapters in merged file |
| Min Chapter Length | `200` chars | Skip EPUB sections shorter than this |

---

## Changing the Memory Limit

Edit `docker-compose.yml` to match your Docker RAM allocation:

```yaml
mem_limit: 20g      # change this
memswap_limit: 22g  # keep 2g above mem_limit
```

Then restart:

```bash
docker compose down && docker compose up
```

---

## Running Locally (macOS Apple Silicon — M1/M2/M3)

**Step 1 — System dependencies**

```bash
brew install ffmpeg libsndfile
```

**Step 2 — Create a fresh conda environment**

```bash
conda create -n scrolltone python=3.11 -y
conda activate scrolltone
```

**Step 3 — Install PyTorch (M1 native with Metal/MPS support)**

```bash
pip install torch torchaudio
```

> Do **not** use `--index-url https://download.pytorch.org/whl/cpu` — that is the Linux CPU-only build. The standard pip package includes M1 Metal acceleration automatically.

**Step 4 — Install app dependencies**

```bash
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

**Step 5 — Run**

```bash
python app.py
```

Open **http://localhost:7860**

### M1 Notes

| Topic | Detail |
|-------|--------|
| First conversion | Kokoro downloads ~330 MB of weights to `~/.cache/huggingface` — one time only |
| Device setting | Leave on **Auto** — Kokoro uses Metal (MPS) automatically on Apple Silicon |
| Workers | 1 worker is plenty on M1 Max; MPS handles inference fast |
| Voice previews | First click per voice takes ~5–10 s to generate, then instant |
| MP3 output | Uses the ffmpeg installed in Step 1 — works natively |

---

## Running Without Docker (Linux / generic)

```bash
pip install -r requirements.txt
python app.py
# Open http://localhost:7860
```

---

## Output Files

Generated audio files are stored in a Docker volume (`scrolltone-outputs`) and persist across container restarts. Download them directly from the browser after conversion.
