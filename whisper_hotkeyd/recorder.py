from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydub import AudioSegment

log = logging.getLogger(__name__)


@dataclass
class RecordingResult:
    path: Path
    duration_ms: int
    rms_dbfs: float
    kept: bool
    reason: str  # "kept" | "too_short" | "too_quiet" | "missing"


class Recorder:
    """Wraps arecord. start() begins capture, stop() terminates and filters."""

    def __init__(
        self,
        output_dir: Path,
        rms_threshold_dbfs: float,
        min_duration_ms: int,
        analyze_last_ms: int,
        timeout_sec: int,
    ) -> None:
        self.output_dir = output_dir
        self.rms_threshold_dbfs = rms_threshold_dbfs
        self.min_duration_ms = min_duration_ms
        self.analyze_last_ms = analyze_last_ms
        self.timeout_sec = timeout_sec

        self._process: subprocess.Popen | None = None
        self._filename: Path | None = None
        self._timeout_timer: threading.Timer | None = None
        self._timeout_cb = None
        self._lock = threading.Lock()

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def start(self, on_timeout=None) -> Path:
        with self._lock:
            if self._process is not None:
                raise RuntimeError("Already recording")

            self.output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._filename = self.output_dir / f"record_{timestamp}.wav"

            log.info("Starting recording: %s", self._filename.name)
            self._process = subprocess.Popen(
                ["arecord", "-q", "-f", "cd", str(self._filename)],
                preexec_fn=os.setsid,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            self._timeout_cb = on_timeout
            self._timeout_timer = threading.Timer(self.timeout_sec, self._fire_timeout)
            self._timeout_timer.daemon = True
            self._timeout_timer.start()

            return self._filename

    def _fire_timeout(self) -> None:
        log.warning("Recording exceeded timeout of %ds, stopping", self.timeout_sec)
        if self._timeout_cb:
            try:
                self._timeout_cb()
            except Exception:
                log.exception("Timeout callback failed")

    def stop(self) -> RecordingResult | None:
        with self._lock:
            if self._process is None or self._filename is None:
                return None

            if self._timeout_timer is not None:
                self._timeout_timer.cancel()
                self._timeout_timer = None

            try:
                if self._process.poll() is None:
                    os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
                    self._process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired) as e:
                log.warning("Trouble stopping arecord: %s", e)

            filename = self._filename
            self._process = None
            self._filename = None

        return self._filter(filename)

    def _filter(self, path: Path) -> RecordingResult:
        if not path.exists():
            log.warning("Recording file missing: %s", path)
            return RecordingResult(path, 0, float("-inf"), False, "missing")

        try:
            audio = AudioSegment.from_wav(str(path))
        except Exception:
            log.exception("Failed to read recording %s", path)
            try:
                path.unlink()
            except OSError:
                pass
            return RecordingResult(path, 0, float("-inf"), False, "missing")

        duration_ms = len(audio)
        if duration_ms < self.min_duration_ms:
            log.info("Discarding short recording (%dms < %dms): %s",
                     duration_ms, self.min_duration_ms, path.name)
            try:
                path.unlink()
            except OSError:
                pass
            return RecordingResult(path, duration_ms, float("-inf"), False, "too_short")

        tail = audio[-self.analyze_last_ms:]
        rms = tail.dBFS
        if rms < self.rms_threshold_dbfs:
            log.info("Discarding quiet recording (RMS %.1f dBFS < %.1f): %s",
                     rms, self.rms_threshold_dbfs, path.name)
            try:
                path.unlink()
            except OSError:
                pass
            return RecordingResult(path, duration_ms, rms, False, "too_quiet")

        log.info("Kept recording: %s (%dms, RMS %.1f dBFS)",
                 path.name, duration_ms, rms)
        return RecordingResult(path, duration_ms, rms, True, "kept")
