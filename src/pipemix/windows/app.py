from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import pystray
import webview
from PIL import Image

from pipemix.windows.backend import WasapiBackend
from pipemix.windows.controller import Controller
from pipemix.config import ConfigManager
from pipemix.api import Api
from pipemix.bridge import Bridge

log = logging.getLogger("pipemix.app")

DEV_SERVER = "http://localhost:5173"


def _roots(relative: str) -> list[Path]:
    """Every place a shipped file could live, most-local first.

    A checkout's root is `parents[3]`; PyInstaller flattens `src/`, making
    `parents[2]` the frozen bundle's root.
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

    # pywebview would only say "URL not found" for a missing path; say what is wrong.
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

    # Closing the window hides it to the tray; without an icon there would be
    # no way back, so then it closes normally.
    icon = _icon()
    tray = None
    if icon:
        quitting = threading.Event()

        def on_closing() -> bool:
            # Runs on the UI thread (a should_lock event), where hide()'s
            # Invoke executes inline; returning False cancels the close.
            if quitting.is_set():
                return True
            window.hide()
            return False

        def quit_app() -> None:
            quitting.set()
            tray.stop()
            window.destroy()

        def on_form_closing(sender, args) -> None:
            # pywebview's closing event has no close reason, so un-cancel the
            # shutdown/sign-out close here (this runs after pywebview's handler).
            from System.Windows.Forms import CloseReason
            if args.CloseReason == CloseReason.WindowsShutDown:
                quitting.set()
                args.Cancel = False

        window.events.closing += on_closing

        def on_before_show() -> None:
            window.native.FormClosing += on_form_closing

        window.events.before_show += on_before_show
        tray = pystray.Icon(
            "PipeMix", Image.open(icon), "PipeMix",
            pystray.Menu(
                pystray.MenuItem("Show", lambda: (window.show(), window.restore()), default=True),
                pystray.MenuItem("Quit", quit_app),
            ),
        )
        tray.run_detached()

    # start() blocks on WASAPI enumeration and the notification client, and
    # its first devices-changed emit needs the bridge already attached.
    try:
        webview.start(controller.start, debug=dev, icon=icon)
    finally:
        if tray:
            tray.stop()
        # Unwind routing before the process goes away.
        bridge.close()
        controller.stop()

    log.info("PipeMix GUI closed.")
    return 0
