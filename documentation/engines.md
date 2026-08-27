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

Either way: in ScrollTone, pick the engine from the **TTS Engine** dropdown and upload a reference voice clip (8-20 seconds of clean, single-speaker audio works best; 5s minimum). `chatterbox_workers > 1` in the UI runs that many ~6.6GB subprocesses *inside the same process's memory* — if running in Docker, `docker-compose.yml`'s `mem_limit`/`memswap_limit` are sized for one worker, so raise them proportionally if you increase that setting, and raise them further if you're also running Higgs (~12GB RAM per conversion) — see the comment at the top of `docker-compose.yml`.

**Trade-offs to know before choosing one:**
- **Higgs Audio V2** is licensed under Boson AI's Community License, not Apache/MIT — it requires attribution and a separate commercial license above 100,000 annual active users. It uses ~12GB RAM per conversion.
- **Chatterbox** is MIT-licensed, but this app **always runs it on CPU**, never MPS/CUDA, regardless of your Device setting — its autoregressive decoder has a confirmed, severe memory leak on Apple's MPS backend (grew past 78GB RSS in testing before being killed). CPU is ~6x slower than realtime but stable (~6.6GB RAM peak for a full chapter in testing). This is a hardcoded safety measure, not a preference.
- Both engines are narrator-only — **Multi-voice character attribution is Kokoro-only for now.**
- If a venv isn't set up, ScrollTone logs a clear setup hint in the conversion log rather than crashing.

See also [ambience.md](ambience.md) for the Ambient Sound feature, which also uses the local Ollama LLM.
