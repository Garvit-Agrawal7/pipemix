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

from pipemix.linux.controller import Controller
from pipemix.linux.services.backend.pactl_backend import PactlBackend
from pipemix.linux.services.config.config_manager import ConfigManager
from pipemix.linux.ui.api import Api
from pipemix.linux.ui.bridge import Bridge

log = logging.getLogger("pipemix.app")

DEV_SERVER = "http://localhost:5173"
INSTALLED_WEB = Path("/usr/share/pipemix/web")


def _entry() -> str:
    """The built frontend: local checkout first, then the installed copy.

    From `src/pipemix/linux/app.py` the repo root is `parents[3]`. It was
    `parents[2]` until this file moved down a level into `linux/`, which
    quietly started pointing at `src/` instead — invisible in day-to-day work,
    because the dev flow sets `PIPEMIX_DEV=1` and loads the vite server.
    """
    candidates = [
        Path(__file__).resolve().parents[3] / "frontend" / "dist",
        INSTALLED_WEB,
    ]
    for root in candidates:
        if (root / "index.html").is_file():
            return str(root / "index.html")

    # Returning a path that does not exist gets rendered as an unhelpful
    # "URL not found" by pywebview's internal file server. Say what is wrong.
    raise FileNotFoundError(
        "No built frontend found. Looked in: "
        + ", ".join(str(c) for c in candidates)
        + ". Build it with: cd frontend && npm install && npm run build"
    )


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
