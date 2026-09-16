# Installation

Docker is the primary, recommended way to run ScrollTone — you don't need Python, `pip`, or `requirements.txt` for this. Those only come into play if you want to [contribute to ScrollTone's code](#local-development-for-contributors).

## Using ScrollTone (Docker)

```bash
docker compose up --build
```

Then open **http://localhost:7860** in your browser.

> The first build takes several minutes — it downloads PyTorch, Kokoro-82M weights (~330 MB), and the spaCy language model so they are baked into the image and never re-downloaded at runtime.

Generated audiobooks are written to `audiobook_output/BookTitle/` on the host (bind-mounted from the container's `/tmp/tts_outputs` — see `docker-compose.yml`). Each book gets its own subfolder named after the book title. Files persist across restarts and can be downloaded directly from the browser during or after conversion.

Want voice cloning (Higgs Audio V2 / Chatterbox) or multi-voice/ambient sound (Ollama)? Those are optional add-ons on top of the Docker setup — see [engines.md](engines.md).

### NVIDIA GPU passthrough (Linux)

By default the container has no GPU access at all — this is true even on macOS, since Docker Desktop cannot pass Apple Silicon's GPU through to a Linux container regardless of any ScrollTone setting. On Linux with an NVIDIA GPU and the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) installed, layer on the GPU overlay instead of editing `docker-compose.yml` directly:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build
```

This is opt-in on purpose — the GPU reservation it adds would make `docker compose up` refuse to start on any machine without a working NVIDIA runtime, so it's kept out of the default file entirely. It unlocks CUDA for Higgs Audio V2 (already supports cuda/mps/cpu with fallback). Chatterbox stays CPU-only regardless — that's a hardcoded safety measure, not something this overlay changes; see [engines.md](engines.md).

### Docker RAM requirements

ScrollTone loads a single Kokoro model per job (~1.5 GB). Allocate at least **4 GB** to Docker Desktop (Settings → Resources → Memory) before running the container — more if you also set up Chatterbox (~6.6 GB per worker) or Higgs (~12 GB per conversion); see [engines.md](engines.md).

Exit code **137** in the container logs always means OOM — increase Docker RAM and restart.

When converting multiple EPUBs, books are processed **sequentially** — one book's pipeline is fully released before the next book starts. This keeps peak RAM predictable regardless of batch size.

### Changing the memory limit

Edit `docker-compose.yml` to match your Docker RAM allocation:

```yaml
mem_limit: 8g        # change this
memswap_limit: 10g   # keep 2g above mem_limit
```

Then restart:

```bash
docker compose down && docker compose up
```

### Updating

```bash
git pull
docker compose up --build
```

### Stopping / removing

```bash
docker compose down
```

This stops both containers. Generated audiobooks in `audiobook_output/` and any Higgs/Chatterbox venvs in their named volumes are untouched — add `-v` to also delete those named volumes (audiobooks are safe either way, since they live in a host bind mount, not a volume).

---

## Local development (for contributors)

Everything below is for people who want to modify ScrollTone's code and run it outside Docker to iterate faster — not needed to just use the app. `requirements.txt` and the per-engine venvs described here have no effect on the Docker image; the Dockerfile installs its own pinned core package list independently (see its comments) to keep the image small.

### macOS (Apple Silicon — M1/M2/M3)

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

#### M1 notes

| Topic | Detail |
|-------|--------|
| First conversion | Kokoro downloads ~330 MB of weights to `~/.cache/huggingface` — one time only |
| Device setting | Leave on **Auto** — Kokoro uses Metal (MPS) automatically on Apple Silicon |
| Voice previews | First click per voice takes ~5–10 s to generate, then instant |
| MP3 output | Uses the ffmpeg installed in Step 1 — works natively |
| Multi-voice | Run `ollama serve` in a separate terminal before starting ScrollTone |

### Linux / generic

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

`requirements.txt` also includes the optional Higgs Audio V2 / Chatterbox engine dependencies — see [engines.md](engines.md) for why they need their own venvs (native or Docker) and how to set those up.
