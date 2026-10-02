"""Core Audio output devices → AudioDevice.

A device's UID is stable across reconnects (a Bluetooth headset comes back
with the same one), so, as on Windows, `AudioDevice.id` and `AudioDevice.sink`
are the same string and there is no MAC/sink split to keep in step.
"""

from __future__ import annotations

import logging

from pipemix.models import AudioDevice, DeviceKind
from pipemix.macos import coreaudio as ca

log = logging.getLogger(__name__)

HUB_UID_PREFIX = "com.pipemix.hub"

_BY_TRANSPORT = {
    ca.TRANSPORT_BUILTIN:     DeviceKind.BUILTIN,
    ca.TRANSPORT_BLUETOOTH:   DeviceKind.BLUETOOTH,
    ca.TRANSPORT_BLUETOOTHLE: DeviceKind.BLUETOOTH,
    ca.TRANSPORT_USB:         DeviceKind.USB,
    ca.TRANSPORT_HDMI:        DeviceKind.HDMI,
    ca.TRANSPORT_DISPLAYPORT: DeviceKind.HDMI,
}

# Which subdevice should clock the aggregate: real wired hardware first, since
# it has the steadiest clock, and Bluetooth last, since it is the one most
# likely to vanish and take the session's clock with it.
_CLOCK_RANK = {
    ca.TRANSPORT_BUILTIN:     0,
    ca.TRANSPORT_USB:         1,
    ca.TRANSPORT_THUNDERBOLT: 1,
    ca.TRANSPORT_HDMI:        2,
    ca.TRANSPORT_DISPLAYPORT: 2,
    ca.TRANSPORT_VIRTUAL:     3,
    ca.TRANSPORT_AIRPLAY:     4,
    ca.TRANSPORT_BLUETOOTH:   5,
    ca.TRANSPORT_BLUETOOTHLE: 5,
}


def device_kind(transport: int) -> DeviceKind:
    return _BY_TRANSPORT.get(transport, DeviceKind.UNKNOWN)


def clock_rank(device_uid: str) -> int:
    dev = ca.device_for_uid(device_uid)
    return _CLOCK_RANK.get(ca.transport(dev), 3) if dev else 9


def is_hub(device_uid: str) -> bool:
    return device_uid.startswith(HUB_UID_PREFIX)


def list_outputs() -> list[AudioDevice]:
    """Every output a user could pick, in the order Core Audio lists them.

    Aggregates are left out: our own hub, and any Multi-Output Device the user
    made by hand in Audio MIDI Setup, which would double up whatever it wraps.
    Virtual devices (BlackHole, Loopback) stay — feeding a recorder or a
    stream alongside the speakers is a fair thing to want.
    """
    out = []
    for dev in ca.device_ids():
        try:
            if not ca.has_output(dev) or ca.is_hidden(dev):
                continue
            transport = ca.transport(dev)
            if transport == ca.TRANSPORT_AGGREGATE:
                continue
            device_uid = ca.uid(dev)
            if not device_uid or is_hub(device_uid):
                continue
            out.append(AudioDevice(
                id=device_uid,
                name=ca.name(dev) or device_uid,
                sink=device_uid,
                kind=device_kind(transport),
                connected=True,
            ))
        except ca.CoreAudioError as e:
            # A device can vanish between listing and asking about it.
            log.debug("Skipping device %d: %s", dev, e)
    return out


def output_uids() -> set[str]:
    return {d.id for d in list_outputs()}


def hub_devices() -> list[tuple[int, str]]:
    """[(AudioObjectID, uid)] for every PipeMix hub present, ours or a crashed run's."""
    found = []
    for dev in ca.device_ids():
        try:
            if ca.transport(dev) != ca.TRANSPORT_AGGREGATE:
                continue
            device_uid = ca.uid(dev)
            if is_hub(device_uid):
                found.append((dev, device_uid))
        except ca.CoreAudioError:
            continue
    return found
