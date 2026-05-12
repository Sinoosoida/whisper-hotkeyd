# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A user-space Linux tray app that records audio on a global hotkey, filters
silence, transcribes via DeepInfra Whisper, and copies the result to the
clipboard. Python + PySide6 + evdev. Distributed as a Python package (PyPI-ready,
`pipx`-installable) and as an Arch package (`PKGBUILD`).

The v4 era was a single root-only script (`whisper-hotkeyd.py`). v5 rearchitected
it into a proper package — see "Why this is the way it is" below before
suggesting changes that look like simplifications.

## Layout

```
whisper_hotkeyd/           # the Python package
    cli.py               # entry point, argparse, Qt bootstrap
    config.py            # TOML config loader/saver (XDG paths)
    logsetup.py          # RotatingFileHandler + stderr + excepthook
    recorder.py          # arecord wrapper + RMS/duration filter
    transcriber.py       # WAV→MP3, DeepInfra HTTP call
    clipboard.py         # xclip/wl-copy auto-detect
    listener.py          # evdev thread, fires engine.request_trigger()
    engine.py            # QObject orchestrator + Qt signals
    tray.py              # QSystemTrayIcon + SettingsDialog
    setup_helpers.py     # --setup wizard, input-group check, autostart
    resources/*.svg      # tray icons (idle / recording / transcribing)
data/                    # desktop entry + systemd user unit (shipped by PKGBUILD)
pyproject.toml           # PyPI build config, scripts entry "whisper-hotkeyd"
PKGBUILD                 # builds wheel via python-build, installs via installer
```

There is no test suite or linter config. Imports are validated with
`python -m py_compile whisper_hotkeyd/*.py`.

## Commands

| Action                          | Command                                                              |
| ------------------------------- | -------------------------------------------------------------------- |
| Run from source (no install)    | `python -m whisper_hotkeyd`                                            |
| Run with debug logging          | `python -m whisper_hotkeyd -v`                                         |
| Build wheel                     | `python -m build --wheel`                                            |
| Build & install Arch package    | `makepkg -si`                                                        |
| Install in editable mode        | `pipx install -e .` or `pip install -e . --break-system-packages`    |
| Compile-check everything        | `python -m py_compile whisper_hotkeyd/*.py`                            |
| Tail the log                    | `tail -f ~/.local/state/whisper-hotkeyd/whisper-hotkeyd.log`             |

The user's config (with their DeepInfra key) lives at
`/home/sinoosoida/.config/whisper-hotkeyd/config.toml`. It is pre-populated; do
not regenerate or overwrite it without their say-so.

## Why this is the way it is

These are the non-obvious decisions a reader needs to know before changing
things — they each cost effort to discover and are easy to undo by accident.

- **User-space, not root.** v4 ran the whole process under `sudo` to read
  `/dev/input`, then re-`sudo`'d back to the user just to call `xclip`. v5
  drops that entirely: the user joins the `input` group once, and everything
  runs in their session. Do **not** reintroduce a root daemon "for cleanliness"
  unless asked — it was specifically the thing being deleted.
- **`engine.py` is a QObject with signals.** The evdev listener runs on a
  background thread; it calls `engine.request_trigger()`, which goes through
  `Signal.emit` to land on the Qt main thread (`_handle_trigger` slot). All
  state changes happen on the GUI thread. Transcription, which is slow, is
  dispatched onto a fresh `threading.Thread` so the UI never stalls. Don't
  change this pattern to direct calls — it would race the recorder state.
- **`request_trigger` is also used as the recording-timeout callback.** When
  `Recorder.start()` is called, `on_timeout=self.request_trigger` is passed, so
  a runaway recording stops via the same code path as a manual key press.
- **Trigger key is an evdev scancode, not a keysym.** `100 = KEY_RIGHTALT`.
  Changing keyboard layout has no effect. To pick a different key, run
  `sudo evtest`, watch the code reported on key-down, and put it in
  `recording.trigger_key`.
- **The listener opens every device whose `EV_KEY` capabilities include the
  trigger key** — not just one matched by `"keyboard"` substring (the v4
  heuristic). Multiple physical keyboards are handled correctly via `select()`.
- **TOML write uses `tomli_w` if available, else a built-in fallback.** The
  fallback only handles the value types the dataclasses actually use (str,
  int, float, bool). If you add nested tables or lists to `Config`, extend
  `_format_toml_value` in `config.py` to match.
- **Clipboard backend resolution.** `clipboard.detect_backend()` checks
  `XDG_SESSION_TYPE` and `WAYLAND_DISPLAY` first, then falls back to whichever
  binary is on `$PATH`. Config can pin to `xclip` or `wl-copy` explicitly.
- **PKGBUILD builds from `$startdir`, not a tarball.** It calls
  `python -m build --wheel --no-isolation` then `python -m installer`. No
  `source=(…)` array — this is a "build the working tree" recipe, not a
  release recipe. If you want to ship to AUR proper, switch to a versioned
  tarball source and add real `sha256sums`.
- **API key is a per-user secret in `~/.config/whisper-hotkeyd/config.toml`.**
  Never hard-code one in the package source. The historical
  `whisper-hotkeyd.py` had one baked in — that was a deliberate testing shortcut,
  not the model going forward.

## Common pitfalls

- "Trigger key does nothing" → user isn't in `input` group, or hasn't logged
  out and back in after `gpasswd`. The tray surfaces this after 2s via a
  notification; check the log file too.
- "Clipboard copy fails silently" → `xclip` not installed on X11, or
  `wl-clipboard` missing on Wayland. Logged as `clipboard: No clipboard
  backend available` at ERROR.
- "Recordings vanish without transcription" → check the log; either duration
  was under `min_duration_ms` or RMS was below `rms_threshold_dbfs`. Both are
  expected for short/silent presses.
- "Tray icon doesn't appear" → some Wayland compositors (e.g. vanilla GNOME)
  don't show `QSystemTrayIcon` without an extension (AppIndicator). On those,
  install the AppIndicator extension or run under X11.

## Things deliberately not in the codebase

- No tests. The user has not asked for them.
- No CI workflow. The user runs locally.
- No PyPI/AUR publication yet — the user is keeping it local for now. The
  pyproject and PKGBUILD are written to be publish-ready when they decide to.
- No telemetry, no auto-update.
