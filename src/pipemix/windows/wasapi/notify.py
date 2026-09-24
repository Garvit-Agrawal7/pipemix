"""Endpoint hotplug → the connect/disconnect callbacks the Controller expects.

This is the Windows replacement for the BlueZ `DeviceMonitor`, and it keeps
the same surface: set `on_connect` / `on_disconnect`, call `start()`. What it
reports is an endpoint id rather than a MAC, and it reports every endpoint,
not just Bluetooth ones — MMDevice does not distinguish, and neither do we.

COM delivers these notifications on an arbitrary MTA thread and forbids doing
real work there, so the client does nothing but drop an id on a queue; a
worker thread resolves the endpoint's actual state and fires the callbacks.
That also collapses the duplicate events Windows emits for one physical
reconnect (`OnDeviceAdded` and `OnDeviceStateChanged` both fire), since the
worker only reports a change from the state it last knew.

The registration itself happens **on that worker thread**, not on whoever
calls `start()`, and that is not a detail. `comtypes.CoInitialize()` puts the
calling thread in a single-threaded apartment, and COM marshals calls to an
object registered from an STA back onto that one thread — where they are only
delivered while it pumps a Windows message loop. `main.py --cli` blocks on
`threading.Event().wait()` and never pumps, so every hotplug notification sat
in a queue nobody drained and a disconnected headset went unnoticed. Owning
the enumerator from an MTA thread removes the requirement entirely.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

import comtypes
from pycaw.api.mmdeviceapi import IMMNotificationClient
from pycaw.constants import DEVICE_STATE, EDataFlow
from pycaw.utils import AudioUtilities

log = logging.getLogger(__name__)

_STOP = object()


class _NotificationClient(comtypes.COMObject):
    """Bare relay. Every method must return promptly and touch nothing COM."""

    _com_interfaces_ = [IMMNotificationClient]

    def __init__(self, put: Callable[[str], None]) -> None:
        self._put = put
        super().__init__()

    def OnDeviceStateChanged(self, this, pwstrDeviceId, dwNewState):
        self._put(pwstrDeviceId)
        return 0

    def OnDeviceAdded(self, this, pwstrDeviceId):
        self._put(pwstrDeviceId)
        return 0

    def OnDeviceRemoved(self, this, pwstrDeviceId):
        self._put(pwstrDeviceId)
        return 0

    def OnDefaultDeviceChanged(self, this, flow, role, pwstrDefaultDeviceId):
        return 0

    def OnPropertyValueChanged(self, this, pwstrDeviceId, key):
        return 0


class DeviceMonitor:
    """Calls on_connect / on_disconnect with an endpoint id as devices come and go."""

    def __init__(self) -> None:
        self.on_connect:    Callable[[str], None] | None = None
        self.on_disconnect: Callable[[str], None] | None = None

        self._queue: queue.Queue = queue.Queue()
        self._enumerator = None
        self._client: _NotificationClient | None = None
        self._worker: threading.Thread | None = None
        self._active: set[str] = set()
        self._ready = threading.Event()
        self._error: Exception | None = None

    def start(self) -> None:
        """Blocks until the callback is registered, and raises if it could not be."""
        self._worker = threading.Thread(target=self._drain, name="wasapi-notify", daemon=True)
        self._worker.start()
        self._ready.wait(timeout=5)
        if self._error:
            raise self._error
        log.info("DeviceMonitor started — %d active render endpoint(s).", len(self._active))

    def stop(self) -> None:
        # Unregistering has to happen on the thread that registered, so the
        # worker does it on its way out.
        self._queue.put(_STOP)
        if self._worker is not None:
            self._worker.join(timeout=2)
        self._worker = None
        log.info("DeviceMonitor stopped.")

    def connected(self) -> list[str]:
        """Endpoint ids currently active, for the Controller's initial state."""
        return sorted(self._active)

    def _render_endpoints(self) -> list:
        from pipemix.windows.wasapi.devices import list_outputs
        return list_outputs()

    def _is_active(self, device_id: str) -> bool:
        try:
            dev = self._enumerator.GetDevice(device_id)
            return dev.GetState() == DEVICE_STATE.ACTIVE.value
        except Exception:
            return False  # gone entirely

    def _is_render(self, device_id: str) -> bool:
        from pycaw.api.mmdeviceapi import IMMEndpoint
        try:
            dev = self._enumerator.GetDevice(device_id)
            return dev.QueryInterface(IMMEndpoint).GetDataFlow() == EDataFlow.eRender.value
        except Exception:
            return device_id in self._active  # removed: trust what we knew

    def _drain(self) -> None:
        # This thread owns the enumerator and the callback registration, so
        # COM delivers notifications straight here with no message pump in the
        # picture. It is also where the ids get resolved and where everything
        # downstream (rebuilding legs) runs, which is all real COM work.
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            try:
                self._enumerator = AudioUtilities.GetDeviceEnumerator()
                self._active = {d.id for d in self._render_endpoints()}
                self._client = _NotificationClient(self._queue.put)
                self._enumerator.RegisterEndpointNotificationCallback(self._client)
            except Exception as e:
                self._error = e
                log.error("DeviceMonitor could not register for notifications: %s", e)
                return
            finally:
                self._ready.set()

            while True:
                item = self._queue.get()
                if item is _STOP:
                    return
                try:
                    self._handle(item)
                except Exception:
                    log.exception("Error handling endpoint change for %s", item)
        finally:
            if self._enumerator is not None and self._client is not None:
                try:
                    self._enumerator.UnregisterEndpointNotificationCallback(self._client)
                except Exception:
                    log.debug("Unregister failed on shutdown", exc_info=True)
            self._client = None
            self._enumerator = None
            comtypes.CoUninitialize()

    def _handle(self, device_id: str) -> None:
        if not self._is_render(device_id):
            return
        active = self._is_active(device_id)
        if active and device_id not in self._active:
            self._active.add(device_id)
            log.info("[endpoint] connected:    %s", device_id)
            if self.on_connect:
                self.on_connect(device_id)
        elif not active and device_id in self._active:
            self._active.discard(device_id)
            log.info("[endpoint] disconnected: %s", device_id)
            if self.on_disconnect:
                self.on_disconnect(device_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    comtypes.CoInitialize()
    mon = DeviceMonitor()
    mon.on_connect = lambda i: print("CONNECT   ", i)
    mon.on_disconnect = lambda i: print("DISCONNECT", i)
    mon.start()
    print("Watching endpoints. Plug or unplug something; Ctrl-C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        mon.stop()
