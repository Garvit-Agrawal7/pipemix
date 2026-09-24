"""Windows endpoint mapping. Pure logic — no COM, so this runs on Linux CI too."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipemix.models import DeviceKind
from pipemix.windows.wasapi.devices import FORM_FACTOR_DIGITAL_DISPLAY, device_kind, is_virtual


def test_bus_wins_over_form_factor():
    # A Bluetooth headset reports form factor Headphones/Headset, but the bus
    # is what the UI groups by.
    assert device_kind("BTHENUM", 4) is DeviceKind.BLUETOOTH
    assert device_kind("BTHHFENUM", 3) is DeviceKind.BLUETOOTH
    assert device_kind("USB", 1) is DeviceKind.USB


def test_hdmi_and_builtin_share_a_bus():
    assert device_kind("HDAUDIO", FORM_FACTOR_DIGITAL_DISPLAY) is DeviceKind.HDMI
    assert device_kind("HDAUDIO", 1) is DeviceKind.BUILTIN


def test_unknown_is_not_a_crash():
    assert device_kind(None, None) is DeviceKind.UNKNOWN
    assert device_kind("SWD", 0) is DeviceKind.UNKNOWN


def test_enumerator_case_does_not_matter():
    assert device_kind("bthenum", None) is DeviceKind.BLUETOOTH


# A virtual endpoint is not somewhere a person can hear anything, and in hub
# mode VB-CABLE's "CABLE Input" is PipeMix's own plumbing — offering it as an
# output would loop the engine's output back into the source it captures from.
# PactlBackend drops `node.virtual` sinks for the same reason.

def test_root_enumerated_devices_are_virtual():
    assert is_virtual("ROOT") is True
    assert is_virtual("root") is True


def test_real_hardware_is_never_virtual():
    for enumerator in ("HDAUDIO", "USB", "BTHENUM", "BTHHFENUM", "PCI"):
        assert is_virtual(enumerator) is False, enumerator


def test_unknown_enumerator_is_not_assumed_virtual():
    # Hiding an output we simply failed to classify would be worse than
    # showing a virtual one: the user loses a device they can actually hear.
    assert is_virtual(None) is False
    assert is_virtual("") is False
