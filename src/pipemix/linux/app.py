from __future__ import annotations

import logging
import os
from pathlib import Path

import webview
from gi.repository import Gio, GLib

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
        # Runs before pywebview loads its backend, so pin GTK 3 here too.
        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AppIndicator, Gtk

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
            ("Show", lambda _: window.restore()),  # present() also unhides
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


def _claim() -> Gio.Application | None:
    """Own PipeMix's D-Bus name, or None after asking whoever owns it to show
    its window: a second instance's clean_orphans would unload its routing.

    A D-Bus name, not a signal: WebKit's GC takes SIGUSR1 and crashes on it.
    """
    gapp = Gio.Application(application_id="io.github.Garvit_Agrawal7.PipeMix")
    gapp.register(None)
    if not gapp.get_is_remote():
        return gapp
    gapp.activate()  # becomes the owner's "activate" signal
    gapp.get_dbus_connection().flush_sync(None)
    return None


def run_gui() -> int:
    gapp = _claim()
    if gapp is None:
        log.info("PipeMix is already running; showing that window.")
        return 0
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
    tray = _tray(window, api.shutdown)

    def on_closing() -> bool:
        # Runs on a worker thread while the GTK main thread waits for it, so
        # only queue work here: hide() does, evaluate_js would deadlock.
        # destroy() fires this too, hence the quitting check. False cancels.
        if api._quitting or tray is None:
            return True
        window.hide()
        return False

    def on_activate(_) -> None:
        # Main thread, where restore() would stall until the window is shown.
        if window.events.shown.is_set():
            window.restore()

    window.events.closing += on_closing
    gapp.connect("activate", on_activate)

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
