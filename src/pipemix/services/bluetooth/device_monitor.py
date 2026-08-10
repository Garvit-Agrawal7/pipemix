"""
PipeMix — DeviceMonitor

Watches BlueZ via D-Bus for Bluetooth device connect/disconnect events.
Emits callbacks into the Controller — never touches PipeWire directly.

How it works:
  BlueZ exposes device objects at /org/bluez/hciX/dev_XX_XX_XX_XX_XX_XX.
  We subscribe to three D-Bus signals:
    1. PropertiesChanged  — detects Connected: True/False transitions
    2. InterfacesAdded    — device object created (usually on pairing)
    3. InterfacesRemoved  — device object destroyed (unpaired/removed)

  The Controller registers callbacks and runs a GLib main loop.
  DeviceMonitor is fully passive — it only reacts, never polls.
"""

from __future__ import annotations

import logging
import re
from typing import Callable

import dbus
import dbus.mainloop.glib

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BlueZ D-Bus constants
# ---------------------------------------------------------------------------

BLUEZ_SERVICE   = "org.bluez"
DBUS_OM_IFACE   = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"
BLUEZ_DEVICE    = "org.bluez.Device1"

# Bluetooth audio profile UUIDs.
# A device with any of these is considered an audio output.
AUDIO_UUIDS: frozenset[str] = frozenset({
    "0000110b-0000-1000-8000-00805f9b34fb",  # A2DP Sink (headphones, earbuds)
    "0000110a-0000-1000-8000-00805f9b34fb",  # A2DP Source
    "0000111e-0000-1000-8000-00805f9b34fb",  # Handsfree (HFP)
    "00001108-0000-1000-8000-00805f9b34fb",  # Headset (HSP)
})

