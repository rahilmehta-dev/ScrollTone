"""
Phase 5 verification driver: runs the real convert_book() pipeline end to
end (real Kokoro synthesis, real Ollama attribution + ambience detection,
real mixing) against test_ambience.epub and reports where the output landed.

Run:
    python scripts/run_ambience_test.py
"""
import asyncio
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import backend.state as state
from backend.pipeline import convert_book

EPUB_PATH = Path(__file__).parent.parent / "test_ambience.epub"
OUT_DIR   = Path(__file__).parent.parent / "test_ambience_output"


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    settings = {
        "epub":            str(EPUB_PATH),
        "filename":        EPUB_PATH.name,
        "out_dir":         str(OUT_DIR),
        "voice":           "af_heart",
        "lang_code":       "a",
        "speed":           1.0,
        "device":          "cpu",
        "trf":             False,
        "merge":           True,
        "chunk_size":      500,
        "silence":         1.0,
        "min_ch_len":      50,
        "output_format":   "wav",
        "bitrate":         192,
        "chapter_indices": None,
        "enhance":         False,
        "multi_voice":     True,
        "ambience":        True,
        "ollama_url":      "http://localhost:11434",
        "ollama_model":    "gemma4:31b-mlx",
    }

    loop = asyncio.new_event_loop()

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()

    job_state = {
        "id": "test", "batch_id": "test", "book_title": "The Storm",
        "status": "running",
        "queue": asyncio.Queue(),
        "stop_event": threading.Event(),
        "out_dir": str(OUT_DIR),
        "files": [],
    }
    state.jobs["test"] = job_state

    async def drain_queue():
        while True:
            msg = await job_state["queue"].get()
            if msg is None:
                break
            data = json.loads(msg)
            if data.get("type") == "log":
                print(data["msg"])
            elif data.get("type") == "status":
                print(f"[status] {data['msg']}")

    drain_future = asyncio.run_coroutine_threadsafe(drain_queue(), loop)

    convert_book(job_state, settings, loop)
    drain_future.result(timeout=30)

    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)

    print("\n=== RESULT ===")
    print("status:", job_state["status"])
    print("files:", job_state["files"])
    for f in job_state["files"]:
        print(" -", OUT_DIR / f)


if __name__ == "__main__":
    main()
