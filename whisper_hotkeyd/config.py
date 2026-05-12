from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

try:
    import tomli_w
except ImportError:
    tomli_w = None


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def xdg_state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")


CONFIG_PATH = xdg_config_home() / "whisper-hotkeyd" / "config.toml"
DEFAULT_RECORDINGS_DIR = xdg_data_home() / "whisper-hotkeyd" / "recordings"
LOG_DIR = xdg_state_home() / "whisper-hotkeyd"


@dataclass
class ApiConfig:
    key: str = ""
    url: str = "https://api.deepinfra.com/v1/openai/audio/transcriptions"
    model: str = "openai/whisper-large-v3-turbo"
    language: str = "ru"
    request_timeout_sec: int = 120


@dataclass
class RecordingConfig:
    trigger_key: int = 100  # evdev KEY_RIGHTALT
    mode: str = "toggle"    # "toggle" | "hold"
    rms_threshold_dbfs: float = -65.0
    min_duration_ms: int = 1000
    analyze_last_ms: int = 100
    timeout_sec: int = 300
    output_dir: str = str(DEFAULT_RECORDINGS_DIR)


@dataclass
class ClipboardConfig:
    backend: str = "auto"  # auto | xclip | wl-copy


@dataclass
class UiConfig:
    notifications: bool = True


@dataclass
class Config:
    api: ApiConfig = field(default_factory=ApiConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    clipboard: ClipboardConfig = field(default_factory=ClipboardConfig)
    ui: UiConfig = field(default_factory=UiConfig)

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> tuple["Config", bool]:
        """Load config from disk. Returns (config, created_new).

        If the file does not exist, writes defaults and returns created_new=True.
        Missing keys in an existing file are filled in from defaults.
        """
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            return cfg, True

        with path.open("rb") as f:
            data = tomllib.load(f)

        cfg = cls(
            api=_build(ApiConfig, data.get("api", {})),
            recording=_build(RecordingConfig, data.get("recording", {})),
            clipboard=_build(ClipboardConfig, data.get("clipboard", {})),
            ui=_build(UiConfig, data.get("ui", {})),
        )
        return cfg, False

    def save(self, path: Path = CONFIG_PATH) -> None:
        if tomli_w is None:
            self._save_manual(path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            tomli_w.dump(asdict(self), f)

    def _save_manual(self, path: Path) -> None:
        """Fallback TOML writer for when tomli_w is unavailable."""
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for section_name, section_obj in (
            ("api", self.api),
            ("recording", self.recording),
            ("clipboard", self.clipboard),
            ("ui", self.ui),
        ):
            lines.append(f"[{section_name}]")
            for key, value in asdict(section_obj).items():
                lines.append(f"{key} = {_format_toml_value(value)}")
            lines.append("")
        path.write_text("\n".join(lines))

    @property
    def output_dir(self) -> Path:
        return Path(os.path.expanduser(self.recording.output_dir))

    def is_configured(self) -> bool:
        return bool(self.api.key.strip())


def _build(cls, data: dict):
    """Construct a dataclass from a TOML table, ignoring unknown keys.

    Lets a newer config file open in an older code version (and vice versa)
    without crashing — unknown keys are dropped with a debug log line.
    """
    import dataclasses
    import logging
    known = {f.name for f in dataclasses.fields(cls)}
    accepted = {k: v for k, v in data.items() if k in known}
    unknown = set(data) - known
    if unknown:
        logging.getLogger(__name__).debug(
            "Ignoring unknown %s keys in config: %s", cls.__name__, sorted(unknown)
        )
    return cls(**accepted)


def _format_toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise TypeError(f"Unsupported TOML value type: {type(value)}")
