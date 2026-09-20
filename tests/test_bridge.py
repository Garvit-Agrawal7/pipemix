"""
The bridge must never block its caller.

pywebview's evaluate_js queues the script onto the GLib main loop and then
blocks until the result comes back. BlueZ connect and disconnect handlers run
on that same main loop, so a push that waited inline would deadlock the whole
UI. These tests pin that down without needing a window.
"""

from __future__ import annotations

import threading
import time

from gi.repository import GObject

from pipemix.linux.models import AudioDevice, DeviceKind, SessionState
from pipemix.linux.services.backend import BackendHealth, BackendStatus
from pipemix.linux.ui.bridge import Bridge, to_json


class FakeController(GObject.Object):
    __gsignals__ = {
        "state-changed":   (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "devices-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "health-changed":  (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }


class BlockingWindow:
    """Stands in for pywebview: evaluate_js does not return until released."""

    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def evaluate_js(self, script: str) -> None:
        self.scripts.append(script)
        self.entered.set()
        self.release.wait(5)


class FakeApi:
    def _devices_payload(self, devices):
        return [{"id": d.id, "selected": True} for d in devices]


def _bridge() -> tuple[Bridge, FakeController, BlockingWindow]:
    controller = FakeController()
    window = BlockingWindow()
    bridge = Bridge(controller, FakeApi())
    bridge.attach(window)
    return bridge, controller, window


def test_emitting_never_blocks_the_caller():
    """The signal emit must return even while the page is still busy."""
    bridge, controller, window = _bridge()
    device = AudioDevice(id="aa", name="Cans", sink="s", kind=DeviceKind.BLUETOOTH, connected=True)

    start = time.monotonic()
    controller.emit("devices-changed", [device])
    controller.emit("state-changed", SessionState.ACTIVE)
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, f"emit blocked for {elapsed:.2f}s — this is the UI freeze"
    assert window.entered.wait(2), "the worker never delivered the push"

    window.release.set()
    bridge.close()


def test_pushes_keep_their_order():
    bridge, controller, window = _bridge()
    window.release.set()  # let each call through immediately

    for state in (SessionState.STARTING, SessionState.ACTIVE, SessionState.IDLE):
        controller.emit("state-changed", state)

    deadline = time.monotonic() + 2
    while len(window.scripts) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert len(window.scripts) == 3
    assert [s.split('"state", ')[1].strip(")") for s in window.scripts] == [
        '"starting"', '"active"', '"idle"'
    ]
    bridge.close()


def test_push_before_a_window_exists_is_dropped():
    """Crash recovery emits during start(), before the page is attached."""
    controller = FakeController()
    Bridge(controller, FakeApi())
    controller.emit("state-changed", SessionState.IDLE)  # must not raise


def test_to_json_unwraps_dataclasses_and_enums():
    device = AudioDevice(id="aa", name="Cans", sink=None, kind=DeviceKind.BLUETOOTH, battery=80)
    assert to_json(device) == {
        "id": "aa", "name": "Cans", "sink": None, "kind": "bluetooth",
        "connected": False, "battery": 80, "volume": 50,
    }
    assert to_json(SessionState.REPAIRING) == "repairing"
    assert to_json(BackendStatus(BackendHealth.DEGRADED, "hmm")) == {
        "health": "degraded", "message": "hmm",
    }
