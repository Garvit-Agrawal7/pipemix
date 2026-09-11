from __future__ import annotations

import logging

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gio', '2.0')
from gi.repository import GLib, Gtk, Gio

from pipemix.controller import Controller
from pipemix.services.backend.pactl_backend import PactlBackend
from pipemix.services.config.config_manager import ConfigManager
from pipemix.ui.main_window import MainWindow

log = logging.getLogger("pipemix.app")


class PipeMixApp(Gtk.Application):

    def __init__(self) -> None:
        super().__init__(
            application_id="org.pipemix.PipeMix",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.controller: Controller | None = None
        self.window: MainWindow | None = None

    def do_activate(self) -> None:
        log.info("Starting PipeMix GUI...")
        self.controller = Controller(PactlBackend(), ConfigManager())
        self.window = MainWindow(self, self.controller)
        self.window.present()
        # After the window is up: start() blocks on pactl/BlueZ, and its
        # devices-changed emit needs a listener already connected.
        GLib.idle_add(self.controller.start)

    def do_shutdown(self) -> None:
        """Tear down audio routing before the process goes away."""
        log.info("Shutting down PipeMix GUI...")
        if self.controller:
            self.controller.stop()
        Gtk.Application.do_shutdown(self)
