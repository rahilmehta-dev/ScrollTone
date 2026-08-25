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
- **Ambient Sound** — local LLM detects scene cues (rain, wind, fire, crowd, …) and mixes a quiet procedurally-generated background bed under the narration (see [AMBIENCE_SOUNDS.md](AMBIENCE_SOUNDS.md))
- **Voice cloning** — optional Higgs Audio V2 / Chatterbox engines clone a voice from an uploaded reference clip (see "Optional" section below)

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

## Optional: Higgs Audio V2 / Chatterbox engines

Kokoro is the default and recommended engine — fast, lightweight, and runs in the same process as the app. ScrollTone can also clone your own voice from a short audio clip using two alternative engines, each of which needs a **one-time, separate setup** (they're not installed by default, and are not bundled into the Docker image):

```bash
# Higgs Audio V2 — ~11.8GB model download, ~1x realtime, ~12GB RAM
python -m venv .venv-higgs
.venv-higgs/bin/pip install -r requirements-higgs.txt

# Chatterbox — ~3GB model download, CPU-only, ~6x slower than realtime
python -m venv .venv-chatterbox
.venv-chatterbox/bin/pip install -r requirements-chatterbox.txt
```

Then in ScrollTone: pick the engine from the **TTS Engine** dropdown and upload a reference voice clip (8-20 seconds of clean, single-speaker audio works best; 5s minimum). Model weights download automatically on first use of that engine.

**Trade-offs to know before choosing one:**
- **Higgs Audio V2** is licensed under Boson AI's Community License, not Apache/MIT — it requires attribution and a separate commercial license above 100,000 annual active users. It uses ~12GB RAM per conversion.
- **Chatterbox** is MIT-licensed, but this app **always runs it on CPU**, never MPS/CUDA, regardless of your Device setting — its autoregressive decoder has a confirmed, severe memory leak on Apple's MPS backend (grew past 78GB RSS in testing before being killed). CPU is ~6x slower than realtime but stable (~6.6GB RAM peak for a full chapter in testing). This is a hardcoded safety measure, not a preference.
- Both engines are narrator-only — **Multi-voice character attribution is Kokoro-only for now.**
- If a venv isn't set up, ScrollTone logs a clear setup hint in the conversion log rather than crashing.

---

## Docker RAM Requirements

ScrollTone loads a single Kokoro model per job (~1.5 GB). Allocate at least **4 GB** to Docker Desktop (Settings → Resources → Memory) before running the container.

Exit code **137** in the container logs always means OOM — increase Docker RAM and restart.

When converting multiple EPUBs, books are processed **sequentially** — one book's pipeline is fully released before the next book starts. This keeps peak RAM predictable regardless of batch size.

---

## All Settings

| Setting | Default | Description |
|---------|---------|-------------|
| TTS Engine | Kokoro | Kokoro (built-in voices) or Higgs Audio V2 / Chatterbox (clone a voice from an uploaded clip) — see "Optional: Higgs Audio V2 / Chatterbox engines" |
| Narrator Voice | `af_heart` | Voice used for narration (and all speech in single-voice mode). Kokoro only. |
| Speed | `1.0×` | Playback speed (0.5 – 2.5) |
| Output Format | WAV | WAV or MP3 (MP3 embeds cover art & metadata) |
| MP3 Bitrate | 192 kbps | 128 / 192 / 256 / 320 kbps |
| Merge Chapters | On | Produce a single combined file in addition to per-chapter files |
| Device | Auto | CPU, CUDA GPU, or MPS (Apple Silicon) — auto-detected |
| Transformer G2P | Off | Better pronunciation, much slower, downloads 457 MB extra on first use |
| Enhance Audio | Off | ffmpeg: compression + 200 Hz warmth + 8 kHz cut. Requires `ffmpeg` on PATH |
| Multi-voice | Off | LLM speaker attribution via Ollama. Requires Ollama running locally. Kokoro only |
| Ambient Sound | Off | LLM scene-cue detection (rain, wind, ocean, fire, forest, crowd) via Ollama, mixed quietly under narration. Loops are procedurally generated, not third-party recordings — see [AMBIENCE_SOUNDS.md](AMBIENCE_SOUNDS.md) |
| Ollama URL | `http://localhost:11434` | URL of your local Ollama instance (shared by Multi-voice and Ambient Sound) |
| Ollama Model | `phi3:mini` | Model used for speaker attribution / scene-cue detection |
| Max Chunk Size | `500` chars | Max characters per TTS synthesis call |
| Chapter Silence | `1.0` s | Silence gap between chapters in merged file |
| Min Chapter Length | `200` chars | Skip EPUB sections shorter than this |
| Chatterbox Speed | `1.0×` | Post-hoc ffmpeg time-stretch — Chatterbox has no native rate control |
| Chatterbox Parallel Workers | `1` | Concurrent Chatterbox subprocesses per chapter (RAM scales ~linearly per worker) |
| Chatterbox CFG Weight | `0.3` | Lower = more expressive, less tied to the reference clip's exact delivery |
| Chatterbox Exaggeration | `0.7` | Emotional intensity of the delivery (~0.5 is neutral) |
| Chatterbox Temperature | `0.8` | Sampling randomness — higher adds natural sentence-to-sentence variation |
| Chatterbox Breathing Pauses | On | Splices a short synthesized breath between chunk boundaries instead of dead silence |

---

## Changing the Memory Limit

Edit `docker-compose.yml` to match your Docker RAM allocation:

```yaml
mem_limit: 4g       # change this
memswap_limit: 6g   # keep 2g above mem_limit
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

---

## Project Structure

```
app.py                  Entry point — boots the FastAPI backend and serves frontend/ at "/"

backend/
├── routes/                HTTP layer — what the browser calls
│   ├── convert.py           POST /api/convert — job lifecycle: /convert, /stream, /stop, /download
│   ├── chapters.py           POST /api/chapters — chapter list for the pre-convert selection UI
│   ├── clone_test.py         POST /api/clone-test — short voice-clone preview (Higgs/Chatterbox)
│   ├── preview.py            GET  /api/preview/{voice}
│   └── ui.py                  /api/config, /pick-folder, /shutdown
├── pipeline.py            The orchestrator — reads the EPUB, sets up the run, drives per-chapter synthesis
├── chapter_processor.py    Per-chapter synthesis + file writing (used by pipeline.py)
├── job_events.py            SSE event emitter used by pipeline.py/chapter_processor.py
├── chunking.py               Text chunking + Chatterbox breath-splicing (used by chapter_processor.py)
├── epub_parser.py         EPUB chapter/metadata extraction (used by pipeline.py)
├── attribution.py          Ollama LLM speaker attribution for multi-voice (used by chapter_processor.py)
├── ambience.py               Ollama LLM ambient-scene-cue detection (used by chapter_processor.py)
├── mixing.py                  Ambience track generation/mixing, loudness normalization
├── voices.py                Voice catalog, VoiceMapper, preview synthesis
├── audio.py                  WAV/MP3 export, ffmpeg enhancement, reference-clip quality checks
├── engines/                 Out-of-process Higgs/Chatterbox workers — see "Optional" section above
├── state.py                Shared app state (upload/output dirs, job registry)
└── schemas.py               Pydantic models

frontend/                The web UI (index.html, app.js, style.css), served by app.py

docs/                    Unrelated — the GitHub Pages marketing/landing site, not part of the running app

tests/                   pytest suite (test_chunker.py) plus fixtures/ (ebooks + audio samples)
                          used by scripts/test_all_engines.py and the *_test_epub.py generators

scripts/                 Dev helpers (generate_previews.py runs at Docker build time)
```

Reading order to understand a conversion request: `backend/routes/convert.py` → `backend/pipeline.py` (`convert_book`, the setup/merge orchestrator) → `backend/chapter_processor.py` (`ChapterProcessor.process`, the per-chapter work) → the leaf modules it calls (`epub_parser.py`, `attribution.py`, `ambience.py`, `voices.py`, `audio.py`, `chunking.py`).
