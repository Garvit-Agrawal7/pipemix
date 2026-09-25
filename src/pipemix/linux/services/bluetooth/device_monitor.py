"""
PipeMix — BlueZ watcher.

Reports Bluetooth connect/disconnect to the Controller; never touches PipeWire.
Uses Gio's D-Bus client (part of GLib, which GTK already pulls in), so there is
no extra dependency and signals arrive on the main loop the app already runs.
"""

from __future__ import annotations

import logging
from typing import Callable

from gi.repository import Gio, GLib

from pipemix.models import path_to_mac

log = logging.getLogger(__name__)

BLUEZ   = "org.bluez"
DEVICE  = "org.bluez.Device1"
OBJ_MGR = "org.freedesktop.DBus.ObjectManager"
PROPS   = "org.freedesktop.DBus.Properties"

# A device offering any of these is an audio output.
AUDIO_UUIDS = frozenset({
    "0000110b-0000-1000-8000-00805f9b34fb",  # A2DP Sink
    "0000110a-0000-1000-8000-00805f9b34fb",  # A2DP Source
    "0000111e-0000-1000-8000-00805f9b34fb",  # Handsfree
    "00001108-0000-1000-8000-00805f9b34fb",  # Headset
})


class DeviceMonitor:
    """
    Calls on_connected / on_disconnected / on_property with a MAC address
    ("61:C5:02:3A:59:49") as BlueZ reports changes. Set the callbacks, call
    start(), and keep a GLib main loop running.
    """

    def __init__(self) -> None:
        self.on_connect:    Callable[[str], None] | None = None
        self.on_disconnect: Callable[[str], None] | None = None
        self.on_property:   Callable[[str, str, object], None] | None = None

        self._bus: Gio.DBusConnection | None = None
        self._subs: list[int] = []

    def start(self) -> None:
        """Raises GLib.Error if the system bus is unavailable."""
        self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        for iface, signal, handler in (
            (PROPS,   "PropertiesChanged", self._on_props),
            (OBJ_MGR, "InterfacesRemoved", self._on_removed),
        ):
            self._subs.append(self._bus.signal_subscribe(
                BLUEZ, iface, signal, None, None, Gio.DBusSignalFlags.NONE, handler,
            ))
        log.info("DeviceMonitor started — listening for BlueZ events.")

    def stop(self) -> None:
        if not self._bus:
            return
        for sub in self._subs:
            self._bus.signal_unsubscribe(sub)
        self._subs.clear()
        log.info("DeviceMonitor stopped.")

    def connected(self) -> list[dict]:
        """Connected Bluetooth audio devices, as {"mac", "name"}."""
        if not self._bus:
            log.warning("connected() called before start()")
            return []

        try:
            reply = self._bus.call_sync(
                BLUEZ, "/", OBJ_MGR, "GetManagedObjects", None,
                GLib.VariantType("(a{oa{sa{sv}}})"), Gio.DBusCallFlags.NONE, 5000, None,
            )
        except GLib.Error as e:
            log.error("GetManagedObjects() failed — is BlueZ running? %s", e)
            return []

        devices = []
        for path, ifaces in reply.unpack()[0].items():
            props = ifaces.get(DEVICE)
            mac = path_to_mac(path)
            if not props or not props.get("Connected") or not mac:
                continue
            if not {str(u).lower() for u in props.get("UUIDs", [])} & AUDIO_UUIDS:
                continue
            devices.append({"mac": mac, "name": str(props.get("Name", f"Bluetooth {mac}"))})

        log.info("Found %d connected audio device(s).", len(devices))
        return devices

    def _on_props(self, _conn, _sender, path, _iface, _signal, params) -> None:
        iface, changed, _ = params.unpack()
        mac = path_to_mac(path)
        if iface != DEVICE or not mac:
            return

        if "Connected" in changed:
            if changed["Connected"]:
                log.info("[BT] Connected:    %s", mac)
                if self.on_connect:
                    self.on_connect(mac)
            else:
                log.info("[BT] Disconnected: %s", mac)
                if self.on_disconnect:
                    self.on_disconnect(mac)

        for key, value in changed.items():
            if key != "Connected" and self.on_property:
                self.on_property(mac, key, value)

    def _on_removed(self, _conn, _sender, _path, _iface, _signal, params) -> None:
        # The device object vanished (unpaired, or dropped by BlueZ). Treat as
        # a disconnect so an active session tears down instead of hanging.
        path, ifaces = params.unpack()
        mac = path_to_mac(path)
        if DEVICE in ifaces and mac:
            log.debug("[BT] Device object removed: %s", mac)
            if self.on_disconnect:
                self.on_disconnect(mac)
