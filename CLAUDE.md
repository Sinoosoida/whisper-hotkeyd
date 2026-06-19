# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A user-space Linux tray app that records audio on a global hotkey, filters
silence, transcribes it via a Whisper-style HTTP API, and copies the result to
the clipboard. The transcription client speaks two formats — multipart
`form-data` (DeepInfra, standard Whisper API) and base64-in-JSON (OpenRouter
`audio/transcriptions`) — selected by `api.request_format`. Python + PySide6 +
evdev. Distributed as a Python package (`pipx`-installable) and as an Arch
package on the AUR (`PKGBUILD` + `scripts/release.sh`).

The v4 era was a single root-only script (`whisper-hotkeyd.py`). v5 rearchitected
it into a proper package — see "Why this is the way it is" below before
suggesting changes that look like simplifications.

## Layout

```
whisper_hotkeyd/           # the Python package
    __init__.py          # __version__ (single source, bumped by release.sh)
    __main__.py          # `python -m whisper_hotkeyd` entry point
    cli.py               # entry point, argparse, Qt bootstrap, single-instance
    config.py            # TOML config loader/saver (XDG paths)
    logsetup.py          # RotatingFileHandler + stderr + excepthook
    recorder.py          # arecord wrapper + RMS/duration filter
    transcriber.py       # WAV→MP3, form-data (DeepInfra) / JSON (OpenRouter) HTTP + retries
    clipboard.py         # xclip/wl-copy auto-detect
    languages.py         # static Whisper language catalog (settings combo)
    listener.py          # evdev thread, fires engine.on_key_press/on_key_release
    engine.py            # QObject orchestrator + Qt signals; owns retry-last
    tray.py              # QSystemTrayIcon + SettingsDialog
    setup_helpers.py     # --setup wizard, input-group check, autostart
    resources/*.svg      # tray icons (idle / recording / transcribing)
data/                    # desktop entry + systemd user unit (shipped by PKGBUILD)
pyproject.toml           # build config, scripts entry "whisper-hotkeyd"
PKGBUILD                 # AUR recipe: builds wheel from the release tarball
scripts/release.sh       # cut a release: bump, tag, GitHub Release, sync to AUR
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

The user's config (with their API key) lives at
`/home/sinoosoida/.config/whisper-hotkeyd/config.toml`. It is pre-populated; do
not regenerate or overwrite it without their say-so. As of this writing it
points at OpenRouter (`qwen/qwen3-asr-flash`, `request_format = "json"`), not
the DeepInfra default in `config.py` — don't assume the dataclass defaults
reflect what they actually run.

## Why this is the way it is

These are the non-obvious decisions a reader needs to know before changing
things — they each cost effort to discover and are easy to undo by accident.

- **User-space, not root.** v4 ran the whole process under `sudo` to read
  `/dev/input`, then re-`sudo`'d back to the user just to call `xclip`. v5
  drops that entirely: the user joins the `input` group once, and everything
  runs in their session. Do **not** reintroduce a root daemon "for cleanliness"
  unless asked — it was specifically the thing being deleted.
- **`engine.py` is a QObject with signals.** The evdev listener runs on a
  background thread; it calls `engine.on_key_press()` / `on_key_release()`,
  which only `Signal.emit` the private `_pressSignal` / `_releaseSignal` /
  `_forceStopSignal` to land on the Qt main thread (the `_handle_press` /
  `_handle_release` / `_handle_force_stop` slots). All state changes happen on
  the GUI thread. Transcription, which is slow, is dispatched onto a fresh
  `threading.Thread` so the UI never stalls. Don't change this pattern to
  direct calls — it would race the recorder state.
- **The recording timeout reuses the manual-stop path.** When
  `Recorder.start()` is called, `on_timeout=self._request_force_stop` is passed;
  it emits `_forceStopSignal`, so a runaway recording stops via the same
  GUI-thread path (`_handle_force_stop` → `_stop_and_process`) as a key press.
- **Kept recordings are never deleted; failed ones can be re-sent.** The
  recorder deletes only *rejected* clips (too short/quiet) in `_filter`; the
  transcriber deletes only the temp MP3. So a kept WAV survives on disk after
  transcription. The engine exploits this: on a transcription failure it stores
  the path in `_last_audio_path` and emits `retryAvailable(True)`; `retry_last()`
  re-runs `_process_recording` on that same WAV. The tray surfaces it as a
  "Retry last transcription" menu item and a clickable error balloon. This is
  the safety net for transient provider 429s on long recordings — the audio is
  never lost to a failed API call.
- **`reload_config()` stops an in-flight recording before swapping.** Settings
  can be saved mid-recording (toggle mode); the reload stops the old recorder
  first so its `arecord` process and timeout `Timer` aren't orphaned, then
  builds fresh `Recorder`/`Transcriber` objects.
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
- **PKGBUILD is an AUR release recipe built from a versioned tarball.** Its
  `source=(…)` fetches `…/archive/refs/tags/v$pkgver.tar.gz` with a committed
  `sha256sums`; `build()` runs `python -m build --wheel --no-isolation` and
  `package()` installs via `python -m installer` after `cd`-ing into the
  extracted `$pkgname-$pkgver`. Do **not** "simplify" it back to a
  build-the-working-tree recipe — the tarball source is what makes `yay -S
  whisper-hotkeyd` work. The `sha256` is recomputed by `scripts/release.sh`;
  don't hand-edit it.
- **API key is a per-user secret in `~/.config/whisper-hotkeyd/config.toml`.**
  Never hard-code one in the package source. The historical
  `whisper-hotkeyd.py` had one baked in — that was a deliberate testing shortcut,
  not the model going forward.

## Releasing

The project ships to two remotes: GitHub (`Sinoosoida/whisper-hotkeyd`, public)
and the AUR (package base `whisper-hotkeyd`). One command cuts a full release:

```
./scripts/release.sh X.Y.Z
```

It bumps `__version__` + the `pyproject` version, commits, tags, pushes
branch+tag, creates a GitHub Release, recomputes the PKGBUILD `sha256` against
the published tarball, regenerates `.SRCINFO`, and pushes `PKGBUILD` +
`.SRCINFO` to the AUR. Requires: `gh` logged in, GitHub push rights, an SSH key
registered with the AUR, and `makepkg`. The version is duplicated in
`whisper_hotkeyd/__init__.py` and `pyproject.toml`, kept in lockstep only by
this script — edit both or neither. Publishing is outward-facing and hard to
reverse; don't run it unprompted.

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
- "Transcription failed: API returned 429" → the provider (OpenRouter /
  DeepInfra) is rate-limiting and the in-request retries (`max_attempts` /
  `retry_backoff_sec`, honoring `Retry-After`) were exhausted. Largely
  time-of-day/load driven, not strictly tied to audio length. The recording is
  **not** lost — use "Retry last transcription" in the tray (or click the error
  balloon). The preceding `Transcribing …: <ms> audio, <KB> mp3` log line shows
  the payload size for diagnosis.
- "Tray icon doesn't appear" → some Wayland compositors (e.g. vanilla GNOME)
  don't show `QSystemTrayIcon` without an extension (AppIndicator). On those,
  install the AppIndicator extension or run under X11.

## Things deliberately not in the codebase

- No tests. The user has not asked for them.
- No CI workflow. The user runs locally.
- No PyPI publication yet — `pyproject` is publish-ready when they decide to.
  (The AUR *is* published: package base `whisper-hotkeyd`, maintainer
  `Sinoosoida`, kept in sync by `scripts/release.sh` — see "Releasing".)
- No telemetry, no auto-update.
