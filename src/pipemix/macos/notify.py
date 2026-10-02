"""Device hotplug → the connect/disconnect callbacks the Controller expects.

The macOS replacement for the BlueZ `DeviceMonitor` and for
`windows/wasapi/notify.py`, with the same surface: set `on_connect` /
`on_disconnect`, call `start()`. It reports a device UID.

Core Audio only says "the device list changed", on a HAL thread that must
not block. So the listener just pokes a queue, and a worker diffs the set of
output UIDs against the last one it saw. That also swallows the changes we
cause ourselves — creating and destroying the hub moves the list too, but
the hub is never an output.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

from pipemix.macos import coreaudio as ca
from pipemix.macos.devices import output_uids

log = logging.getLogger(__name__)

_STOP = object()
_CHANGED = object()

# A Bluetooth device often shows up a moment before it can take audio.
SETTLE_S = 0.5


class DeviceMonitor:
    """Calls on_connect / on_disconnect with a device UID as outputs come and go."""

    def __init__(self) -> None:
        self.on_connect:    Callable[[str], None] | None = None
        self.on_disconnect: Callable[[str], None] | None = None

        self._queue: queue.Queue = queue.Queue()
        self._listener: ca.Listener | None = None
        self._worker: threading.Thread | None = None
        self._known: set[str] = set()

    def start(self) -> None:
        self._known = output_uids()
        self._listener = ca.Listener(lambda: self._queue.put(_CHANGED))
        self._listener.add()
        self._worker = threading.Thread(target=self._drain, name="coreaudio-notify", daemon=True)
        self._worker.start()
        log.info("DeviceMonitor started — %d output(s).", len(self._known))

    def stop(self) -> None:
        if self._listener:
            try:
                self._listener.remove()
            except Exception:
                log.debug("Removing the device listener failed", exc_info=True)
            self._listener = None
        self._queue.put(_STOP)
        if self._worker is not None:
            self._worker.join(timeout=2)
        self._worker = None
        log.info("DeviceMonitor stopped.")

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            # Collapse a burst (one reconnect fires several changes) into one diff.
            stop = False
            try:
                while True:
                    item = self._queue.get(timeout=SETTLE_S)
                    if item is _STOP:
                        stop = True
                        break
            except queue.Empty:
                pass
            try:
                self._handle()
            except Exception:
                log.exception("Error handling a device list change")
            if stop:
                return

    def _handle(self) -> None:
        now = output_uids()
        gone, new = self._known - now, now - self._known
        self._known = now
        for device_uid in sorted(gone):
            log.info("[device] disconnected: %s", device_uid)
            if self.on_disconnect:
                self.on_disconnect(device_uid)
        for device_uid in sorted(new):
            log.info("[device] connected:    %s", device_uid)
            if self.on_connect:
                self.on_connect(device_uid)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    mon = DeviceMonitor()
    mon.on_connect = lambda i: print("CONNECT   ", i)
    mon.on_disconnect = lambda i: print("DISCONNECT", i)
    mon.start()
    print("Watching outputs. Connect or disconnect something; Ctrl-C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        mon.stop()
