from __future__ import annotations

import logging
import queue
import threading
import time
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from whisper_hotkeyd import clipboard
from whisper_hotkeyd.config import Config
from whisper_hotkeyd.recorder import Recorder
from whisper_hotkeyd.transcriber import Transcriber, TranscriptionError

log = logging.getLogger(__name__)

# Sentinel enqueued to make the transcription worker exit cleanly on shutdown.
_STOP = object()


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

    Transcription is serialized: every kept recording is put on a FIFO queue and
    a single worker thread transcribes them one at a time, in order. After a
    transcription that produced text, if more work is queued the worker waits
    `ui.paste_delay_sec` before starting the next one, so the clipboard result
    can be pasted before it is overwritten.
    """

    statusChanged = Signal(Status)
    transcriptionReady = Signal(str)
    errorOccurred = Signal(str)
    notify = Signal(str, str)  # title, body
    retryAvailable = Signal(bool)  # True when a failed recording can be re-sent

    _pressSignal = Signal()
    _releaseSignal = Signal()
    _forceStopSignal = Signal()
    # The transcription worker never mutates engine state directly; it marshals
    # status/retry updates onto the GUI thread through these, so every write to
    # _status / _last_audio_path happens on one thread (no cross-thread race).
    _statusDirtySignal = Signal()
    _retrySignal = Signal(object)  # Path to arm, or None to clear

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self._paused = False
        self._status = Status.IDLE
        # Path of the last recording that failed to transcribe, kept on disk
        # so it can be re-sent without re-recording. None when nothing to retry.
        self._last_audio_path: Path | None = None
        # Toggle-mode press parity: a binary "should I be recording?" intent that
        # flips on EVERY trigger press, independent of whether audio is actually
        # being captured. This makes the press AFTER a recording that ended on
        # its own (e.g. the 300s timeout) a no-op "stop" rather than the start of
        # a fresh (and, with a shared mute/trigger key, silent) recording.
        # Reset to False (expecting start) on pause/reload. Only used in toggle.
        self._intent = False

        # Serialized transcription: FIFO queue drained by one worker thread.
        self._jobs: "queue.Queue" = queue.Queue()
        self._busy = False  # True while the worker is mid-transcription
        self._paste_delay = max(0.0, config.ui.paste_delay_sec)

        self.recorder = self._build_recorder(config)
        self.transcriber = self._build_transcriber(config)

        # Funnel events (possibly from listener / timer threads) through Qt's
        # event loop so all state changes happen on the GUI thread.
        self._pressSignal.connect(self._handle_press)
        self._releaseSignal.connect(self._handle_release)
        self._forceStopSignal.connect(self._handle_force_stop)
        self._statusDirtySignal.connect(self._refresh_status)
        self._retrySignal.connect(self._set_retry)

        self._worker = threading.Thread(
            target=self._worker_loop, name="TranscribeWorker", daemon=True
        )
        self._worker.start()

    # --- construction helpers ---

    @staticmethod
    def _build_recorder(config: Config) -> Recorder:
        return Recorder(
            output_dir=config.output_dir,
            rms_threshold_dbfs=config.recording.rms_threshold_dbfs,
            min_duration_ms=config.recording.min_duration_ms,
            analyze_last_ms=config.recording.analyze_last_ms,
            timeout_sec=config.recording.timeout_sec,
        )

    @staticmethod
    def _build_transcriber(config: Config) -> Transcriber:
        return Transcriber(
            api_key=config.api.key,
            api_url=config.api.url,
            model=config.api.model,
            language=config.api.language,
            request_format=config.api.request_format,
            request_timeout_sec=config.api.request_timeout_sec,
            max_attempts=config.api.max_attempts,
            retry_backoff_sec=config.api.retry_backoff_sec,
            proxy=config.api.proxy,
        )

    # --- properties ---

    @property
    def status(self) -> Status:
        return self._status

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def mode(self) -> str:
        return self.config.recording.mode

    # --- status ---

    def _set_status(self, s: Status) -> None:
        if s != self._status:
            log.debug("Status: %s -> %s", self._status, s)
            self._status = s
            self.statusChanged.emit(s)

    def _compute_status(self) -> Status:
        if self._paused:
            return Status.PAUSED
        if self.recorder.is_recording:
            return Status.RECORDING
        if self._busy or not self._jobs.empty():
            return Status.TRANSCRIBING
        return Status.IDLE

    def _refresh_status(self) -> None:
        self._set_status(self._compute_status())

    # --- thread-safe entry points (listener / timer threads) ---

    def on_key_press(self) -> None:
        """Trigger key went down (from listener thread)."""
        self._pressSignal.emit()

    def on_key_release(self) -> None:
        """Trigger key went up (from listener thread)."""
        self._releaseSignal.emit()

    def _request_force_stop(self) -> None:
        """Recorder timed out (from Timer thread)."""
        self._forceStopSignal.emit()

    # --- retry ---

    def _set_retry(self, path: Path | None) -> None:
        """Arm (path set) or clear (None) the 'retry last transcription' action."""
        self._last_audio_path = path
        self.retryAvailable.emit(path is not None)

    @Slot()
    def retry_last(self) -> None:
        """Re-queue the last failed recording for transcription.

        Runs on the GUI thread (tray menu / notification click). Kept WAVs are
        never deleted after transcription, so a recording that failed (e.g. the
        provider returned 429) can be sent again without re-recording it. The
        job joins the same serialized queue as normal recordings.
        """
        if self._paused or self.recorder.is_recording:
            log.info("Retry ignored (paused=%s, recording=%s)",
                     self._paused, self.recorder.is_recording)
            return
        path = self._last_audio_path
        if path is None:
            return
        if not path.exists():
            log.warning("Retry: recording no longer on disk: %s", path)
            self.errorOccurred.emit("Cannot retry — the recording file is gone")
            self._set_retry(None)
            return
        log.info("Retrying transcription: %s", path.name)
        self._set_retry(None)
        self._enqueue(path)

    # --- config / lifecycle ---

    def set_paused(self, value: bool) -> None:
        if value == self._paused:
            return
        self._paused = value
        self._intent = False  # realign toggle parity after pausing/unpausing
        log.info("Pause toggled: %s", value)
        if value and self.recorder.is_recording:
            # Discard the in-progress recording when pausing.
            self.recorder.stop()
        self._refresh_status()

    def reload_config(self, config: Config) -> None:
        log.info("Reloading config (mode=%s, trigger_key=%d)",
                 config.recording.mode, config.recording.trigger_key)
        # Stop any in-flight recording before swapping the recorder, otherwise
        # the old arecord process and its timeout Timer are orphaned.
        if self.recorder.is_recording:
            log.info("Stopping in-flight recording before applying new config")
            try:
                self.recorder.stop()
            except Exception:
                log.exception("Error stopping recorder during config reload")
        self.config = config
        self._paste_delay = max(0.0, config.ui.paste_delay_sec)
        self.recorder = self._build_recorder(config)
        self.transcriber = self._build_transcriber(config)
        self._intent = False
        self._refresh_status()

    def shutdown(self) -> None:
        """Stop an in-flight recording and the transcription worker at exit.

        arecord runs in its own session (setsid) and would otherwise outlive the
        process, writing the WAV unbounded; the worker is asked to exit so a
        clean shutdown doesn't rely solely on the daemon-thread kill."""
        try:
            if self.recorder.is_recording:
                log.info("Shutdown: stopping in-flight recording")
                self.recorder.stop()
        except Exception:
            log.exception("Error during engine shutdown")
        self._jobs.put(_STOP)
        self._worker.join(timeout=2.0)

    # --- trigger handling (GUI thread) ---

    @Slot()
    def _handle_press(self) -> None:
        if self._paused:
            log.info("Press ignored: engine is paused")
            return

        if self.mode == "hold":
            if not self.recorder.is_recording:
                self._start_recording()
            # else: spurious press while already recording — ignore
            return

        # toggle: flip a binary intent on every press, independent of whether
        # audio is actually being captured. So the press after a recording that
        # ended on its own (timeout) is absorbed as the "stop" the user meant,
        # keeping their press count aligned with a shared mute/trigger key.
        self._intent = not self._intent
        if self._intent:
            if not self.recorder.is_recording:
                self._start_recording()
            else:
                log.debug("toggle press: START intent but already recording")
        else:
            if self.recorder.is_recording:
                self._stop_and_process()
            else:
                log.info("toggle press: STOP absorbed (recording already ended)")

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
            self._refresh_status()  # -> RECORDING
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
            # Surface a muted mic: pure digital silence (RMS -inf) almost always
            # means the input is muted, which otherwise just looks like the app
            # "did nothing". (A merely-quiet room has a finite dBFS.)
            if (result is not None and result.reason == "too_quiet"
                    and result.rms_dbfs == float("-inf")
                    and self.config.ui.notifications):
                self.notify.emit(
                    "No speech recorded",
                    "The recording was silent — is your microphone muted?",
                )
            self._refresh_status()  # back to IDLE / TRANSCRIBING
            return

        # A fresh recording supersedes any previously-failed one.
        self._set_retry(None)
        self._enqueue(result.path)

    # --- serialized transcription queue ---

    def _enqueue(self, path: Path) -> None:
        self._jobs.put(path)
        self._refresh_status()  # -> TRANSCRIBING (work is pending)

    def _worker_loop(self) -> None:
        while True:
            item = self._jobs.get()
            if item is _STOP:
                self._jobs.task_done()
                return
            try:
                self._busy = True
                self._statusDirtySignal.emit()  # -> TRANSCRIBING (GUI thread)
                copied = self._transcribe_one(item)
                self._busy = False
                more = not self._jobs.empty()
                self._statusDirtySignal.emit()  # -> TRANSCRIBING (more) or IDLE
                # Pause only when there is a fresh clipboard result to protect
                # AND another job is waiting; a failed/empty job copied nothing,
                # so don't needlessly slow the queue (e.g. during a 429 storm).
                if more and copied and self._paste_delay > 0:
                    log.debug("Waiting %.1fs before next queued transcription",
                              self._paste_delay)
                    time.sleep(self._paste_delay)
            except Exception:
                # Backstop: one bad job must never kill the worker, which would
                # otherwise freeze the whole queue.
                log.exception("Transcription worker caught unexpected error")
                self._busy = False
                self._statusDirtySignal.emit()
            finally:
                self._jobs.task_done()

    def _transcribe_one(self, path: Path) -> bool:
        """Transcribe one recording. Returns True iff it produced text that was
        copied — used to decide whether the post-job paste pause is needed."""
        try:
            text = self.transcriber.transcribe(path)
        except TranscriptionError as e:
            log.error("Transcription failed: %s", e)
            self._retrySignal.emit(path)   # arm retry (applied on GUI thread)
            self.errorOccurred.emit(f"Transcription failed: {e}")
            return False
        except Exception as e:
            log.exception("Unexpected transcription error")
            self._retrySignal.emit(path)
            self.errorOccurred.emit(f"Unexpected error: {e}")
            return False

        if not text:
            log.info("Transcription returned empty text")
            return False

        ok = clipboard.copy(text, backend=self.config.clipboard.backend)
        if not ok:
            self.errorOccurred.emit(
                "Could not copy to clipboard (install xclip or wl-clipboard)"
            )
        # A successful transcription is the latest outcome, so clear any retry
        # armed by an older failed clip.
        self._retrySignal.emit(None)
        self.transcriptionReady.emit(text)
        if self.config.ui.notifications:
            preview = text if len(text) <= 200 else text[:197] + "..."
            self.notify.emit("Transcribed", preview)
        return True
