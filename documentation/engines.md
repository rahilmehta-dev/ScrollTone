# TTS engines

## Multi-voice (speaker attribution) setup

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

> **Note:** as of this build, only **Chatterbox** is exposed as a selectable engine in the UI (the "Clone a Voice" flow goes straight to it — no engine picker). Higgs Audio V2 support is still fully implemented server-side (`backend/engines/higgs_synth.py`, `POST /convert` with `engine=higgs`, etc.) and works if called directly, it's just not offered as a choice in the frontend right now. See git history around 2026-09-08 if re-enabling it in the UI.

Kokoro is the default and recommended engine — fast, lightweight, and runs in the same process as the app. ScrollTone can also clone your own voice from a short audio clip using two alternative engines, each of which needs a **one-time, separate setup** (they're not installed by default, and are not bundled into the main Docker image, to keep it small).

`backend/engines/runner.py` always launches Higgs and Chatterbox as a subprocess in a dedicated venv at a hardcoded path (`.venv-higgs`, `.venv-chatterbox`) — this keeps their heavier, independently-pinned dependency stacks from having to coexist with each other or with the main app's dependencies.

### Docker (recommended)

Build the venv *inside* the running container, into a named volume so it survives restarts and stays out of the (slim, CPU-only-torch) main image:

```bash
# One-time setup — the backend container must be up so its named volumes
# (docker-compose.yml's higgs-venv / chatterbox-venv) exist, then build the
# venv into the running container:
docker compose up -d backend

# Higgs Audio V2
docker compose exec backend python -m venv /app/.venv-higgs
docker compose exec backend .venv-higgs/bin/pip install transformers torch torchaudio accelerate librosa soundfile

# Chatterbox
docker compose exec backend python -m venv /app/.venv-chatterbox
docker compose exec backend .venv-chatterbox/bin/pip install chatterbox-tts torch torchaudio
```

This downloads the engine's packages (~1-2GB for Chatterbox, more for Higgs's transformers/accelerate/librosa stack) into the named volume, separate from the main image; model weights (~3GB Chatterbox / ~11.8GB Higgs) still download on first actual use. Restart the backend afterwards (`docker compose restart backend`) so it picks up the new venv.

A plain host-path bind mount won't work here: a venv built on macOS/Windows has non-Linux binaries (torch, transformers/chatterbox-tts) that can't execute inside the Linux container — you'd get an exec error, not just "not found". That's why these are named volumes built from inside the container instead.

### Native (contributor / local dev) install

If you're running ScrollTone natively rather than via Docker (see [installation.md](installation.md#local-development-for-contributors)):

```bash
# Higgs Audio V2 — ~11.8GB model download, ~1x realtime, ~12GB RAM
python -m venv .venv-higgs
.venv-higgs/bin/pip install transformers torch torchaudio accelerate librosa soundfile

# Chatterbox — ~3GB model download, CPU-only, ~6x slower than realtime
python -m venv .venv-chatterbox
.venv-chatterbox/bin/pip install chatterbox-tts torch torchaudio
```

`requirements.txt` lists these same packages too (in its "optional" sections), but installing that whole file into your main venv is not enough to enable the engines — they specifically need their own `.venv-higgs` / `.venv-chatterbox` at the paths above, for the isolation reason described above.

---

Either way: pick the engine from the **TTS Engine** dropdown (Chatterbox only, in this build — see the note above) and upload a reference voice clip (8-20 seconds of clean, single-speaker audio works best; 5s minimum). `chatterbox_workers > 1` (up to 16) in the UI runs that many subprocesses concurrently, ~6.6GB each, CPU-only, well-tested at that count — *inside the same process's memory*; if running in Docker, `docker-compose.yml`'s `mem_limit`/`memswap_limit` are sized for one worker, so raise them proportionally if you increase that setting, and raise them further if you're also running Higgs directly via the API (`higgs_workers`, up to 4, ~12GB each — capped much lower than Chatterbox's because they all contend for the same GPU) — see the comment at the top of `docker-compose.yml`.

