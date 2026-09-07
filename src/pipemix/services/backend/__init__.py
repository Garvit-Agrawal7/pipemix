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
    message: str       # shown in the UI
    details: str = ""  # logged only


class BackendError(Exception):
    """A backend operation failed unrecoverably."""
