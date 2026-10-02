"""
PipeMix — GUI startup (macOS).

pywebview picks its Cocoa backend (WKWebView) here, which must own the main
thread — `webview.start()` is the only thing blocking, as on Windows. The
Controller needs no run loop of its own: Core Audio notifications are
detached onto the HAL's own thread (see `coreaudio._libs`). The one thing
that does need Cocoa's loop is the volume-key monitor, installed on the main
thread once the app is running.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import webview

from pipemix.macos.backend import CoreAudioBackend
from pipemix.macos.controller import Controller
from pipemix.macos.media_keys import MediaKeys
from pipemix.linux.services.config.config_manager import ConfigManager
from pipemix.linux.ui.api import Api
from pipemix.linux.ui.bridge import Bridge

log = logging.getLogger("pipemix.app")

DEV_SERVER = "http://localhost:5173"


def _roots(relative: str) -> list[Path]:
    """Every place a shipped file could live, most-local first.

    From `src/pipemix/macos/app.py` the repo root is `parents[3]`. Inside the
    frozen .app, PyInstaller puts data files in `Contents/Resources`, which is
    `sys._MEIPASS` in a BUNDLE build; `parents[2]` covers a plain onedir build.
    """
    here = Path(__file__).resolve()
    roots = [here.parents[3] / relative, here.parents[2] / relative]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.insert(0, Path(meipass) / relative)
        roots.append(Path(meipass).parent / "Resources" / relative)
    return roots


def _entry() -> str:
    """The built frontend, wherever this copy of PipeMix keeps it."""
    for root in _roots("frontend/dist"):
        if (root / "index.html").is_file():
            return str(root / "index.html")

    raise FileNotFoundError(
        "No built frontend found. Looked in: "
        + ", ".join(str(r) for r in _roots("frontend/dist"))
        + ". Build it with: cd frontend && npm install && npm run build"
    )


def run_gui() -> int:
    log.info("Starting PipeMix GUI...")

    controller = Controller(CoreAudioBackend(), ConfigManager())
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

    # Not one of the shared Bridge's signals: only the Mac build moves master
    # from outside the page.
    controller.connect("master-changed", lambda _c, level: bridge._push("master", level))
    keys = MediaKeys(controller.volume_key)

    def started() -> None:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(keys.install)
        controller.start()

    def unwind() -> None:
        # Unwind routing before the process goes away: the hub is a real
        # device and would outlive us, still the default output. Safe to run
        # twice — the second time there is nothing left to stop.
        bridge.close()
        controller.stop()

    # ⌘Q goes through NSApp terminate:, which exits the process without
    # returning from webview.start(), so the finally below never runs. pywebview
    # fires `closing` synchronously for both ⌘Q and the close button, before
    # either takes effect — that is the one place cleanup is sure to happen.
    window.events.closing += unwind

    # start() enumerates devices and registers the hotplug listener, and its
    # first devices-changed emit needs the bridge already attached.
    try:
        webview.start(started, debug=dev)
    finally:
        unwind()

    log.info("PipeMix GUI closed.")
    return 0
