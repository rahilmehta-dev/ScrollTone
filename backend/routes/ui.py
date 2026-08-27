"""
UI system routes.

GET /config        — return runtime config (e.g. whether running inside Docker)
GET /sample-text   — the shared preview sample text (see backend/voices.py)
GET /pick-folder   — open a native OS folder-picker and return the chosen path
                     (native/non-Docker runs only — see frontend/app.js
                     chooseDeviceFolder() for how Docker picks a save folder:
                     the browser's own File System Access API, since the
                     container can't reach an arbitrary host path itself)
POST /shutdown     — stop the server process
"""
import os
import subprocess
import sys
import threading

from fastapi import APIRouter

from backend.voices import SAMPLE_TEXT

router = APIRouter()


@router.get("/config")
def config():
    """Return runtime configuration flags for the frontend, including which
    Processing Device options actually make sense to offer — e.g. a Linux
    Docker container can never have Apple Silicon's MPS, so don't list it."""
    devices = ["auto", "cpu"]
    try:
        import torch
        if torch.cuda.is_available():
            devices.append("cuda")
        if torch.backends.mps.is_available():
            devices.append("mps")
    except Exception:
        pass
    return {
        "docker": os.path.exists("/.dockerenv"),
        "cpu_count": os.cpu_count() or 1,
        "devices": devices,
    }


@router.get("/sample-text")
def sample_text():
    """The shared ~200-word sample used everywhere a voice/engine is
    previewed, so the UI can display it without duplicating the string."""
    return {"text": SAMPLE_TEXT}


@router.get("/pick-folder")
def pick_folder():
    """Open a native OS folder-picker dialog and return the chosen path.

    macOS  : AppleScript 'choose folder' (no extra permissions needed)
    Linux  : zenity (GNOME) → kdialog (KDE) fallback chain
    Other  : returns {"path": ""}
    """
    # macOS — AppleScript Finder dialog
    if sys.platform == "darwin":
        try:
            script = (
                'POSIX path of '
                '(choose folder with prompt "Select output folder for audiobooks")'
            )
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0:
                return {"path": r.stdout.strip()}
        except Exception:
            pass

    # Linux — zenity (GNOME) then kdialog (KDE)
    try:
        r = subprocess.run(
            ["zenity", "--file-selection", "--directory", "--title=Select output folder"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            return {"path": r.stdout.strip()}
    except FileNotFoundError:
        pass

    try:
        r = subprocess.run(
            ["kdialog", "--getexistingdirectory", "."],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            return {"path": r.stdout.strip()}
    except FileNotFoundError:
        pass

    return {"path": ""}


@router.post("/shutdown")
def shutdown():
    """Stop the server process."""
    threading.Timer(0.3, os._exit, args=(0,)).start()
    return {"status": "shutting_down"}
