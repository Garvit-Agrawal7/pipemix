"""
PipeMix — Qt application entry point.

Replaces the GTK Gtk.Application. Owns:
  - QApplication (Qt event loop)
  - GLibBridge   (drains GLib events inside the Qt loop so the Controller works)
  - Controller   (unchanged backend)
  - ControllerAdapter (bridges GLib signals → Qt signals for the UI)
  - MainWindow   (the PyQt6 UI)
"""

from __future__ import annotations

import logging
import sys

import gi
gi.require_version("Gtk", "4.0")   # kept so gi doesn't warn on first import
gi.require_version("Gio", "2.0")
from gi.repository import GLib     # noqa: E402  (needed for GLibBridge ctx)

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon
from PyQt6.QtCore import QTimer

from pipemix.controller import Controller
from pipemix.services.backend.pactl_backend import PactlBackend
from pipemix.services.config.config_manager import ConfigManager
from pipemix.ui.glib_bridge import GLibBridge
from pipemix.ui.controller_adapter import ControllerAdapter
from pipemix.ui.main_window import MainWindow

log = logging.getLogger("pipemix.app")


class PipeMixApp:
    """
    Owns the full application lifecycle:

        app = PipeMixApp()
        sys.exit(app.run())
    """

    def __init__(self) -> None:
        self._qt_app: QApplication | None = None
        self._bridge: GLibBridge | None = None
        self._controller: Controller | None = None
        self._adapter: ControllerAdapter | None = None
        self._window: MainWindow | None = None

    def run(self) -> int:
        """
        Build and show the UI, then hand control to the Qt event loop.
        Returns the exit code for sys.exit().
        """
        # 1. Qt application — must exist before any Qt objects are created.
        self._qt_app = QApplication.instance() or QApplication(sys.argv)
        self._qt_app.setApplicationName("PipeMix")
        self._qt_app.setOrganizationName("PipeMix")

        # Force Fusion style so the window looks identical on every desktop
        # environment (GNOME, MATE, KDE, i3 …) regardless of the installed
        # GTK/Qt theme.
        self._qt_app.setStyle("Fusion")

        # 2. GLib bridge — must start before controller.start() so that
        #    GLib.timeout_add() timers and D-Bus signals are processed.
        self._bridge = GLibBridge(interval_ms=20)
        self._bridge.start()

        # 3. Backend + Controller (completely unchanged).
        backend = PactlBackend()
        config  = ConfigManager()
        self._controller = Controller(backend, config)

        # 4. Adapter — wraps the GLib signals as Qt signals.
        self._adapter = ControllerAdapter(self._controller)

        # 5. Main window.
        self._window = MainWindow(self._adapter)
        self._window.show()

        log.info("Starting PipeMix GUI...")

        # 6. Start the controller *after* the window is shown so that the
        #    first devices-changed signal already has a listener.
        #    QTimer.singleShot(0, …) fires after the event loop starts —
        #    equivalent to GLib.idle_add() in the old code.
        QTimer.singleShot(0, self._controller.start)

        # 7. Connect clean shutdown.
        self._qt_app.aboutToQuit.connect(self._on_quit)

        # 8. Enter the Qt event loop.
        return self._qt_app.exec()

    def _on_quit(self) -> None:
        """Tear down audio routing before the process exits."""
        log.info("Shutting down PipeMix GUI...")
        if self._bridge:
            self._bridge.stop()
        if self._controller:
            self._controller.stop()
