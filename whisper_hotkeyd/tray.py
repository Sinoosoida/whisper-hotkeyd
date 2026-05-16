from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from whisper_hotkeyd import __version__
from whisper_hotkeyd.config import CONFIG_PATH, Config
from whisper_hotkeyd.engine import Engine, Status
from whisper_hotkeyd.languages import WHISPER_LANGUAGES, find_index
from whisper_hotkeyd.listener import KeyListener, format_key_name

log = logging.getLogger(__name__)

RESOURCES = Path(__file__).parent / "resources"
ICON_IDLE = RESOURCES / "icon.svg"
ICON_REC = RESOURCES / "icon-recording.svg"
ICON_TRANSCRIBE = RESOURCES / "icon-transcribing.svg"


class _CaptureBridge(QObject):
    """Receives the captured keycode from the listener thread and re-emits
    on the GUI thread via Qt's queued connection."""
    keyCaptured = Signal(int)


class _CaptureDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Press any key")
        self.setModal(True)
        self.captured_code: int | None = None

        layout = QVBoxLayout(self)
        label = QLabel("Press the key you want to use as trigger.\n"
                       "Cancel to keep the current one.")
        label.setMinimumWidth(320)
        layout.addWidget(label)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        layout.addWidget(cancel)

    @Slot(int)
    def _on_captured(self, code: int) -> None:
        self.captured_code = code
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, config: Config, listener: KeyListener | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Whisper Hotkey — Settings")
        self.config = config
        self.listener = listener
        self._trigger_key_value = config.recording.trigger_key

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.api_key = QLineEdit(config.api.key)
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setMinimumWidth(360)
        form.addRow("API key:", self.api_key)

        self.api_url = QLineEdit(config.api.url)
        form.addRow("API URL:", self.api_url)

        self.model = QLineEdit(config.api.model)
        form.addRow("Model:", self.model)

        self.language = QComboBox()
        for code, name in WHISPER_LANGUAGES:
            label = name if not code else f"{name} ({code})"
            self.language.addItem(label, code)
        idx = find_index(config.api.language)
        if idx < 0:
            # Preserve a custom value the user typed in by hand earlier.
            self.language.addItem(
                f"Custom: {config.api.language}", config.api.language
            )
            idx = self.language.count() - 1
        self.language.setCurrentIndex(idx)
        form.addRow("Speech language:", self.language)

        self.request_timeout = QSpinBox()
        self.request_timeout.setRange(5, 3600)
        self.request_timeout.setSuffix(" s")
        self.request_timeout.setValue(config.api.request_timeout_sec)
        form.addRow("HTTP timeout:", self.request_timeout)

        self.max_attempts = QSpinBox()
        self.max_attempts.setRange(1, 10)
        self.max_attempts.setValue(config.api.max_attempts)
        self.max_attempts.setToolTip(
            "Retries on HTTP 429/502/503/504 and network errors."
        )
        form.addRow("Max API attempts:", self.max_attempts)

        self.retry_backoff = QDoubleSpinBox()
        self.retry_backoff.setRange(0.0, 30.0)
        self.retry_backoff.setSingleStep(0.5)
        self.retry_backoff.setSuffix(" s")
        self.retry_backoff.setValue(config.api.retry_backoff_sec)
        self.retry_backoff.setToolTip(
            "Initial backoff; doubles each retry. Server Retry-After overrides."
        )
        form.addRow("Retry backoff:", self.retry_backoff)

        self.mode = QComboBox()
        self.mode.addItem("Toggle — press to start, press again to stop", "toggle")
        self.mode.addItem("Hold — record while key is held", "hold")
        idx = self.mode.findData(config.recording.mode)
        self.mode.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("Mode:", self.mode)

        trigger_row = QWidget()
        h = QHBoxLayout(trigger_row)
        h.setContentsMargins(0, 0, 0, 0)
        self.trigger_label = QLabel(format_key_name(self._trigger_key_value))
        self.capture_btn = QPushButton("Select key...")
        self.capture_btn.clicked.connect(self._capture_key)
        h.addWidget(self.trigger_label, 1)
        h.addWidget(self.capture_btn)
        form.addRow("Trigger key:", trigger_row)

        self.rms = QDoubleSpinBox()
        self.rms.setRange(-120.0, 0.0)
        self.rms.setSingleStep(1.0)
        self.rms.setValue(config.recording.rms_threshold_dbfs)
        form.addRow("RMS threshold (dBFS):", self.rms)

        self.min_dur = QSpinBox()
        self.min_dur.setRange(0, 60000)
        self.min_dur.setSuffix(" ms")
        self.min_dur.setValue(config.recording.min_duration_ms)
        form.addRow("Min duration:", self.min_dur)

        self.timeout = QSpinBox()
        self.timeout.setRange(10, 3600)
        self.timeout.setSuffix(" s")
        self.timeout.setValue(config.recording.timeout_sec)
        form.addRow("Recording timeout:", self.timeout)

        self.clipboard_backend = QComboBox()
        self.clipboard_backend.addItems(["auto", "xclip", "wl-copy"])
        self.clipboard_backend.setCurrentText(config.clipboard.backend)
        form.addRow("Clipboard backend:", self.clipboard_backend)

        self.notifications = QCheckBox("Show notification with transcription")
        self.notifications.setChecked(config.ui.notifications)
        form.addRow(self.notifications)

        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _capture_key(self) -> None:
        if self.listener is None or not self.listener.is_alive():
            QMessageBox.warning(
                self, "Capture unavailable",
                "Key listener is not running. You can still edit "
                "recording.trigger_key in the config file (evdev keycode).",
            )
            return

        dlg = _CaptureDialog(self)
        bridge = _CaptureBridge()
        # Queued connection: emit happens on listener thread, slot runs on GUI thread.
        bridge.keyCaptured.connect(dlg._on_captured, Qt.QueuedConnection)
        self.listener.begin_capture(bridge.keyCaptured.emit)

        try:
            result = dlg.exec()
        finally:
            self.listener.cancel_capture()

        if result == QDialog.Accepted and dlg.captured_code is not None:
            self._trigger_key_value = dlg.captured_code
            self.trigger_label.setText(format_key_name(dlg.captured_code))
            log.info("Trigger key reassigned to %s",
                     format_key_name(dlg.captured_code))

    def to_config(self) -> Config:
        cfg = self.config
        cfg.api.key = self.api_key.text().strip()
        cfg.api.url = self.api_url.text().strip()
        cfg.api.model = self.model.text().strip()
        cfg.api.language = self.language.currentData()
        cfg.api.request_timeout_sec = self.request_timeout.value()
        cfg.api.max_attempts = self.max_attempts.value()
        cfg.api.retry_backoff_sec = self.retry_backoff.value()
        cfg.recording.trigger_key = self._trigger_key_value
        cfg.recording.mode = self.mode.currentData()
        cfg.recording.rms_threshold_dbfs = self.rms.value()
        cfg.recording.min_duration_ms = self.min_dur.value()
        cfg.recording.timeout_sec = self.timeout.value()
        cfg.clipboard.backend = self.clipboard_backend.currentText()
        cfg.ui.notifications = self.notifications.isChecked()
        return cfg


