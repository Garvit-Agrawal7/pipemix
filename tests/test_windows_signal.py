"""SignalEmitter has to be a drop-in for GObject.Object.

It exists for exactly one reason: `windows/controller.py` is a fork of the
Linux controller, and the things listening to it — `ui/bridge.py` above all —
are *shared source*, written against GObject's calling convention. GObject
passes the emitting object as the first argument to every handler, which is
why `Bridge._on_health` is declared `(self, _controller, status)`.

Getting that wrong does not crash anything: `SignalEmitter.emit` logs the
TypeError per handler and carries on, so the app starts, the window opens, the
first paint works (the page pulls `snapshot()` itself) and then nothing ever
updates again. These tests are here because the only way that was caught was
launching the real GUI and reading its log.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

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