# Matches /org/bluez/hci0/dev_61_C5_02_3A_59_49
_DEV_PATH_RE = re.compile(r"^/org/bluez/hci\d+/dev_((?:[0-9A-Fa-f]{2}_){5}[0-9A-Fa-f]{2})$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _path_to_mac(path: str) -> str | None:
    """
    Extract Bluetooth MAC from a BlueZ object path.
    '/org/bluez/hci0/dev_61_C5_02_3A_59_49' → '61:C5:02:3A:59:49'
    Returns None if path is not a device path.
    """
    m = _DEV_PATH_RE.match(str(path))
    if m:
        return m.group(1).replace("_", ":").upper()
    return None


def _is_audio_device(uuids: list[str]) -> bool:
    """Return True if the device UUID list includes any known audio profile."""
    return bool({u.lower() for u in uuids} & AUDIO_UUIDS)


# ---------------------------------------------------------------------------
# DeviceMonitor
# ---------------------------------------------------------------------------

class DeviceMonitor:
    """
    Subscribes to BlueZ D-Bus signals and invokes registered callbacks
    when Bluetooth audio devices connect or disconnect.

    All callbacks receive a single argument: the device MAC address (str).
    e.g. "61:C5:02:3A:59:49"

    Lifecycle:
        monitor = DeviceMonitor()
        monitor.on_connected    = lambda mac: ...
        monitor.on_disconnected = lambda mac: ...
        monitor.start()
        # GLib main loop must be running for events to fire
        # (GTK4 provides this; for tests, use GLib.MainLoop().run())
        monitor.stop()

    Thread safety:
        All callbacks fire in the GLib main loop thread.
        Do not call start/stop from a different thread without a lock.
    """

    def __init__(self) -> None:
        # Callbacks — set these before calling start()
        self.on_connected:        Callable[[str], None] | None = None
        self.on_disconnected:     Callable[[str], None] | None = None
        self.on_property_changed: Callable[[str, str, object], None] | None = None

        self._bus:     dbus.SystemBus | None = None
        self._started: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Connect to D-Bus system bus and subscribe to BlueZ signals.
        Must be called before the GLib main loop starts running.
        Raises dbus.DBusException if D-Bus is unavailable.
        """
        # Integrate D-Bus with GLib event loop.
        # Safe to call multiple times — dbus-python handles that.
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

        try:
            self._bus = dbus.SystemBus()
        except dbus.DBusException as e:
            log.error("Cannot connect to D-Bus system bus: %s", e)
            raise

        # PropertiesChanged fires when Connected, Paired, Battery, etc. change.
        # path_keyword="path" makes dbus-python pass the object path to our handler.
        self._bus.add_signal_receiver(
            self._on_properties_changed,
            signal_name="PropertiesChanged",
            dbus_interface=DBUS_PROP_IFACE,
            bus_name=BLUEZ_SERVICE,
            path_keyword="path",
        )

        # InterfacesAdded fires when a new BlueZ object appears (device paired, etc.)
        self._bus.add_signal_receiver(
            self._on_interfaces_added,
            signal_name="InterfacesAdded",
            dbus_interface=DBUS_OM_IFACE,
            bus_name=BLUEZ_SERVICE,
        )

        # InterfacesRemoved fires when a BlueZ object disappears (device removed).
        self._bus.add_signal_receiver(
            self._on_interfaces_removed,
            signal_name="InterfacesRemoved",
            dbus_interface=DBUS_OM_IFACE,
            bus_name=BLUEZ_SERVICE,
        )

        self._started = True
        log.info("DeviceMonitor started — listening for BlueZ D-Bus events.")

    def stop(self) -> None:
        """Unsubscribe from D-Bus signals. Safe to call multiple times."""
        if not (self._bus and self._started):
            return

        try:
            self._bus.remove_signal_receiver(
                self._on_properties_changed,
                signal_name="PropertiesChanged",
                dbus_interface=DBUS_PROP_IFACE,
            )
            self._bus.remove_signal_receiver(
                self._on_interfaces_added,
                signal_name="InterfacesAdded",
                dbus_interface=DBUS_OM_IFACE,
            )
            self._bus.remove_signal_receiver(
                self._on_interfaces_removed,
                signal_name="InterfacesRemoved",
                dbus_interface=DBUS_OM_IFACE,
            )
        except dbus.DBusException as e:
            log.warning("Error while unsubscribing from D-Bus signals: %s", e)

        self._started = False
        log.info("DeviceMonitor stopped.")

    # ------------------------------------------------------------------
    # Synchronous query (no event needed)
    # ------------------------------------------------------------------

    def get_connected_devices(self) -> list[dict]:
        """
        Return all currently connected Bluetooth audio devices.
        Queries BlueZ synchronously via GetManagedObjects.
        Does not require any events to have fired.

        Returns a list of dicts:
            {"mac": str, "name": str, "uuids": list[str]}
        """
        if not self._bus:
            log.warning("get_connected_devices() called before start()")
            return []

        try:
            manager = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, "/"),
                DBUS_OM_IFACE,
            )
            objects = manager.GetManagedObjects()
        except dbus.DBusException as e:
            log.error("GetManagedObjects() failed — is BlueZ running? %s", e)
            return []

        devices: list[dict] = []
        for path, interfaces in objects.items():
            if BLUEZ_DEVICE not in interfaces:
                continue

            props = interfaces[BLUEZ_DEVICE]
            connected = bool(props.get("Connected", False))
            if not connected:
                continue

            uuids = [str(u) for u in props.get("UUIDs", [])]
            if not _is_audio_device(uuids):
                log.debug("Skipping non-audio device at %s", path)
                continue

            mac = _path_to_mac(str(path))
            if not mac:
                continue

            entry = {
                "mac":   mac,
                "name":  str(props.get("Name", f"Bluetooth {mac}")),
                "uuids": uuids,
            }
            devices.append(entry)
            log.debug("Found connected audio device: %s (%s)", entry["name"], mac)

        log.info(
            "get_connected_devices(): found %d connected audio device(s).", len(devices)
        )
        return devices

    # ------------------------------------------------------------------
    # D-Bus signal handlers (private)
    # ------------------------------------------------------------------

    def _on_properties_changed(
        self,
        interface: str,
        changed: dbus.Dictionary,
        invalidated: dbus.Array,
        path: str,
    ) -> None:
        """
        Handles PropertiesChanged signal.
        We only act on org.bluez.Device1 and only on the Connected property.
        Other property changes (battery, codec) are forwarded to on_property_changed.
        """
        if interface != BLUEZ_DEVICE:
            return

        mac = _path_to_mac(str(path))
        if not mac:
            return

        # Handle Connected change
        if "Connected" in changed:
            if bool(changed["Connected"]):
                log.info("[BT] Connected:    %s", mac)
                if self.on_connected:
                    self.on_connected(mac)
            else:
                log.info("[BT] Disconnected: %s", mac)
                if self.on_disconnected:
                    self.on_disconnected(mac)

        # Forward all other property changes (battery level, etc.)
        for key, value in changed.items():
            if key == "Connected":
                continue
            log.debug("[BT] Property changed: %s  %s = %r", mac, key, value)
            if self.on_property_changed:
                self.on_property_changed(mac, str(key), value)

    def _on_interfaces_added(
        self,
        path: str,
        interfaces: dbus.Dictionary,
    ) -> None:
        """
        Handles InterfacesAdded — a new device object appeared.
        Usually means a device was just paired. We log it; the Controller
        will get the real Connected event from PropertiesChanged.
        """
        if BLUEZ_DEVICE not in interfaces:
            return
        mac = _path_to_mac(str(path))
        if mac:
            log.debug("[BT] Device object added (paired?): %s", mac)

    def _on_interfaces_removed(
        self,
        path: str,
        interfaces: dbus.Array,
    ) -> None:
        """
        Handles InterfacesRemoved — a device object disappeared.
        This happens when a device is unpaired or BlueZ removes it.
        Treated as a disconnect event for safety.
        """
        if BLUEZ_DEVICE not in [str(i) for i in interfaces]:
            return
        mac = _path_to_mac(str(path))
        if mac:
            log.debug("[BT] Device object removed (unpaired?): %s", mac)
            if self.on_disconnected:
                self.on_disconnected(mac)
