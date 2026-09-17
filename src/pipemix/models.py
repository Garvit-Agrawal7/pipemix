"""
PipeMix — shared data types.

Pure data: no business logic, no I/O. The Controller owns every instance;
the UI only reads them.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import Enum


class DeviceKind(Enum):
    BLUETOOTH = "bluetooth"
    USB       = "usb"
    HDMI      = "hdmi"
    BUILTIN   = "builtin"
    UNKNOWN   = "unknown"


class SessionState(Enum):
    """Only the Controller transitions between these."""
    IDLE      = "idle"
    STARTING  = "starting"
    ACTIVE    = "active"
    REPAIRING = "repairing"
    STOPPING  = "stopping"
    ERROR     = "error"


# A Bluetooth device has three spellings of one identity: the BlueZ object path,
# the PipeWire sink name, and the MAC we store in config. These keep them in sync.

_MAC       = r"[0-9A-Fa-f]{2}(?:_[0-9A-Fa-f]{2}){5}"
_SINK_RE   = re.compile(rf"^bluez_output\.({_MAC})\.")
_BLUEZ_RE  = re.compile(rf"^/org/bluez/hci\d+/dev_({_MAC})$")


def sink_to_mac(sink: str) -> str | None:
    """'bluez_output.61_C5_02_3A_59_49.1' → '61:C5:02:3A:59:49'."""
    m = _SINK_RE.match(sink)
    return m.group(1).replace("_", ":").upper() if m else None


def path_to_mac(path: str) -> str | None:
    """'/org/bluez/hci0/dev_61_C5_02_3A_59_49' → '61:C5:02:3A:59:49'."""
    m = _BLUEZ_RE.match(path)
    return m.group(1).replace("_", ":").upper() if m else None


@dataclass
class AudioDevice:
    """
    A physical audio output.

    id is stable across reconnects (MAC for Bluetooth, hardware path otherwise)
    and is what config stores. sink is resolved at runtime and is None while the
    device is known but disconnected.
    """
    id:        str
    name:      str
    sink:      str | None
    kind:      DeviceKind
    connected: bool       = False
    battery:   int | None = None
    volume:    int        = 50

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AudioDevice):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)


@dataclass
class VirtualSink:
    """The hub sink we created. module unloads it; legs are its per-output loopbacks."""
    module: int
    name:   str
    legs:   dict[str, int] = field(default_factory=dict)

    @staticmethod
    def make_name() -> str:
        # The pipemix_ prefix is how crash recovery tells our orphans from a
        # user's real sinks; the uuid keeps two instances from colliding.
        return f"pipemix_{uuid.uuid4().hex[:8]}"


@dataclass
class SharingSession:
    """The current session. Mutated only by the Controller."""
    devices: list[AudioDevice]  = field(default_factory=list)
    sink:    VirtualSink | None = None
    state:   SessionState       = SessionState.IDLE

    @property
    def is_active(self) -> bool:
        return self.state == SessionState.ACTIVE
