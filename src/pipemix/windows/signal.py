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
                # Emitter first, as GObject does: the handlers are shared with Linux
                # (`Bridge._on_health(self, _controller, status)`) and would TypeError.
                handler(self, *args)
            except Exception:
                # A dead handler on the UI side must not take a routing
                # operation down with it, nor stop its sibling handlers.
                log.exception("Handler for signal %r raised", name)
