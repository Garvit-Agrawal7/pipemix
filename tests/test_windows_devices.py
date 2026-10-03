"""Windows endpoint mapping. Pure logic — no COM, so this runs on Linux CI too."""

from __future__ import annotations

import pytest

from pipemix.models import DeviceKind
from pipemix.windows.wasapi.devices import FORM_FACTOR_DIGITAL_DISPLAY, device_kind, is_virtual


@pytest.mark.parametrize("enumerator, form_factor, kind", [
    # A Bluetooth headset reports form factor Headphones/Headset, but the bus is what the UI groups by.
    ("BTHENUM", 4, DeviceKind.BLUETOOTH),
    ("BTHHFENUM", 3, DeviceKind.BLUETOOTH),
    ("USB", 1, DeviceKind.USB),
    # HDMI and built-in share a bus.
    ("HDAUDIO", FORM_FACTOR_DIGITAL_DISPLAY, DeviceKind.HDMI),
    ("HDAUDIO", 1, DeviceKind.BUILTIN),
    # Unknown is not a crash; enumerator case does not matter.
    (None, None, DeviceKind.UNKNOWN),
    ("SWD", 0, DeviceKind.UNKNOWN),
    ("bthenum", None, DeviceKind.BLUETOOTH),
])
def test_device_kind(enumerator, form_factor, kind):
    assert device_kind(enumerator, form_factor) is kind


# Virtual endpoints are inaudible, and in hub mode "CABLE Input" is our own plumbing:
# offering it would loop the engine into its own source (as PactlBackend's node.virtual).
# An unclassified enumerator is not assumed virtual: hiding an output we failed to
# classify would cost the user a device they can actually hear.
@pytest.mark.parametrize("enumerator, virtual", [
    ("ROOT", True), ("root", True),
    ("HDAUDIO", False), ("USB", False), ("BTHENUM", False), ("BTHHFENUM", False), ("PCI", False),
    (None, False), ("", False),
])
def test_is_virtual(enumerator, virtual):
    assert is_virtual(enumerator) is virtual
