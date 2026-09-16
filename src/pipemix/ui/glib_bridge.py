"""
GLibBridge — integrates the GLib main context into the Qt event loop.

Why this exists:
  The Controller (and DeviceMonitor) run on GLib signals and GLib.timeout_add()
  timers. PyQt6 has its own QApplication event loop and does not process GLib
  events automatically.

  This bridge runs a QTimer every `interval_ms` milliseconds and drains all
  pending GLib events. `iteration(False)` is non-blocking — it only processes
  what is already queued, so it never stalls the Qt loop.

  Without this, Bluetooth connect/disconnect events and Controller retries
  would never reach the UI.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QTimer
from gi.repository import GLib

log = logging.getLogger(__name__)


class GLibBridge:
    """
    Polls the GLib default main context inside the Qt event loop.

    Usage:
        bridge = GLibBridge()
        bridge.start()          # call after QApplication is created
        app.exec()
        bridge.stop()           # call after app.exec() returns
    """

    def __init__(self, interval_ms: int = 20) -> None:
        """
        Args:
            interval_ms: How often to drain GLib events. 20ms gives ~50 Hz
                         responsiveness for Bluetooth events without noticeable
                         CPU overhead.
        """
        self._timer = QTimer()
        self._timer.setInterval(interval_ms)
        self._ctx = GLib.MainContext.default()
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        """Start bridging. Call this after QApplication is created."""
        log.debug("GLibBridge started (interval=%dms)", self._timer.interval())
        self._timer.start()

    def stop(self) -> None:
        """Stop bridging. Call this before the process exits."""
        self._timer.stop()
        log.debug("GLibBridge stopped.")

    def _tick(self) -> None:
        """Drain all pending GLib events without blocking."""
        while self._ctx.pending():
            self._ctx.iteration(False)
