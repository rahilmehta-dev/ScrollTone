# Project structure

```
app.py                  Entry point — boots the FastAPI backend and serves frontend/ at "/"

backend/
├── routes/                HTTP layer — what the browser calls
│   ├── convert.py           POST /api/convert — job lifecycle: /convert, /stream, /stop, /download
│   ├── chapters.py           POST /api/chapters — chapter list for the pre-convert selection UI
│   ├── preview.py            GET /api/preview/{voice}, POST /api/preview-job — Preview & Tweak
│   ├── autotune.py           POST /api/autotune-job, /api/autotune-batch-job — see engines.md's Auto-tune section
│   └── ui.py                  /api/config, /pick-folder, /shutdown
├── pipeline.py            The orchestrator — reads the EPUB, sets up the run, drives per-chapter synthesis
├── chapter_processor.py    Per-chapter synthesis + file writing (used by pipeline.py)
├── voice_tuning.py         Auto-tune / Voice Lab: LHS parameter search + DNSMOS scoring (used by routes/autotune.py)
├── job_events.py            SSE event emitter used by pipeline.py/chapter_processor.py/voice_tuning.py
├── chunking.py               Text chunking + Chatterbox breath-splicing (used by chapter_processor.py)
├── epub_parser.py         EPUB chapter/metadata extraction (used by pipeline.py)
├── attribution.py          Ollama LLM speaker attribution for multi-voice (used by chapter_processor.py)
├── ambience.py               Ollama LLM ambient-scene-cue detection (used by chapter_processor.py) — see ambience.md
├── mixing.py                  Ambience track generation/mixing, loudness normalization
├── voices.py                Voice catalog, VoiceMapper, preview synthesis
├── audio.py                  WAV/MP3 export, ffmpeg enhancement, reference-clip quality checks
├── engines/                 Out-of-process Higgs/Chatterbox workers + the runner.py dispatcher both
│                             chapter_processor.py and voice_tuning.py call into — see engines.md
├── state.py                Shared app state (upload/output dirs, job registry)
└── schemas.py               Pydantic models

frontend/                The web UI (index.html, app.js, style.css) served at "/", plus the standalone
                          Voice Lab tool page (voice-lab.html, voice-lab.js) at "/voice-lab.html" —
                          both served as static files by app.py

docs/                    Unrelated — the GitHub Pages marketing/landing site, not part of the running app

documentation/           Installation, engine setup, settings reference, and this file

tests/                   The one test folder: pytest suite (test_chunker.py), fixtures/
                          (ebooks + audio samples used by scripts/test_all_engines.py and the
                          *_test_epub.py generators), and output/ (gitignored — where dev/test
                          scripts write generated artifacts; never committed)

scripts/                 Dev helpers (generate_previews.py — optional, manual pre-bake of voice preview clips; previews otherwise generate lazily on first request)
```

Reading order to understand a conversion request: `backend/routes/convert.py` → `backend/pipeline.py` (`convert_book`, the setup/merge orchestrator) → `backend/chapter_processor.py` (`ChapterProcessor.process`, the per-chapter work) → the leaf modules it calls (`epub_parser.py`, `attribution.py`, `ambience.py`, `voices.py`, `audio.py`, `chunking.py`).

Reading order to understand Auto-tune / Voice Lab: `backend/routes/autotune.py` (the two POST endpoints) → `backend/voice_tuning.py` (`run_autotune_job` for one clip, `run_batch_autotune_job` for several, both built on the shared `_autotune_one_voice` search core) → `backend/engines/runner.py`'s `on_chunk_done` callback, which is what lets a candidate get scored the moment its wav is written instead of waiting for the whole batch.
