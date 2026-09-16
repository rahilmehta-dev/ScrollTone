# ScrollTone — EPUB to Audiobook

A self-hosted web app that converts EPUB books to audiobooks using [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M). **Docker is the primary way to run it** — no Python setup, no `requirements.txt`, no virtual environments. Accessible from any device on your network.

---

## Quick Start

```bash
docker compose up --build
```

Then open **http://localhost:7860** in your browser. That's it — everything else (PyTorch, Kokoro weights, spaCy) is baked into the image at build time.

> The first build takes several minutes for that reason. Subsequent starts are fast.

Want to modify ScrollTone's code instead of just running it? See [documentation/installation.md#local-development-for-contributors](documentation/installation.md#local-development-for-contributors) for the native Python setup (`requirements.txt`, venvs) — that path is for contributors, not required for normal use.

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
- **Ambient Sound** — local LLM detects scene cues (rain, wind, fire, crowd, …) and mixes a quiet procedurally-generated background bed under the narration (see [documentation/ambience.md](documentation/ambience.md))
- **Voice cloning** — the optional Chatterbox engine clones a voice from an uploaded reference clip (see [documentation/engines.md](documentation/engines.md))
- **Auto-tune this voice / Voice Lab** — automatically searches Chatterbox's sampling parameters against your reference clip and scores each candidate with an offline audio-quality model, so you don't have to A/B settings by ear; Voice Lab runs the same search across several candidate clips at once and ranks them (see [documentation/engines.md](documentation/engines.md#auto-tune-this-voice))

---

## Documentation

| Doc | Covers |
|---|---|
| [documentation/installation.md](documentation/installation.md) | Docker setup + RAM tuning (primary path); native macOS/Linux setup for contributors |
| [documentation/engines.md](documentation/engines.md) | Multi-voice (Ollama) setup, the Chatterbox voice-cloning engine (+ Higgs Audio V2 server-side), and Auto-tune / Voice Lab |
| [documentation/ambience.md](documentation/ambience.md) | How the procedurally-generated ambient sound beds work |
| [documentation/settings.md](documentation/settings.md) | Every setting in the UI, what it does, and its default |
| [documentation/architecture.md](documentation/architecture.md) | Project structure and where to start reading the code |

---

## Output Files

Audio files are saved to `audiobook_output/BookTitle/` next to the app (or your chosen output folder). Each book gets its own subfolder named after the book title. Files persist across restarts and can be downloaded directly from the browser during or after conversion.
