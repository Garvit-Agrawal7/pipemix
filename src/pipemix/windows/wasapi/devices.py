from __future__ import annotations

import logging

from pipemix.models import AudioDevice, DeviceKind

log = logging.getLogger(__name__)

# pycaw keys a device's property bag by str(PROPERTYKEY) — "{GUID} pid".
PKEY_FriendlyName  = "{A45C254E-DF1C-4EFD-8020-67D146A850E0} 14"
PKEY_EnumeratorName = "{A45C254E-DF1C-4EFD-8020-67D146A850E0} 24"
PKEY_FormFactor    = "{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E} 0"

FORM_FACTOR_DIGITAL_DISPLAY = 9  # EndpointFormFactor::DigitalAudioDisplayDevice

_BY_ENUMERATOR = {
    "BTHENUM":   DeviceKind.BLUETOOTH,
    "BTHHFENUM": DeviceKind.BLUETOOTH,
    "BTHLEENUM": DeviceKind.BLUETOOTH,
    "USB":       DeviceKind.USB,
    "USBAUDIO":  DeviceKind.USB,
}


def device_kind(enumerator: str | None, form_factor: int | None) -> DeviceKind:
    """Windows tells us the bus, not the intent; this is the mapping.

    HDMI and DisplayPort endpoints sit on the same HDAUDIO bus as the internal
    speakers, so the form factor is what separates them.
    """
    enum = (enumerator or "").upper()
    if enum in _BY_ENUMERATOR:
        return _BY_ENUMERATOR[enum]
    if form_factor == FORM_FACTOR_DIGITAL_DISPLAY:
        return DeviceKind.HDMI
    if enum == "HDAUDIO":
        return DeviceKind.BUILTIN
    return DeviceKind.UNKNOWN


def _to_audio_device(dev) -> AudioDevice:
    props = dev.properties
    name = props.get(PKEY_FriendlyName) or dev.id
    return AudioDevice(
        id=dev.id,
        name=name,
        sink=dev.id,  # same namespace on Windows
        kind=device_kind(props.get(PKEY_EnumeratorName), props.get(PKEY_FormFactor)),
        connected=True,  # only ACTIVE endpoints are enumerated
    )


def is_virtual(enumerator: str | None) -> bool:
    """A software device with no hardware behind it.

    Windows has no virtual flag, but bus-less drivers (VB-CABLE, Voicemeeter)
    enumerate under ROOT; real cards come up HDAUDIO, USB, BTHENUM or PCI.
    """
    return (enumerator or "").upper() == "ROOT"


def list_outputs(include_virtual: bool = False) -> list[AudioDevice]:
    """Every active render endpoint, exactly as Windows reports it.

    A Bluetooth headset appears twice (Stereo and Hands-Free); both are kept, as
    they are two real outputs.

    Virtual endpoints are left out: nobody hears them, and feeding CABLE Input in
    hub mode would loop the engine into its own source (`PactlBackend` drops
    `node.virtual` for the same reason). `include_virtual=True` is for the
    VB-CABLE probe in `WasapiBackend._find_cable`.
    """
    # pycaw is imported here, not at module scope, so `device_kind` stays
    # importable (and testable) on a machine without comtypes.
    from pycaw.constants import DEVICE_STATE, EDataFlow
    from pycaw.utils import AudioUtilities

    devices = []
    for d in AudioUtilities.GetAllDevices(
        EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value
    ):
        if d is None:
            continue
        if not include_virtual and is_virtual(d.properties.get(PKEY_EnumeratorName)):
            continue
        devices.append(_to_audio_device(d))

    log.debug("Enumerated %d active render endpoint(s).", len(devices))
    return devices


def default_output_id() -> str | None:
    """The current eMultimedia render default — the leader-mode starting point."""
    from pycaw.constants import EDataFlow, ERole
    from pycaw.utils import AudioUtilities

    try:
        enumerator = AudioUtilities.GetDeviceEnumerator()
        dev = enumerator.GetDefaultAudioEndpoint(
            EDataFlow.eRender.value, ERole.eMultimedia.value
        )
        return dev.GetId()
    except Exception as e:  # no active render endpoint at all
        log.warning("No default render endpoint: %s", e)
        return None


if __name__ == "__main__":
    import comtypes

    comtypes.CoInitialize()
    default = default_output_id()
    for d in list_outputs():
        print(f"{'*' if d.id == default else ' '} {d.kind.value:<9} {d.name}\n  {d.id}")
