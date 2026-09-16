"""
PipeMix — GUI startup.

pywebview renders in WebKitGTK and runs the GLib main loop, so the Controller's
GObject signals, GLib timers and Gio D-Bus BlueZ monitoring keep working. It
pins Gtk 3.0 itself, so nothing here may require a Gtk version.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import webview

from pipemix.controller import Controller
from pipemix.services.backend.pactl_backend import PactlBackend
from pipemix.services.config.config_manager import ConfigManager
from pipemix.ui.api import Api
from pipemix.ui.bridge import Bridge

log = logging.getLogger("pipemix.app")

DEV_SERVER = "http://localhost:5173"
INSTALLED_WEB = Path("/usr/share/pipemix/web")


def _entry() -> str:
    """The built frontend: local checkout first, then the installed copy."""
    local = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    root = local if local.exists() else INSTALLED_WEB
    return str(root / "index.html")


def run_gui() -> int:
    log.info("Starting PipeMix GUI...")

    controller = Controller(PactlBackend(), ConfigManager())
    api = Api(controller)
    bridge = Bridge(controller, api)

    dev = os.environ.get("PIPEMIX_DEV") == "1"
    window = webview.create_window(
        "PipeMix",
        url=DEV_SERVER if dev else _entry(),
        js_api=api,
        width=800,
        height=600,
        min_size=(560, 420),
        background_color="#0F1214",
    )
    bridge.attach(window)

    # start() blocks on pactl and BlueZ, and its first devices-changed emit
    # needs the bridge already attached.
    try:
        webview.start(controller.start, gui="gtk", debug=dev)
    finally:
        # Unwind routing before the process goes away.
        bridge.close()
        controller.stop()

    log.info("PipeMix GUI closed.")
    return 0
