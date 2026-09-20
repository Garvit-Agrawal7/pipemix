"""Backend errors and health reporting. The concrete backend is PactlBackend."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BackendHealth(Enum):
    OK          = "ok"
    UNAVAILABLE = "unavailable"  # pactl / PipeWire not running
    DEGRADED    = "degraded"     # running, but something is wrong


@dataclass
class BackendStatus:
    health:  BackendHealth
    message: str  # shown in the UI
    engine:  str = "native"  # "native" on Linux; "hub" / "leader" on Windows


class BackendError(Exception):
    """A backend operation failed unrecoverably."""
