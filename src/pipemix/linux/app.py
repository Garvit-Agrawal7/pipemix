from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import webview

from pipemix.linux.controller import Controller
from pipemix.linux.pactl_backend import PactlBackend
from pipemix.config import ConfigManager
from pipemix.api import Api
from pipemix.bridge import Bridge

log = logging.getLogger("pipemix.app")

DEV_SERVER = "http://localhost:5173"
INSTALLED_WEB = Path("/usr/share/pipemix/web")


def _entry() -> str:
    """The built frontend: local checkout first, then the installed copy.

    From `src/pipemix/linux/app.py` the repo root is `parents[3]`.
    """
    candidates = [
        Path(__file__).resolve().parents[3] / "frontend" / "dist",
        INSTALLED_WEB,
    ]
    for root in candidates:
        if (root / "index.html").is_file():
            return str(root / "index.html")

    # pywebview would only say "URL not found" for a missing path; say what is wrong.
    raise FileNotFoundError(
        "No built frontend found. Looked in: "
        + ", ".join(str(c) for c in candidates)
        + ". Build it with: cd frontend && npm install && npm run build"
    )


def _tray(window: webview.Window, quit_app):
    """A tray icon with Show/Quit, or None when there is nowhere to show it.

    Needs AyatanaAppIndicator3 and a StatusNotifier host: stock GNOME has none,
    and a window hidden there could never be brought back.
    """
    try:
        import gi
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AppIndicator, Gio, GLib, Gtk

        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        has_host = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
            GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, -1, None,
        ).unpack()[0]
        if not has_host:
            raise RuntimeError("no StatusNotifier host")

        png = Path(__file__).resolve().parents[3] / "data" / "icons" / "pipemix.png"
        indicator = AppIndicator.Indicator.new(
            "pipemix", "pipemix", AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        indicator.set_icon_full(str(png) if png.is_file() else "pipemix", "PipeMix")
        menu = Gtk.Menu()
        for label, action in (
            ("Show", lambda _: (window.show(), window.restore())),
            ("Quit", lambda _: quit_app()),
        ):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", action)
            menu.append(item)
        menu.show_all()
        indicator.set_menu(menu)
        indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        return indicator
    except Exception as e:
        log.info("No tray (%s); closing the window will quit.", e)
        return None


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

    # Closing the window hides it to the tray; without a tray there would be
    # no way back, so then it closes normally.
    quitting = threading.Event()
    tray = None

    def on_closing() -> bool:
        # Runs on the GTK main thread (a should_lock event); hide() only
        # queues an idle callback, so it can't deadlock. Returning False
        # cancels the close.
        if quitting.is_set() or tray is None:
            return True
        window.hide()
        return False

    def quit_app() -> None:
        quitting.set()
        window.destroy()

    def on_before_show() -> None:
        # Also a should_lock event, so this runs on the GTK main thread, where
        # the indicator and its menu callbacks have to live.
        nonlocal tray
        tray = _tray(window, quit_app)

    window.events.closing += on_closing
    window.events.before_show += on_before_show

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
