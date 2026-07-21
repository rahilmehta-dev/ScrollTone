"""
Shared mutable state — imported by all other modules.
Only this file should define these objects; everything else imports from here.
"""
import os
import threading
from pathlib import Path

BASE_DIR    = Path(__file__).parent.parent          # TTS/
UPLOAD_DIR  = Path("/tmp/tts_uploads")
OUTPUT_DIR  = Path("/tmp/tts_outputs")
PREVIEW_DIR = Path(os.environ.get("PREVIEW_DIR", str(BASE_DIR / "previews")))

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

# job_id → {id, status, queue, stop_event, out_dir, files}
jobs: dict = {}

# lang_code → KPipeline (lazy, one per language, shared across preview requests)
_preview_pipeline: dict = {}
_preview_lock = threading.Lock()
