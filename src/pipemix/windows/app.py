"""
PipeMix — GUI startup (Windows).

pywebview picks EdgeChromium here (no `gui=` kwarg needed, unlike the pinned
`gui="gtk"` on Linux), and runs its own message loop rather than GLib's, so
`webview.start()` is the only thing blocking — the Controller has no GLib
main loop dependency to share with it on this platform.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import webview

from pipemix.windows.backend import WasapiBackend
from pipemix.windows.controller import Controller
from pipemix.linux.services.config.config_manager import ConfigManager
from pipemix.linux.ui.api import Api
from pipemix.linux.ui.bridge import Bridge

log = logging.getLogger("pipemix.app")

DEV_SERVER = "http://localhost:5173"


def _roots(relative: str) -> list[Path]:
    """Every place a shipped file could live, most-local first.

    From `src/pipemix/windows/app.py` the repo root is `parents[3]`; it was
    `parents[2]` until this file moved down a level, which quietly started
    pointing at `src/`. PyInstaller flattens `src/` away, so inside a frozen
    build `parents[2]` *is* the bundle root — hence both, in that order.
    """
    here = Path(__file__).resolve()
    return [
        here.parents[3] / relative,                                      # checkout
        here.parents[2] / relative,                                      # frozen bundle
        Path(os.environ.get("PROGRAMDATA", "")) / "PipeMix" / relative,  # installed
    ]


def _entry() -> str:
    """The built frontend, wherever this copy of PipeMix keeps it."""
    for root in _roots("frontend/dist"):
        if (root / "index.html").is_file():
            return str(root / "index.html")

    # A path that does not exist comes out of pywebview's internal file server
    # as a bare "URL not found" against localhost, which tells nobody anything.
    raise FileNotFoundError(
        "No built frontend found. Looked in: "
        + ", ".join(str(r) for r in _roots("frontend/dist"))
        + ". Build it with: cd frontend && npm install && npm run build"
    )


def _icon() -> str | None:
    """The window and taskbar icon, or None rather than failing to start over it."""
    for root in _roots("data/icons/pipemix.ico"):
        if root.is_file():
            return str(root)
    log.warning("No pipemix.ico found — the window will use the default icon.")
    return None


def run_gui() -> int:
    log.info("Starting PipeMix GUI...")

    controller = Controller(WasapiBackend(), ConfigManager())
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

    # start() blocks on WASAPI enumeration and the notification client, and
    # its first devices-changed emit needs the bridge already attached.
    try:
        webview.start(controller.start, debug=dev, icon=_icon())
    finally:
        # Unwind routing before the process goes away.
        bridge.close()
        controller.stop()

    log.info("PipeMix GUI closed.")
    return 0
