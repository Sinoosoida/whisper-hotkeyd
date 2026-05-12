# Whisper Hotkey

Push-to-talk voice transcription to clipboard, with a system tray icon.

Hold (well, press once to start, press again to finish) a configurable key — by default Right Alt — and Whisper Hotkey records audio, filters out silence, transcribes it via the DeepInfra Whisper API, and pastes the result into your clipboard. Runs in the background as a tray app, no terminal needed.

## Status

Version 5.0 — rearchitected from a single-file root script (v4.2) into a proper
user-space tray app. The v1–v4 history lives in the commit log and in the
historical `.pkg.tar.zst` artifacts.

## Features

- **Global trigger key** read directly from `/dev/input` via evdev — works regardless of focus, on X11 and Wayland.
- **Smart filter** — discards recordings shorter than 1s or whose last 100ms RMS is below −65 dBFS (catches mute / whitespace).
- **Whisper turbo path** — WAV → MP3 (64 kbps) → DeepInfra `whisper-large-v3-turbo`, language pinned to one configured value for latency.
- **Clipboard backend autodetect** — `xclip` on X11, `wl-copy` on Wayland.
- **Tray icon** (PySide6) with status indicator, settings dialog, and quick access to the config and recordings folder.
- **File logging** — rotating logs at `~/.local/state/whisper-hotkeyd/whisper-hotkeyd.log`.
- **Autostart** via XDG desktop entry; optional systemd-user unit.

## Requirements

- Linux with a freedesktop-compliant desktop environment (GNOME, KDE, XFCE, Cinnamon, MATE, …).
- Python 3.9+.
- System packages: `ffmpeg`, `alsa-utils`, `xclip` (X11) and/or `wl-clipboard` (Wayland), `libnotify`, `xdg-utils`.
- Membership in the `input` group (one-time setup — see below).

## Install

### Arch / Manjaro (via makepkg)

```bash
makepkg -si
```

### Other distros (via pipx)

```bash
sudo apt install ffmpeg alsa-utils xclip wl-clipboard libnotify-bin xdg-utils  # Debian/Ubuntu
pipx install .
```

(`pipx install whisper-hotkeyd` once the package is published to PyPI.)

### First-time setup

```bash
whisper-hotkeyd --setup
```

This:

1. Checks whether your user is in the `input` group. If not, prints the
   `sudo gpasswd -a $USER input` command — run it, then **log out and back in**
   for the group to take effect.
2. Offers to install the XDG autostart entry so Whisper Hotkey launches on login.

Set your DeepInfra API key from the tray menu (**Settings…**) or by editing
`~/.config/whisper-hotkeyd/config.toml` directly.

## Configuration

All settings live in `~/.config/whisper-hotkeyd/config.toml`. The file is created
with defaults on first launch.

```toml
[api]
key = ""                                                              # your DeepInfra key
url = "https://api.deepinfra.com/v1/openai/audio/transcriptions"
model = "openai/whisper-large-v3-turbo"
language = "ru"

[recording]
trigger_key = 100              # evdev keycode (100 = KEY_RIGHTALT)
rms_threshold_dbfs = -65.0
min_duration_ms = 1000
analyze_last_ms = 100
timeout_sec = 300
output_dir = "~/.local/share/whisper-hotkeyd/recordings"

[clipboard]
backend = "auto"               # auto | xclip | wl-copy

[ui]
notifications = true
```

To find the keycode for a different trigger key, run:

```bash
sudo evtest                    # press the key you want, note the code shown
```

## Files

| Path                                                | Purpose                       |
| --------------------------------------------------- | ----------------------------- |
| `~/.config/whisper-hotkeyd/config.toml`               | User configuration            |
| `~/.local/share/whisper-hotkeyd/recordings/`          | Kept WAV recordings           |
| `~/.local/state/whisper-hotkeyd/whisper-hotkeyd.log*`   | Rotating log files (5 × 10MB) |
| `~/.config/autostart/whisper-hotkeyd.desktop`         | Autostart entry (optional)    |

## CLI

```
whisper-hotkeyd              # launch the tray app (no terminal needed)
whisper-hotkeyd --setup      # interactive setup wizard
whisper-hotkeyd --config-path
whisper-hotkeyd --log-path
whisper-hotkeyd -v           # debug-level logging
whisper-hotkeyd --version
```

## Architecture

A single user-space process. `cli.py` boots Qt and wires three actors:

- **`listener.py`** — background thread reading `/dev/input/event*` via `evdev`.
  Emits a thread-safe trigger into the engine on each key-down of the
  configured trigger key.
- **`engine.py`** — QObject coordinating state. On trigger, starts/stops the
  `Recorder`. On stop, off-loads transcription to a worker thread so the GUI
  stays responsive. Emits Qt signals for status, transcription, and errors.
- **`tray.py`** — `QSystemTrayIcon` with menu and settings dialog. Subscribes
  to engine signals to update the icon and surface notifications.

No more `sudo`. Access to `/dev/input/event*` comes from `input`-group
membership; clipboard goes through `xclip`/`wl-copy` running as the same user.

## Releasing (maintainer)

```bash
./scripts/release.sh 5.2.0
```

Bumps the version, tags, pushes a GitHub Release, recomputes the PKGBUILD's
sha256 against the freshly-published tarball, and syncs PKGBUILD + .SRCINFO to
the AUR repo. Requires `gh` logged in and an SSH key registered with AUR.

## License

MIT.
