from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

from whisper_hotkeyd import __version__
from whisper_hotkeyd.config import CONFIG_PATH, LOG_DIR, Config
from whisper_hotkeyd.logsetup import LOG_FILE, setup_logging

log = logging.getLogger(__name__)


def _instance_lock_name() -> str:
    # Per-user named socket. Different users on the same host don't collide.
    return f"whisper-hotkeyd-{os.getuid()}"


def _claim_single_instance(app):
    """Try to become the only running instance.

    Returns the QLocalServer on success (the caller must keep a reference to
    it for the lifetime of the app), or None if another live instance was
    detected (caller should exit).
    """
    from PySide6.QtNetwork import QLocalServer, QLocalSocket

    name = _instance_lock_name()

    probe = QLocalSocket()
    probe.connectToServer(name)
    if probe.waitForConnected(500):
        # Existing instance answered — ring the doorbell and bail.
        try:
            probe.write(b"raise\n")
            probe.waitForBytesWritten(500)
        finally:
            probe.disconnectFromServer()
        return None

    # No live listener (or stale socket from a crashed process) — claim it.
    QLocalServer.removeServer(name)
    server = QLocalServer(app)
    if not server.listen(name):
        log.warning(
            "Could not claim instance lock '%s' (%s); continuing without it.",
            name, server.errorString(),
        )
    return server


def _run_tray(verbose: bool) -> int:
    setup_logging(level=logging.DEBUG if verbose else logging.INFO)
    log.info("whisper-hotkeyd v%s starting", __version__)

    try:
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon
    except ImportError:
        log.critical("PySide6 is required. Install it: pip install PySide6")
        return 1

    config, created = Config.load()
    if created:
        log.info("Created default config at %s", CONFIG_PATH)

    from whisper_hotkeyd.setup_helpers import sync_autostart
    sync_autostart(config.ui.autostart_managed)

    app = QApplication(sys.argv)
    app.setApplicationName("whisper-hotkeyd")
    app.setApplicationDisplayName("Whisper Hotkey")
    app.setQuitOnLastWindowClosed(False)

    instance_server = _claim_single_instance(app)
    if instance_server is None:
        log.info("Another instance is already running; exiting.")
        print("whisper-hotkeyd is already running.", file=sys.stderr)
        return 0

    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.critical("System tray is not available on this desktop session")
        return 1

    from whisper_hotkeyd.engine import Engine
    from whisper_hotkeyd.listener import InputAccessError, KeyListener
    from whisper_hotkeyd.tray import TrayApp

    engine = Engine(config)
    listener = KeyListener(
        trigger_key=config.recording.trigger_key,
        on_press=engine.on_key_press,
        on_release=engine.on_key_release,
    )
    tray = TrayApp(app, engine, config, listener)
    listener.start()

    def _on_second_launch():
        sock = instance_server.nextPendingConnection()
        if sock is not None:
            sock.disconnectFromServer()
        log.info("Second launch attempt suppressed by single-instance lock")
        tray.tray.showMessage(
            "Whisper Hotkey",
            "Already running — second launch ignored.",
            QSystemTrayIcon.Information,
            4000,
        )
    instance_server.newConnection.connect(_on_second_launch)

    # Surface input-group issues to the user via tray after a short delay.
    from PySide6.QtCore import QTimer
    def _check_listener_alive():
        if not listener.is_alive():
            tray.tray.showMessage(
                "Whisper Hotkey — key listener failed",
                "Could not access /dev/input. Add yourself to the 'input' "
                "group: sudo gpasswd -a $USER input, then log out and back in.",
                QSystemTrayIcon.Critical,
                10000,
            )
            log.error("Key listener exited; trigger will not work")
    QTimer.singleShot(2000, _check_listener_alive)

    # Make Ctrl+C in terminal launches actually quit.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    # Wake up the Qt event loop periodically so SIGINT can be processed.
    timer = QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    rc = app.exec()
    listener.stop()
    listener.join(timeout=2.0)
    log.info("whisper-hotkeyd exiting (rc=%d)", rc)
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="whisper-hotkeyd",
        description="Push-to-talk voice transcription to clipboard.",
    )
    parser.add_argument("--version", action="version",
                        version=f"whisper-hotkeyd {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")
    parser.add_argument("--setup", action="store_true",
                        help="Run interactive setup (input group check, autostart)")
    parser.add_argument("--config-path", action="store_true",
                        help="Print path to config file and exit")
    parser.add_argument("--log-path", action="store_true",
                        help="Print path to log file and exit")

    args = parser.parse_args(argv)

    if args.config_path:
        print(CONFIG_PATH)
        return 0
    if args.log_path:
        print(LOG_FILE)
        return 0
    if args.setup:
        setup_logging(level=logging.INFO)
        from whisper_hotkeyd.setup_helpers import run_setup_wizard
        return run_setup_wizard()

    return _run_tray(verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
