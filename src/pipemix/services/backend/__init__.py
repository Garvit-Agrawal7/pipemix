"""
PipeMix — AudioBackend Abstract Base Class

All audio backends must implement this interface.
The UI and Controller never interact with PipeWire primitives directly —
only through this interface.

To add a new backend:
  1. Create a new file in this directory.
  2. Subclass AudioBackend.
  3. Implement all abstract methods.
  4. Instantiate it in main.py and pass to the Controller.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipemix.models import AudioDevice, VirtualSink


# ---------------------------------------------------------------------------
# Backend health
# ---------------------------------------------------------------------------

class BackendHealth(Enum):
    OK               = "ok"
    UNAVAILABLE      = "unavailable"       # pactl / PipeWire not running
    DEGRADED         = "degraded"          # Running but something is wrong
    PERMISSION_ERROR = "permission_error"  # Cannot access PipeWire socket


@dataclass
class BackendStatus:
    """
    Result of a health_check() call.
    message is shown in the UI.
    details is written to the log only.
    """
    health:  BackendHealth
    message: str           # Human-readable, safe to show in a dialog
    details: str = ""      # Technical detail for logs


# ---------------------------------------------------------------------------
# Backend exceptions
# ---------------------------------------------------------------------------

class BackendError(Exception):
    """Raised when a backend operation fails unrecoverably."""
    pass


class BackendUnavailableError(BackendError):
    """Raised when the backend (PipeWire / pactl) is not accessible."""
    pass


# ---------------------------------------------------------------------------
# Abstract backend
# ---------------------------------------------------------------------------

class AudioBackend(ABC):
    """
    Abstract interface for all audio backends.

    Contract:
      - health_check() must NEVER raise — always return a BackendStatus.
      - All other methods may raise BackendError on failure.
      - destroy_virtual_output() must NOT raise if the sink is already gone.
      - find_orphaned_virtual_sinks() must NOT raise even on partial failure.
    """

    @abstractmethod
    def health_check(self) -> BackendStatus:
        """
        Check whether the backend is operational.
        Called on startup, after every BackendError, and on Reset Audio.
        Must never raise — always returns BackendStatus.
        """
        ...

    @abstractmethod
    def list_outputs(self) -> list[AudioDevice]:
        """
        Return all currently available audio output devices.
        Excludes PipeMix virtual sinks.
        Raises BackendError on failure.
        """
        ...

    @abstractmethod
    def create_virtual_output(self, outputs: list[AudioDevice]) -> VirtualSink:
        """
        Create a combined virtual sink from the given output devices.
        Returns a VirtualSink on success.
        Raises BackendError if creation fails.
        """
        ...

    @abstractmethod
    def move_streams(self, target_sink_name: str, exclude_stream_ids: list[int] | None = None) -> None:
        """
        Move all currently active audio streams to the target sink,
        optionally excluding specific stream IDs.
        Raises BackendError on failure.
        """
        ...

    @abstractmethod
    def move_stream(self, stream_id: int, target_sink_name: str) -> None:
        """
        Move a specific audio stream (sink-input ID) to a specific target sink.
        Raises BackendError on failure.
        """
        ...

    @abstractmethod
    def destroy_virtual_output(self, sink: VirtualSink) -> None:
        """
        Unload the combined sink module from PipeWire.
        Safe to call even if the sink no longer exists — must not raise in that case.
        """
        ...

    @abstractmethod
    def get_sink_volume(self, sink_name: str) -> int:
        """
        Get the current volume of a sink (0-100).
        Raises BackendError on failure.
        """
        ...

    @abstractmethod
    def set_sink_volume(self, sink_name: str, volume_percent: int) -> None:
        """
        Set the volume of a sink (0-100).
        Raises BackendError on failure.
        """
        ...

    @abstractmethod
    def set_sink_mute(self, sink_name: str, mute: bool) -> None:
        """
        Set the mute state of a sink.
        Raises BackendError on failure.
        """
        ...

    @abstractmethod
    def find_orphaned_virtual_sinks(self) -> list[VirtualSink]:
        """
        Find any pipemix_* sinks in PipeWire not tracked by the current session.
        Used during startup crash recovery.
        Must not raise — returns an empty list on any error.
        """
        ...

    @abstractmethod
    def set_default_sink(self, sink_name: str) -> None:
        """
        Set the system default audio output.
        Raises BackendError on failure.
        """
        ...
