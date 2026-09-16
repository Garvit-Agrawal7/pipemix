"""
ControllerAdapter — bridges GLib GObject signals to PyQt6 pyqtSignals.

Why this exists:
  The Controller inherits from GObject.Object and emits signals via GLib's
  signal system (controller.connect("devices-changed", callback)).
  PyQt6 widgets expect PyQt6 signals (pyqtSignal), not GLib signals.

  This adapter:
    1. Subscribes to all three Controller GLib signals.
    2. Re-emits them as proper pyqtSignals that any QWidget can connect to.
    3. Proxies all Controller method calls transparently via __getattr__,
       so the UI never needs to import or hold a direct Controller reference.

  The UI connects to the adapter exactly as it would connect to the Controller —
  just using Qt's signal/slot system instead of GLib's.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, pyqtSignal

from pipemix.models import AudioDevice, SessionState

if TYPE_CHECKING:
    from pipemix.controller import Controller
    from pipemix.services.backend import BackendStatus

log = logging.getLogger(__name__)


class ControllerAdapter(QObject):
    """
    Wraps a Controller and re-emits its GLib signals as Qt signals.

    Connect to these exactly as you would with any Qt signal:
        adapter.devices_changed.connect(my_slot)
        adapter.state_changed.connect(my_slot)
        adapter.health_changed.connect(my_slot)

    Call Controller methods directly on the adapter:
        adapter.start_sharing(devices)
        adapter.stop_sharing()
        adapter.reset_audio()
        adapter.devices          # property access also works
        adapter.session          # etc.
    """

    # Qt signals — identical semantics to the GLib originals
    devices_changed = pyqtSignal(list)    # list[AudioDevice]
    state_changed   = pyqtSignal(object)  # SessionState
    health_changed  = pyqtSignal(object)  # BackendStatus

    def __init__(self, controller: "Controller") -> None:
        super().__init__()
        self._ctrl = controller

        # Subscribe to all three GLib signals on the Controller
        controller.connect("devices-changed", self._on_devices)
        controller.connect("state-changed",   self._on_state)
        controller.connect("health-changed",  self._on_health)

        log.debug("ControllerAdapter connected to Controller GLib signals.")

    # ---- GLib signal callbacks → re-emit as Qt signals ----

    def _on_devices(self, _ctrl: "Controller", devices: list[AudioDevice]) -> None:
        self.devices_changed.emit(devices)

    def _on_state(self, _ctrl: "Controller", state: "SessionState") -> None:
        self.state_changed.emit(state)

    def _on_health(self, _ctrl: "Controller", status: "BackendStatus") -> None:
        self.health_changed.emit(status)

    # ---- Transparent proxy to the real Controller ----

    def __getattr__(self, name: str) -> object:
        """
        Any attribute or method not defined on the adapter is forwarded
        to the underlying Controller. This means:
            adapter.start_sharing(devices)  →  controller.start_sharing(devices)
            adapter.devices                 →  controller.devices
            adapter.session.is_active       →  controller.session.is_active
        """
        return getattr(self._ctrl, name)
