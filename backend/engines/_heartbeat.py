"""Shared heartbeat helper for the standalone synthesis workers (higgs_synth.py,
chatterbox_synth.py). Both spend long stretches — loading a multi-GB model,
or a single generate() call — producing zero stdout output, which looks
indistinguishable from a hang from the parent process's side. This starts a
daemon thread that prints whatever the caller last set `stage["text"]` to,
every HEARTBEAT_INTERVAL_S seconds; runner.py's parent process recognizes
lines prefixed with HEARTBEAT_PREFIX and forwards them to the job log
instead of discarding them as noise.

Imported by bare `import _heartbeat` (not `backend.engines._heartbeat`) —
these worker scripts run as standalone files via a separate venv's
interpreter, and Python puts a run script's own directory on sys.path[0],
so a same-directory sibling module resolves without needing the `backend`
package (or its venv-only deps) importable at all.
"""
import resource
import sys
import threading
import time

HEARTBEAT_PREFIX = "##HEARTBEAT##"
HEARTBEAT_INTERVAL_S = 10


def _rss_gb() -> float:
    # ru_maxrss is in BYTES on macOS but KILOBYTES on Linux (getrusage(2)).
    # Without this scaling the Linux/Docker path — the primary deployment
    # target — under-reports memory by 1024x, so the safety abort below can
    # never fire and the memory guard silently does nothing.
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        rss *= 1024
    return rss / (1024.0 ** 3)


_write_lock = threading.Lock()


def emit(line: str) -> None:
    """Write one complete line to stdout under a shared lock.

    Defensive, not a fix for an observed failure: print() issues separate
    write() calls for the text and for the trailing newline and holds no
    lock across them, so nothing in the language spec stops the heartbeat
    thread from splicing a tick into the middle of a ##PROGRESS## line the
    main thread is printing — which runner.py's PROGRESS_RE would then not
    match, silently losing that chunk's progress update and its
    on_chunk_done callback. In practice CPython's buffering made that
    unreproducible over 3000 contended lines, so this guards against an
    implementation detail changing rather than a bug seen in the wild.
    Cheap enough to just not depend on it. Use this instead of print() for
    any line the parent process parses.
    """
    with _write_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _loop(stage: dict, stop: threading.Event):
    start = time.time()
    while not stop.wait(HEARTBEAT_INTERVAL_S):
        elapsed = time.time() - start
        emit(f"{HEARTBEAT_PREFIX} {elapsed:.0f}s elapsed — {stage['text']} "
             f"(rss={_rss_gb():.1f}GB)")


def start(initial_text: str) -> tuple[dict, threading.Event]:
    """Starts the heartbeat thread. Returns (stage, stop_event):
    - set stage["text"] = "..." any time to change what the next tick prints
    - call stop_event.set() when done (in a finally block) to stop ticking;
      the thread is a daemon so it also dies automatically with the process
    """
    stage = {"text": initial_text}
    stop = threading.Event()
    threading.Thread(target=_loop, args=(stage, stop), daemon=True).start()
    return stage, stop