**Trade-offs to know before choosing one:**
- **Higgs Audio V2** is licensed under Boson AI's Community License, not Apache/MIT — it requires attribution and a separate commercial license above 100,000 annual active users. It uses ~12GB RAM per conversion.
- **Chatterbox** is MIT-licensed, but this app **always runs it on CPU**, never MPS/CUDA, regardless of your Device setting — its autoregressive decoder has a confirmed, severe memory leak on Apple's MPS backend (grew past 78GB RSS in testing before being killed). CPU is ~6x slower than realtime but stable (~6.6GB RAM peak for a full chapter in testing). This is a hardcoded safety measure, not a preference.
- Both engines are narrator-only — **Multi-voice character attribution is Kokoro-only for now.**
- If a venv isn't set up, ScrollTone logs a clear setup hint in the conversion log rather than crashing.

---

## Auto-tune this voice

Once a reference clip is uploaded for Chatterbox, an **Auto-tune this voice** button appears next to it (opt-in — it's not automatic on upload, since it takes real time — 20 candidates is easily an overnight job). It:

1. Samples **20 parameter combos by default** (3-30 configurable) via Latin Hypercube Sampling, seeded (**411 by default**) so a rerun reproduces the same candidates instead of a fresh random set each time — over `cfg_weight`/`temperature` (`exaggeration` is excluded — it's baked into `prepare_conditionals()`, computed once per job to avoid a known memory-growth issue, so it can't safely vary per-candidate), narrowed to the region actually associated with natural-sounding output rather than the sliders' full technical range (see `PARAM_RANGES` in `backend/voice_tuning.py` for the exact bounds and reasoning).
2. Synthesizes the same short sample text (`PREVIEW_JOB_TEXT`) with each combo against your reference clip, in parallel across `chatterbox_workers`.
3. Scores each candidate with **DNSMOS** (Microsoft's `speechmos` package) — an offline, no-reference audio-quality model — **the moment its wav is written**, not after the whole batch finishes. It's tuned for noise/artifact evaluation more than pure TTS naturalness (unlike UTMOS, which is trained specifically on TTS naturalness ratings but has no cleanly-packaged pip install), but on clean synthesized speech it still tracks clean-and-natural vs. distorted-and-robotic reasonably well.
4. Shows a live, reshuffling "best so far" as candidates complete (they don't finish in index order across parallel workers, so the leader can change candidate to candidate), then ranks the final set, auto-fills the top-scoring combo into Advanced Settings, and lets you play/compare every candidate and override the pick by ear if it doesn't match your taste.

Every log line the job produces is timestamped/duration-stamped in the text itself (started/finished wall-clock time, how long each candidate batch and — in Voice Lab — each voice took) — useful for a long unattended run checked the next morning rather than watched live.

**Voice Lab** (`/voice-lab.html`, linked from the Auto-tune section) runs this same search independently across **several reference clips in one job** — upload 1-8 candidate voice clips, get each one's ranked results and best config as they finish, one at a time, instead of running Auto-tune once per clip by hand. Same seed by default across every voice in the batch, so scores are directly comparable voice-to-voice (voice A's candidate #7 and voice B's candidate #7 were tested with the identical parameter combo). See `backend/voice_tuning.py`'s `run_batch_autotune_job`.

See `backend/voice_tuning.py` for the implementation and full reasoning (including why LHS over a full grid, random sampling, or Bayesian optimization for this specific budget/dimensionality).

**Setup**: `speechmos`, `librosa`, `onnxruntime`, and `scipy` need to be installed in the **main** app environment (not `.venv-higgs`/`.venv-chatterbox` — scoring runs in the main process). Native installs following `requirements.txt` already get these. Running in Docker: since the default image is kept slim, install them into the running container once —
```bash
docker compose exec backend pip install speechmos librosa onnxruntime scipy
docker compose restart backend
```
- Both can go a while between log lines — loading the model, or a single chunk's generate() call, can each take a minute or more with no output. A `[higgs]`/`[chatterbox]` heartbeat line appears in the conversion log every 10s during those stretches (elapsed time, current stage, RSS) so a long wait doesn't look like a hang.

See also [ambience.md](ambience.md) for the Ambient Sound feature, which also uses the local Ollama LLM.
