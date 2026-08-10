"""
PipeMix — Core Data Models

All shared data structures used across services, controller, and UI.
The UI, Controller, and backends all import from here — nothing else.

Design rules:
  - Pure data: no business logic, no I/O, no side effects.
  - The UI never constructs these directly; the Controller owns them.
  - device_id is always stable across reconnects (MAC for BT, hw path for others).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DeviceKind(Enum):
    """Physical category of an audio output device."""
    BLUETOOTH = "bluetooth"
    USB       = "usb"
    HDMI      = "hdmi"
    BUILTIN   = "builtin"
    UNKNOWN   = "unknown"


class SessionState(Enum):
    """
    State of the current SharingSession.
    The Controller is the only entity that transitions between states.
    """
    IDLE      = "idle"       # No virtual sink exists
    STARTING  = "starting"   # Creating virtual sink + routing streams
    ACTIVE    = "active"     # Virtual sink exists, streams routed
    REPAIRING = "repairing"  # Reset Audio in progress
    STOPPING  = "stopping"   # Tearing down virtual sink
    ERROR     = "error"      # Unrecoverable error; user action required


# ---------------------------------------------------------------------------
# Device Capabilities
# ---------------------------------------------------------------------------

@dataclass
class DeviceCapabilities:
    """
    Declares what a device supports.
    The UI checks these before showing any device-specific widget —
    it never checks device.kind directly.
    """
    supports_volume:            bool = True
    supports_battery:           bool = False
    supports_latency:           bool = False
    supports_avrcp:             bool = False
    supports_disconnect_events: bool = False

    @classmethod
    def for_bluetooth(cls) -> DeviceCapabilities:
        return cls(
            supports_volume=True,
            supports_battery=True,
            supports_latency=True,
            supports_avrcp=True,
            supports_disconnect_events=True,
        )

    @classmethod
    def for_hdmi(cls) -> DeviceCapabilities:
        return cls(
            supports_volume=True,
            supports_battery=False,
            supports_latency=False,
            supports_avrcp=False,
            supports_disconnect_events=False,
        )

    @classmethod
    def for_usb(cls) -> DeviceCapabilities:
        return cls(
            supports_volume=True,
            supports_battery=False,
            supports_latency=True,
            supports_avrcp=False,
            supports_disconnect_events=True,
        )

    @classmethod
    def for_builtin(cls) -> DeviceCapabilities:
        return cls(
            supports_volume=True,
            supports_battery=False,
            supports_latency=False,
            supports_avrcp=False,
            supports_disconnect_events=False,
        )


# ---------------------------------------------------------------------------
# AudioDevice
# ---------------------------------------------------------------------------

@dataclass
class AudioDevice:
    """
    Represents a physical audio output device.

    Identity: device_id is stable across reconnects.
      - Bluetooth: MAC address ("61:C5:02:3A:59:49")
      - Others: stable hardware path from PipeWire (e.g. alsa_output.pci-...)

    sink_name is resolved at runtime from PipeWire's current state.
    It can be None if the device is known but currently disconnected.
    """
    device_id:         str                   # Stable identifier, stored in config
    display_name:      str                   # User-visible name (editable)
    sink_name:         str | None            # Current PipeWire sink name (runtime)
    kind:              DeviceKind
    capabilities:      DeviceCapabilities
    is_connected:      bool        = False
    battery_level:     int | None  = None    # 0–100, or None if unsupported
    latency_offset_ms: int         = 0       # User-set latency compensation (ms)
    volume:            int         = 50      # Runtime volume (0-100), default 50%

    @property
    def stable_id(self) -> str:
        """The identifier stored in config presets. Always stable."""
        return self.device_id

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AudioDevice):
            return NotImplemented
        return self.device_id == other.device_id

    def __hash__(self) -> int:
        return hash(self.device_id)

    def __repr__(self) -> str:
        return (
            f"AudioDevice(id={self.device_id!r}, name={self.display_name!r}, "
            f"kind={self.kind.value}, connected={self.is_connected})"
        )


# ---------------------------------------------------------------------------
# AudioStream
# ---------------------------------------------------------------------------

@dataclass
class AudioStream:
    """An active audio stream (PipeWire sink-input) currently playing."""
    stream_id: int    # PipeWire sink-input index
    name:      str    # Application name ("Firefox", "VLC", etc.)
    sink_name: str    # Current sink this stream is routed to

    def __repr__(self) -> str:
        return f"AudioStream(id={self.stream_id}, name={self.name!r} → {self.sink_name!r})"


# ---------------------------------------------------------------------------
# VirtualSink
# ---------------------------------------------------------------------------

@dataclass
class VirtualSink:
    """
    A PipeWire combined sink created by PipeMix.

    Naming uses a UUID fragment ("pipemix_8f3a2c1d") so that:
      - Multiple instances never collide.
      - Crash recovery can identify orphaned sinks by the "pipemix_" prefix.
      - We never accidentally destroy a user's real sink.
    """
    module_id:  int    # PipeWire module ID — required to unload the module
    sink_name:  str    # e.g. "pipemix_8f3a2c1d"
    created_at: float  = field(default_factory=time.time)

    @staticmethod
    def make_name() -> str:
        """Generate a collision-safe, recoverable sink name."""
        short_id = uuid.uuid4().hex[:8]
        return f"pipemix_{short_id}"

    def __repr__(self) -> str:
        return f"VirtualSink(name={self.sink_name!r}, module_id={self.module_id})"


# ---------------------------------------------------------------------------
# SharingSession
# ---------------------------------------------------------------------------

@dataclass
class SharingSession:
    """
    First-class object representing the current sharing session.

    Owned and mutated exclusively by the Controller.
    The UI receives read-only snapshots via GLib signals — it never
    holds a reference to this object directly.
    """
    selected_devices: list[AudioDevice] = field(default_factory=list)
    virtual_sink:     VirtualSink | None = None
    active_streams:   list[AudioStream]  = field(default_factory=list)
    state:            SessionState       = SessionState.IDLE

    @property
    def is_active(self) -> bool:
        return self.state == SessionState.ACTIVE

    def __repr__(self) -> str:
        devices = [d.display_name for d in self.selected_devices]
        return (
            f"SharingSession(state={self.state.value}, "
            f"devices={devices}, sink={self.virtual_sink})"
        )
