"""
PipeMix — Controller signals to the web page.

Three signals, one direction. Everything the other way goes through Api.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pipemix.controller import Controller
    from pipemix.ui.api import Api

log = logging.getLogger(__name__)


def to_json(obj: Any) -> Any:
    """Dataclasses become dicts, Enum members their value. All three are strings."""
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_json(v) for k, v in asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [to_json(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_json(v) for k, v in obj.items()}
    return obj


class Bridge:

    def __init__(self, controller: Controller, api: Api) -> None:
        self.api = api
        self.window = None

        controller.connect("devices-changed", self._on_devices)
        controller.connect("state-changed", self._on_state)
        controller.connect("health-changed", self._on_health)

    def attach(self, window) -> None:
        self.window = window

    def _push(self, event: str, payload: Any) -> None:
        # Signals can fire before the page exists (crash recovery on startup)
        # and after it goes away (teardown); neither is worth an error.
        if not self.window:
            return
        try:
            self.window.evaluate_js(
                f"window.pipemix && window.pipemix.push({json.dumps(event)}, {json.dumps(payload)})"
            )
        except Exception as e:
            log.debug("Could not push '%s' to the page: %s", event, e)

    def _on_devices(self, _controller, devices) -> None:
        self._push("devices", self.api.devices_payload(devices))

    def _on_state(self, _controller, state) -> None:
        self._push("state", to_json(state))

    def _on_health(self, _controller, status) -> None:
        self._push("health", to_json(status))
