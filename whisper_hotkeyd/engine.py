from __future__ import annotations

import logging
import threading
from enum import Enum

from PySide6.QtCore import QObject, Signal, Slot

from whisper_hotkeyd import clipboard
from whisper_hotkeyd.config import Config
from whisper_hotkeyd.recorder import Recorder
from whisper_hotkeyd.transcriber import Transcriber, TranscriptionError

log = logging.getLogger(__name__)


class Status(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    PAUSED = "paused"
    ERROR = "error"


class Engine(QObject):
    """Coordinator object — owns Recorder, Transcriber, and emits Qt signals
    so the tray UI can react. Thread-safe trigger via on_key_press/on_key_release.

    Recording semantics depend on `config.recording.mode`:
      - "toggle": each press flips between record and stop.
      - "hold":   press starts, release stops.
    """

    statusChanged = Signal(Status)
    transcriptionReady = Signal(str)
    errorOccurred = Signal(str)
    notify = Signal(str, str)  # title, body

    _pressSignal = Signal()
    _releaseSignal = Signal()
    _forceStopSignal = Signal()

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self._paused = False
        self._status = Status.IDLE

        self.recorder = Recorder(
            output_dir=config.output_dir,
            rms_threshold_dbfs=config.recording.rms_threshold_dbfs,
            min_duration_ms=config.recording.min_duration_ms,
            analyze_last_ms=config.recording.analyze_last_ms,
            timeout_sec=config.recording.timeout_sec,
        )
        self.transcriber = Transcriber(
            api_key=config.api.key,
            api_url=config.api.url,
            model=config.api.model,
            language=config.api.language,
            request_timeout_sec=config.api.request_timeout_sec,
        )

        # Funnel events (possibly from listener / timer threads) through Qt's
        # event loop so all state changes happen on the GUI thread.
        self._pressSignal.connect(self._handle_press)
        self._releaseSignal.connect(self._handle_release)
        self._forceStopSignal.connect(self._handle_force_stop)

    @property
    def status(self) -> Status:
        return self._status

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def mode(self) -> str:
        return self.config.recording.mode

    def _set_status(self, s: Status) -> None:
        if s != self._status:
            log.debug("Status: %s -> %s", self._status, s)
            self._status = s
            self.statusChanged.emit(s)

    def on_key_press(self) -> None:
        """Thread-safe entry point: trigger key went down (from listener thread)."""
        self._pressSignal.emit()

    def on_key_release(self) -> None:
        """Thread-safe entry point: trigger key went up (from listener thread)."""
        self._releaseSignal.emit()

    def _request_force_stop(self) -> None:
        """Thread-safe entry point: recorder timed out (from Timer thread)."""
        self._forceStopSignal.emit()

    def set_paused(self, value: bool) -> None:
        if value == self._paused:
            return
        self._paused = value
        log.info("Pause toggled: %s", value)
        if value and self.recorder.is_recording:
            self.recorder.stop()
            self._set_status(Status.PAUSED)
        elif value:
            self._set_status(Status.PAUSED)
        else:
            self._set_status(Status.IDLE)

    def reload_config(self, config: Config) -> None:
        log.info("Reloading config (mode=%s, trigger_key=%d)",
                 config.recording.mode, config.recording.trigger_key)
        self.config = config
        self.recorder = Recorder(
            output_dir=config.output_dir,
            rms_threshold_dbfs=config.recording.rms_threshold_dbfs,
            min_duration_ms=config.recording.min_duration_ms,
            analyze_last_ms=config.recording.analyze_last_ms,
            timeout_sec=config.recording.timeout_sec,
        )
        self.transcriber = Transcriber(
            api_key=config.api.key,
            api_url=config.api.url,
            model=config.api.model,
            language=config.api.language,
            request_timeout_sec=config.api.request_timeout_sec,
        )

    @Slot()
    def _handle_press(self) -> None:
        if self._paused:
            log.info("Press ignored: engine is paused")
            return

        mode = self.mode
        if mode == "hold":
            if not self.recorder.is_recording:
                self._start_recording()
            # else: spurious press while already recording — ignore
        else:  # toggle
            if not self.recorder.is_recording:
                self._start_recording()
            else:
                self._stop_and_process()

    @Slot()
    def _handle_release(self) -> None:
        if self._paused:
            return
        if self.mode != "hold":
            return  # toggle ignores release
        if self.recorder.is_recording:
            self._stop_and_process()

    @Slot()
    def _handle_force_stop(self) -> None:
        log.info("Force-stop requested (timeout)")
        if self.recorder.is_recording:
            self._stop_and_process()

    def _start_recording(self) -> None:
        try:
            self.recorder.start(on_timeout=self._request_force_stop)
            self._set_status(Status.RECORDING)
        except Exception as e:
            log.exception("Failed to start recording")
            self.errorOccurred.emit(f"Cannot start recording: {e}")
            self._set_status(Status.ERROR)

    def _stop_and_process(self) -> None:
        try:
            result = self.recorder.stop()
        except Exception as e:
            log.exception("Failed to stop recording")
            self.errorOccurred.emit(f"Cannot stop recording: {e}")
            self._set_status(Status.ERROR)
            return

        if result is None or not result.kept:
            self._set_status(Status.IDLE)
            return

        self._set_status(Status.TRANSCRIBING)
        # Heavy work off the GUI thread.
        threading.Thread(
            target=self._process_recording,
            args=(result.path,),
            name="TranscribeWorker",
            daemon=True,
        ).start()

    def _process_recording(self, path) -> None:
        try:
            text = self.transcriber.transcribe(path)
        except TranscriptionError as e:
            log.error("Transcription failed: %s", e)
            self.errorOccurred.emit(f"Transcription failed: {e}")
            self._set_status(Status.ERROR)
            return
        except Exception as e:
            log.exception("Unexpected transcription error")
            self.errorOccurred.emit(f"Unexpected error: {e}")
            self._set_status(Status.ERROR)
            return

        if not text:
            log.info("Transcription returned empty text")
            self._set_status(Status.IDLE)
            return

        ok = clipboard.copy(text, backend=self.config.clipboard.backend)
        if not ok:
            self.errorOccurred.emit(
                "Could not copy to clipboard (install xclip or wl-clipboard)"
            )

        self.transcriptionReady.emit(text)
        if self.config.ui.notifications:
            preview = text if len(text) <= 200 else text[:197] + "..."
            self.notify.emit("Transcribed", preview)
        self._set_status(Status.IDLE)
