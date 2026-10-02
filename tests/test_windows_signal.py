from __future__ import annotations

import sys
from pathlib import Path


from pipemix.windows.signal import SignalEmitter


def test_handler_receives_the_emitter_first():
    emitter = SignalEmitter()
    seen = []
    emitter.connect("health-changed", lambda *args: seen.append(args))
    emitter.emit("health-changed", "payload")
    assert seen == [(emitter, "payload")]


def test_a_gobject_style_bound_handler_works_unchanged():
    # Exactly the shape ui/bridge.py uses, which is the whole point.
    class FakeBridge:
        def __init__(self):
            self.got = None

        def _on_health(self, _controller, status):
            self.got = status

    emitter = SignalEmitter()
    bridge = FakeBridge()
    emitter.connect("health-changed", bridge._on_health)
    emitter.emit("health-changed", {"engine": "leader"})
    assert bridge.got == {"engine": "leader"}


def test_a_raising_handler_does_not_stop_its_siblings():
    emitter = SignalEmitter()
    calls = []

    def boom(_emitter, _payload):
        calls.append("boom")
        raise RuntimeError("the page went away")

    emitter.connect("state-changed", boom)
    emitter.connect("state-changed", lambda _e, p: calls.append(p))
    emitter.emit("state-changed", "active")
    assert calls == ["boom", "active"]


def test_emitting_a_signal_nobody_listens_to_is_fine():
    SignalEmitter().emit("devices-changed", [])
