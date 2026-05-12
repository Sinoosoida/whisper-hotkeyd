from __future__ import annotations

import logging
import os
import shutil
import subprocess

log = logging.getLogger(__name__)


def detect_backend() -> str | None:
    """Return 'wl-copy' or 'xclip' depending on session type and available binaries."""
    session = (os.environ.get("XDG_SESSION_TYPE") or "").lower()
    on_wayland = session == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))

    if on_wayland and shutil.which("wl-copy"):
        return "wl-copy"
    if shutil.which("xclip"):
        return "xclip"
    if shutil.which("wl-copy"):
        return "wl-copy"
    return None


def copy(text: str, backend: str = "auto") -> bool:
    chosen = backend if backend != "auto" else detect_backend()
    if not chosen:
        log.error("No clipboard backend available (install xclip or wl-clipboard).")
        return False

    if chosen == "wl-copy":
        cmd = ["wl-copy"]
    elif chosen == "xclip":
        cmd = ["xclip", "-selection", "clipboard"]
    else:
        log.error("Unknown clipboard backend: %s", chosen)
        return False

    try:
        proc = subprocess.run(cmd, input=text, text=True, check=False)
        if proc.returncode != 0:
            log.error("%s exited with %d", chosen, proc.returncode)
            return False
        return True
    except FileNotFoundError:
        log.error("%s not found on PATH", chosen)
        return False
