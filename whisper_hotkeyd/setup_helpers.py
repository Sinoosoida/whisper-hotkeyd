from __future__ import annotations

import grp
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from whisper_hotkeyd.config import CONFIG_PATH, xdg_config_home

log = logging.getLogger(__name__)

AUTOSTART_PATH = xdg_config_home() / "autostart" / "whisper-hotkeyd.desktop"

DESKTOP_FILE = """\
[Desktop Entry]
Type=Application
Name=Whisper Hotkey
Comment=Push-to-talk voice transcription to clipboard
Exec={exec_path}
Icon={icon_path}
Terminal=false
Categories=Utility;AudioVideo;
X-GNOME-Autostart-enabled=true
StartupNotify=false
"""


def _find_executable() -> str:
    """Find the whisper-hotkeyd command. Falls back to `python -m whisper_hotkeyd`."""
    found = shutil.which("whisper-hotkeyd")
    if found:
        return found
    return f"{sys.executable} -m whisper_hotkeyd"


def check_input_group() -> tuple[bool, str | None]:
    """Return (user_is_in_input_group, username) or (False, None) on lookup error."""
    try:
        user = os.environ.get("USER") or os.getlogin()
    except OSError:
        user = None

    try:
        input_grp = grp.getgrnam("input")
    except KeyError:
        log.warning("Group 'input' does not exist on this system")
        return False, user

    in_group = user in input_grp.gr_mem if user else False
    # Also check current process supplementary groups in case session is already updated.
    if not in_group:
        try:
            in_group = input_grp.gr_gid in os.getgroups()
        except OSError:
            pass
    return in_group, user


def install_autostart() -> Path:
    AUTOSTART_PATH.parent.mkdir(parents=True, exist_ok=True)
    exec_path = _find_executable()
    icon_path = Path(__file__).parent / "resources" / "icon.svg"
    content = DESKTOP_FILE.format(exec_path=exec_path, icon_path=icon_path)
    AUTOSTART_PATH.write_text(content)
    log.info("Wrote autostart entry: %s", AUTOSTART_PATH)
    return AUTOSTART_PATH


def remove_autostart() -> bool:
    if AUTOSTART_PATH.exists():
        AUTOSTART_PATH.unlink()
        log.info("Removed autostart entry: %s", AUTOSTART_PATH)
        return True
    return False


def autostart_installed() -> bool:
    return AUTOSTART_PATH.exists()


def sync_autostart(managed: bool) -> None:
    """Make the autostart .desktop file presence match `managed`.

    Called at startup so that first-run installs autostart silently, and
    later toggles in Settings actually create/remove the file. Any IO
    error is logged but not raised — autostart is a nice-to-have, the app
    must keep working without it.
    """
    try:
        if managed and not autostart_installed():
            install_autostart()
        elif not managed and autostart_installed():
            remove_autostart()
    except OSError:
        log.exception("Could not sync autostart entry (managed=%s)", managed)


def run_setup_wizard() -> int:
    print("Whisper Hotkey — setup")
    print("=====================")
    print()

    in_input_group, user = check_input_group()
    if in_input_group:
        print(f"[ok] User '{user}' is in the 'input' group.")
    else:
        print(f"[warn] User '{user}' is NOT in the 'input' group.")
        print("       The app needs this to read keyboard events without sudo.")
        print(f"       Run: sudo gpasswd -a {user} input")
        print("       Then log out and back in (or reboot) for the change to apply.")
    print()

    print(f"Config file: {CONFIG_PATH}")
    if CONFIG_PATH.exists():
        print("       (exists)")
    else:
        print("       (will be created on first run)")
    print()

    if autostart_installed():
        print(f"[ok] Autostart entry exists: {AUTOSTART_PATH}")
        answer = input("Remove it? [y/N] ").strip().lower()
        if answer == "y":
            remove_autostart()
            print("Removed.")
    else:
        answer = input("Install autostart entry so Whisper Hotkey starts at login? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            install_autostart()
            print(f"Installed: {AUTOSTART_PATH}")
        else:
            print("Skipped autostart.")
    print()

    print("Done.")
    return 0
