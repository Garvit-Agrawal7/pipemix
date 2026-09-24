"""The smallest thing that keeps `controller.emit(...)` / `controller.connect(...)`
working without `GObject.Object`.

Handlers run synchronously, on whatever thread calls `emit` — deliberately: no
threading cleverness here. `ui/bridge.py` already queues work onto the right
thread, and notification callbacks already resolve onto a worker thread before
they ever reach the Controller, so `emit` just has to call what it's given.
"""

from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)


class SignalEmitter:
    """Minimal connect/emit, one signal name at a time, any number of handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = {}

    def connect(self, name: str, handler: Callable) -> None:
        self._handlers.setdefault(name, []).append(handler)

    def emit(self, name: str, *args) -> None:
        for handler in list(self._handlers.get(name, ())):
            try:
                # The emitter goes first, because GObject does that and the
                # handlers on the other end are shared with the Linux build:
                # `Bridge._on_health(self, _controller, status)`. Calling them
                # with the payload alone raises a TypeError per signal and the
                # page never updates.
                handler(self, *args)
            except Exception:
                # A dead handler on the UI side must not take a routing
                # operation down with it, nor stop its sibling handlers.
                log.exception("Handler for signal %r raised", name)
