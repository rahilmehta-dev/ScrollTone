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

## Features

- 19 Kokoro voices — American & British, male & female
- Speed control (0.5× – 2.5×) with preset buttons
- Parallel chapter processing (1–4 workers per book)
- Batch mode — upload multiple EPUBs, processed sequentially to keep RAM usage predictable
- Chapter selection — pick specific chapters before converting
- Per-chapter live progress grid in the UI
- Real-time status streamed to the browser via SSE
- Output as WAV or MP3 (MP3 embeds cover art, title, author, and track tags)
- Optional merged full-audiobook file
- Output saved directly to `audiobook_output/BookTitle/` — no UUID folders
- Navigate back to Settings mid-conversion without losing progress (live banner to return)
- Transformer G2P — better pronunciation for unusual words and names (slower, downloads 457 MB extra)
- **Enhance Audio** — optional ffmpeg post-processing: compression + 200 Hz warmth boost + 8 kHz harshness cut
- **Multi-voice (Speaker Attribution)** — local LLM via Ollama detects dialogue speakers and assigns a unique Kokoro voice to each character automatically

---

## Multi-voice Setup

Multi-voice uses a local LLM to detect who is speaking each dialogue line and assigns different voices to different characters. The narrator uses your chosen voice; characters are assigned gender-matched voices automatically.

**Requirements:** [Ollama](https://ollama.com) running locally.

```bash
# Install Ollama (macOS)
brew install ollama

# Start Ollama
ollama serve

# Pull a model (pick one)
ollama pull phi3:mini       # ~2 GB RAM — recommended
ollama pull llama3.2:1b     # ~1 GB RAM — fastest
ollama pull llama3.2:3b     # ~2.5 GB RAM — best quality
```

Then in ScrollTone: enable **Multi-voice** in Advanced Settings, set the Ollama URL to `http://localhost:11434`, and pick your model. The LLM Attribution card in the output panel shows each character being assigned a voice in real time.

> If Ollama is not running, ScrollTone logs the error and automatically falls back to single-voice — it will not crash.

> **Running in Docker?** Set the Ollama URL to `http://host.docker.internal:11434` — `localhost` inside a container refers to the container itself, not your Mac. ScrollTone detects Docker and updates the default automatically.

---

## Docker RAM Requirements

Each parallel worker loads a full copy of the Kokoro model (~0.9 GB each). You must allocate enough RAM to Docker Desktop (Settings → Resources → Memory) **before** running the container.

| Parallel Workers | Docker RAM (minimum) | Recommended |
|-----------------|----------------------|-------------|
| 1 (default)     | 4 GB                 | 4 GB        |
| 2               | 6 GB                 | 6 GB        |
| 3               | 8 GB                 | 10 GB       |
| **4**           | **18 GB**            | **22 GB**   |

> Go to **Docker Desktop → Settings → Resources → Memory slider → set to 22 GB → Apply & Restart**.

The `docker-compose.yml` sets `mem_limit: 20g` by default. The app also auto-reduces workers at runtime if there isn't enough free RAM and logs the reason.

Exit code **137** in the container logs always means OOM — increase Docker RAM and restart.

When converting multiple EPUBs, books are processed **sequentially** — each book uses the full worker budget, then releases all pipelines before the next book starts. This keeps peak RAM predictable regardless of batch size.

---

## All Settings

| Setting | Default | Description |
|---------|---------|-------------|
| Narrator Voice | `af_heart` | Voice used for narration (and all speech in single-voice mode) |
| Speed | `1.0×` | Playback speed (0.5 – 2.5) |
| Output Format | WAV | WAV or MP3 (MP3 embeds cover art & metadata) |
| MP3 Bitrate | 192 kbps | 128 / 192 / 256 / 320 kbps |
| Merge Chapters | On | Produce a single combined file in addition to per-chapter files |
| Parallel Workers | `1` | Chapters processed simultaneously per book (1–4) |
| Device | Auto | CPU, CUDA GPU, or MPS (Apple Silicon) — auto-detected |
| Transformer G2P | Off | Better pronunciation, much slower, downloads 457 MB extra on first use |
| Enhance Audio | Off | ffmpeg: compression + 200 Hz warmth + 8 kHz cut. Requires `ffmpeg` on PATH |
| Multi-voice | Off | LLM speaker attribution via Ollama. Requires Ollama running locally |
| Ollama URL | `http://localhost:11434` | URL of your local Ollama instance |
| Ollama Model | `phi3:mini` | Model used for speaker attribution |
| Max Chunk Size | `500` chars | Max characters per TTS synthesis call |
| Chapter Silence | `1.0` s | Silence gap between chapters in merged file |
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
| Workers | 1–2 workers recommended on M1/M2; MPS handles inference fast |
| Voice previews | First click per voice takes ~5–10 s to generate, then instant |
| MP3 output | Uses the ffmpeg installed in Step 1 — works natively |
| Multi-voice | Run `ollama serve` in a separate terminal before starting ScrollTone |

---

## Running Without Docker (Linux / generic)

**Step 1 — System dependencies**

```bash
sudo apt install ffmpeg libsndfile1   # Debian / Ubuntu
sudo dnf install ffmpeg libsndfile    # Fedora / RHEL
```

**Step 2 — Install app dependencies**

```bash
pip install -r requirements.txt
python app.py
# Open http://localhost:7860
```

---

## Output Files

Audio files are saved to `audiobook_output/BookTitle/` next to the app (or your chosen output folder). Each book gets its own subfolder named after the book title. Files persist across restarts and can be downloaded directly from the browser during or after conversion.
