"""SSE-compatible event emitter for a running conversion job.

Wraps a job's asyncio.Queue with typed push helpers so pipeline code
constructs events through a small interface instead of raw dicts scattered
through the pipeline. Pushed messages are JSON strings compatible with the
SSE stream format read by routes/convert.py's /stream endpoint:
    {"type": "log"|"status"|"progress"|"ch_info"|"ch_start"|"ch_prog"|"ch_skip"|"file"}
"""
import json
from asyncio import AbstractEventLoop


class JobEmitter:
    def __init__(self, job_state: dict, loop: AbstractEventLoop):
        self._job_state = job_state
        self._loop = loop

    def push(self, data: dict) -> None:
        """Escape hatch for event shapes with no dedicated helper below
        (ch_info, ch_start, ch_prog, ch_skip, file)."""
        self._loop.call_soon_threadsafe(self._job_state["queue"].put_nowait, json.dumps(data))

    def log(self, msg: str) -> None:
        self.push({"type": "log", "msg": msg})

    def status(self, msg: str) -> None:
        self.push({"type": "status", "msg": msg})

    def progress(self, value, label: str = "") -> None:
        self.push({"type": "progress", "value": value, "label": label})

    def done(self) -> None:
        self._loop.call_soon_threadsafe(self._job_state["queue"].put_nowait, None)
