# Project structure

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
├── ambience.py               Ollama LLM ambient-scene-cue detection (used by chapter_processor.py) — see ambience.md
├── mixing.py                  Ambience track generation/mixing, loudness normalization
├── voices.py                Voice catalog, VoiceMapper, preview synthesis
├── audio.py                  WAV/MP3 export, ffmpeg enhancement, reference-clip quality checks
├── engines/                 Out-of-process Higgs/Chatterbox workers — see engines.md
├── state.py                Shared app state (upload/output dirs, job registry)
└── schemas.py               Pydantic models

frontend/                The web UI (index.html, app.js, style.css), served by app.py

docs/                    Unrelated — the GitHub Pages marketing/landing site, not part of the running app

documentation/           Installation, engine setup, settings reference, and this file

tests/                   The one test folder: pytest suite (test_chunker.py), fixtures/
                          (ebooks + audio samples used by scripts/test_all_engines.py and the
                          *_test_epub.py generators), and output/ (gitignored — where dev/test
                          scripts write generated artifacts; never committed)

scripts/                 Dev helpers (generate_previews.py runs at Docker build time)
```

Reading order to understand a conversion request: `backend/routes/convert.py` → `backend/pipeline.py` (`convert_book`, the setup/merge orchestrator) → `backend/chapter_processor.py` (`ChapterProcessor.process`, the per-chapter work) → the leaf modules it calls (`epub_parser.py`, `attribution.py`, `ambience.py`, `voices.py`, `audio.py`, `chunking.py`).
