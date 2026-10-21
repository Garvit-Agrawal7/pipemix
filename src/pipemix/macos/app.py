from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import webview

from pipemix.macos.backend import CoreAudioBackend
from pipemix.macos.controller import Controller
from pipemix.macos.media_keys import MediaKeys
from pipemix.config import ConfigManager
from pipemix.api import Api
from pipemix.bridge import Bridge

log = logging.getLogger("pipemix.app")

DEV_SERVER = "http://localhost:5173"


def _roots(relative: str) -> list[Path]:
    """Every place a shipped file could live, most-local first.

    A checkout's root is `parents[3]`. In the frozen .app, data files sit under
    `sys._MEIPASS` (Contents/Frameworks) or its sibling Contents/Resources.
    """
    here = Path(__file__).resolve()
    roots = [here.parents[3] / relative, here.parents[2] / relative]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.insert(0, Path(meipass) / relative)
        roots.append(Path(meipass).parent / "Resources" / relative)
    return roots


def _entry() -> str:
    for root in _roots("frontend/dist"):
        if (root / "index.html").is_file():
            return str(root / "index.html")

    # pywebview would only say "URL not found" for a missing path; say what is wrong.
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

    # Not a shared Bridge signal: only macOS moves master from outside the page.
    controller.connect("master-changed", lambda _c, level: bridge._push("master", level))
    keys = MediaKeys(controller.volume_key)

    def started() -> None:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(keys.install)  # NSEvent monitors belong on the main thread
        controller.start()

    def unwind() -> None:
        # The hub is a real device and would outlive us as the default output.
        # Safe to run twice.
        bridge.close()
        controller.stop()

    # ⌘Q exits through NSApp terminate: and never returns from webview.start(), but
    # pywebview fires `closing` synchronously first, for ⌘Q and the close button alike.
    window.events.closing += unwind

    try:
        webview.start(started, debug=dev)
    finally:
        unwind()

    log.info("PipeMix GUI closed.")
    return 0