class TrayApp(QObject):
    def __init__(self, app: QApplication, engine: Engine, config: Config,
                 listener: KeyListener) -> None:
        super().__init__()
        self.app = app
        self.engine = engine
        self.config = config
        self.listener = listener

        self._icons = {
            Status.IDLE: self._load_icon(ICON_IDLE),
            Status.RECORDING: self._load_icon(ICON_REC),
            Status.TRANSCRIBING: self._load_icon(ICON_TRANSCRIBE),
            Status.PAUSED: self._load_icon(ICON_IDLE),
            Status.ERROR: self._load_icon(ICON_IDLE),
        }

        self.tray = QSystemTrayIcon(self._icons[Status.IDLE])
        self.tray.setToolTip(self._tooltip(Status.IDLE))

        self._build_menu()
        self.tray.setVisible(True)

        engine.statusChanged.connect(self._on_status_changed)
        engine.transcriptionReady.connect(self._on_transcription)
        engine.errorOccurred.connect(self._on_error)
        engine.notify.connect(self._on_notify)

        if not config.is_configured():
            self.tray.showMessage(
                "Whisper Hotkey",
                "API key is not set. Open Settings from the tray menu.",
                QSystemTrayIcon.Warning,
                8000,
            )

    @staticmethod
    def _load_icon(path: Path) -> QIcon:
        if not path.exists():
            log.warning("Icon not found: %s", path)
            return QIcon()
        return QIcon(QPixmap(str(path)))

    def _tooltip(self, status: Status) -> str:
        return (f"Whisper Hotkey — {status.value}  "
                f"[{self.config.recording.mode}, "
                f"{format_key_name(self.config.recording.trigger_key)}]")

    def _build_menu(self) -> None:
        menu = QMenu()

        self.status_action = QAction("Status: idle")
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)

        self.last_text_action = QAction("Last: (none)")
        self.last_text_action.setEnabled(False)
        menu.addAction(self.last_text_action)

        menu.addSeparator()

        self.pause_action = QAction("Pause")
        self.pause_action.setCheckable(True)
        self.pause_action.toggled.connect(self.engine.set_paused)
        menu.addAction(self.pause_action)

        settings_action = QAction("Settings...")
        settings_action.triggered.connect(self._open_settings)
        menu.addAction(settings_action)

        open_cfg_action = QAction("Open config file")
        open_cfg_action.triggered.connect(lambda: self._xdg_open(CONFIG_PATH))
        menu.addAction(open_cfg_action)

        open_rec_action = QAction("Open recordings folder")
        open_rec_action.triggered.connect(lambda: self._xdg_open(self.config.output_dir))
        menu.addAction(open_rec_action)

        from whisper_hotkeyd.config import LOG_DIR
        open_log_action = QAction("Open log folder")
        open_log_action.triggered.connect(lambda: self._xdg_open(LOG_DIR))
        menu.addAction(open_log_action)

        menu.addSeparator()

        about_action = QAction(f"About (v{__version__})")
        about_action.triggered.connect(self._about)
        menu.addAction(about_action)

        quit_action = QAction("Quit")
        quit_action.triggered.connect(self.app.quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)

    @Slot(Status)
    def _on_status_changed(self, status: Status) -> None:
        self.tray.setIcon(self._icons.get(status, self._icons[Status.IDLE]))
        self.tray.setToolTip(self._tooltip(status))
        self.status_action.setText(f"Status: {status.value}")

    @Slot(str)
    def _on_transcription(self, text: str) -> None:
        preview = text if len(text) <= 80 else text[:77] + "..."
        self.last_text_action.setText(f"Last: {preview}")

    @Slot(str, str)
    def _on_notify(self, title: str, body: str) -> None:
        self.tray.showMessage(title, body, QSystemTrayIcon.Information, 4000)

    @Slot(str)
    def _on_error(self, message: str) -> None:
        self.tray.showMessage("Whisper Hotkey — error", message,
                              QSystemTrayIcon.Critical, 6000)

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.config, self.listener)
        if dlg.exec() == QDialog.Accepted:
            cfg = dlg.to_config()
            cfg.save()
            self.config = cfg
            self.engine.reload_config(cfg)
            self.listener.set_trigger_key(cfg.recording.trigger_key)
            self.tray.setToolTip(self._tooltip(self.engine.status))
            log.info("Settings saved (mode=%s, key=%s)",
                     cfg.recording.mode,
                     format_key_name(cfg.recording.trigger_key))

    def _about(self) -> None:
        QMessageBox.information(
            None,
            "Whisper Hotkey",
            f"Whisper Hotkey v{__version__}\n\n"
            "Push-to-talk voice transcription to clipboard.\n"
            f"Config: {CONFIG_PATH}",
        )

    @staticmethod
    def _xdg_open(path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(
                ["xdg-open", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            log.error("xdg-open not available")
