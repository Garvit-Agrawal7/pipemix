"""
PipeMix — Gtk.Application Subclass

Coordinates application launching, window initialization,
and system-level hooks (like clean shutdown).
"""

from __future__ import annotations

import logging
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gio', '2.0')
from gi.repository import Gtk, Gio

from pipemix.controller import Controller
from pipemix.services.backend.pactl_backend import PactlBackend
from pipemix.services.config.config_manager import ConfigManager
from pipemix.ui.main_window import MainWindow

log = logging.getLogger("pipemix.app")

class PipeMixApplication(Gtk.Application):
    """
    Standard Gtk.Application entry class for PipeMix.
    """

    def __init__(self) -> None:
        super().__init__(
            application_id="org.pipemix.PipeMix",
            flags=Gio.ApplicationFlags.FLAGS_NONE
        )
        self.controller: Controller | None = None
        self.window: MainWindow | None = None

    def do_activate(self) -> None:
        """Fires when Gtk.Application starts."""
        log.info("Starting PipeMix GUI...")

        # 1. Initialize core services
        config = ConfigManager()
        backend = PactlBackend()
        self.controller = Controller(backend, config)

        # 2. Start controller (runs crash recovery and discovery)
        self.controller.start()

        # 3. Create and show main window
        self.window = MainWindow(self, self.controller)
        self.window.present()

    def do_shutdown(self) -> None:
        """Fires on app exit. Ensures clean shutdown of audio loops."""
        log.info("Shutting down PipeMix GUI...")
        if self.controller:
            self.controller.stop()
        # Call base class shutdown handler
        Gtk.Application.do_shutdown(self)
