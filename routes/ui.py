"""
UI routes.

GET /             — serve the main index.html page
GET /pick-folder  — open a native OS folder-picker and return the chosen path
"""
import subprocess
import sys

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

import core.state as state

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index():
    return (state.BASE_DIR / "templates" / "index.html").read_text()


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
