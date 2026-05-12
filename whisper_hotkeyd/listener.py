from __future__ import annotations

import errno
import logging
import select
import threading
from typing import Callable

import evdev

log = logging.getLogger(__name__)


class InputAccessError(Exception):
    """Raised when /dev/input cannot be read (user not in 'input' group)."""


_KEY_NAME_SPECIAL = {
    "BACKSPACE": "Backspace",
    "CAPSLOCK": "Caps Lock",
    "NUMLOCK": "Num Lock",
    "SCROLLLOCK": "Scroll Lock",
    "PRINTSCREEN": "Print Screen",
    "SYSRQ": "SysRq",
}


def format_key_name(code: int) -> str:
    """Human-readable label for an evdev keycode (e.g. 100 -> 'Right Alt (100)')."""
    if not code:
        return "—"
    name = evdev.ecodes.keys.get(code)
    if isinstance(name, list):
        name = name[0]
    if not (name and isinstance(name, str) and name.startswith("KEY_")):
        return f"keycode {code}"
    raw = name[4:]
    if raw in _KEY_NAME_SPECIAL:
        return f"{_KEY_NAME_SPECIAL[raw]} ({code})"
    import re
    parts = [p for p in re.split(r"(LEFT|RIGHT|PAGE)", raw) if p]
    pretty = " ".join(p.title() for p in parts).replace("_", " ")
    return f"{pretty} ({code})"


class KeyListener(threading.Thread):
    """Background thread that watches all keyboard-like evdev devices.

    Fires on_press()/on_release() for the configured trigger key, ignoring
    auto-repeat events. While a capture callback is registered via
    begin_capture(), the next key-down across ANY device is delivered to it
    instead of producing a normal trigger.
    """

    def __init__(
        self,
        trigger_key: int,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
    ) -> None:
        super().__init__(name="KeyListener", daemon=True)
        self._trigger_key = trigger_key
        self.on_press = on_press
        self.on_release = on_release
        self._stop_event = threading.Event()
        self._capture_cb: Callable[[int], None] | None = None
        self._capture_lock = threading.Lock()

    @property
    def trigger_key(self) -> int:
        return self._trigger_key

    def set_trigger_key(self, code: int) -> None:
        if code != self._trigger_key:
            log.info("Trigger key changed: %d -> %d", self._trigger_key, code)
            self._trigger_key = code

    def begin_capture(self, callback: Callable[[int], None]) -> None:
        """Forward the next key-down (any code, any device) to `callback`.

        The capture is one-shot — it auto-clears after firing or when
        cancel_capture() is called.
        """
        with self._capture_lock:
            self._capture_cb = callback
        log.debug("Capture mode armed")

    def cancel_capture(self) -> None:
        with self._capture_lock:
            self._capture_cb = None
        log.debug("Capture mode disarmed")

    def stop(self) -> None:
        self._stop_event.set()

    @staticmethod
    def _open_keyboards() -> list[evdev.InputDevice]:
        """Open every readable evdev device whose capability includes KEY_A.

        That filter excludes mice/touchpads (only BTN_*) and power buttons,
        and lets us listen on multiple physical keyboards at once.
        """
        devices: list[evdev.InputDevice] = []
        access_denied = False
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except PermissionError:
                access_denied = True
                continue
            except OSError as e:
                if e.errno in (errno.EACCES, errno.EPERM):
                    access_denied = True
                continue
            caps = dev.capabilities().get(evdev.ecodes.EV_KEY, [])
            if evdev.ecodes.KEY_A in caps:
                log.info("Listening on %s (%s)", dev.path, dev.name)
                devices.append(dev)
            else:
                dev.close()

        if not devices and access_denied:
            raise InputAccessError(
                "No accessible input devices. Add your user to the 'input' "
                "group and re-login (sudo gpasswd -a $USER input)."
            )
        if not devices:
            raise InputAccessError("No keyboard-like input device found.")
        return devices

    def run(self) -> None:
        try:
            devices = self._open_keyboards()
        except InputAccessError:
            log.exception("Cannot open input devices")
            return
        except Exception:
            log.exception("Unexpected error opening input devices")
            return

        fd_to_dev = {dev.fd: dev for dev in devices}
        log.info("Key listener started, trigger_key=%d, devices=%d",
                 self._trigger_key, len(devices))

        try:
            while not self._stop_event.is_set():
                try:
                    r, _, _ = select.select(fd_to_dev.keys(), [], [], 0.5)
                except OSError:
                    log.exception("select() failed")
                    break

                for fd in r:
                    dev = fd_to_dev[fd]
                    try:
                        for event in dev.read():
                            self._dispatch(event)
                    except OSError as e:
                        log.warning("Device %s read error: %s; dropping",
                                    dev.path, e)
                        fd_to_dev.pop(fd, None)
                        try:
                            dev.close()
                        except Exception:
                            pass

                if not fd_to_dev:
                    log.error("All input devices gone, listener stopping")
                    break
        finally:
            for dev in fd_to_dev.values():
                try:
                    dev.close()
                except Exception:
                    pass
            log.info("Key listener stopped")

    def _dispatch(self, event) -> None:
        if event.type != evdev.ecodes.EV_KEY:
            return
        if event.value == 2:  # auto-repeat
            return
        if event.value not in (0, 1):
            return

        if event.value == 1:
            with self._capture_lock:
                cb = self._capture_cb
                if cb is not None:
                    self._capture_cb = None
            if cb is not None:
                log.info("Captured keycode %d", event.code)
                try:
                    cb(event.code)
                except Exception:
                    log.exception("Capture callback failed")
                return

        if event.code != self._trigger_key:
            return

        try:
            if event.value == 1:
                log.debug("Trigger pressed")
                self.on_press()
            else:
                log.debug("Trigger released")
                self.on_release()
        except Exception:
            log.exception("Listener callback failed")
