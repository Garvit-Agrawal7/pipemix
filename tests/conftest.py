from __future__ import annotations

import json
import sys
import threading
from unittest.mock import MagicMock

import pytest

try:
    import gi  # noqa: F401
except ImportError:
    # GObject / GLib are C extensions that may be absent (CI, Windows). Stub them just
    # enough for the Linux Controller module to import. test_bridge needs the real gi.
    _gi_mock = MagicMock()
    _gi_mock._pipemix_stub = True

    _gi_mock.repository.GObject.Object = type("Object", (), {
        "__init__": lambda self, *a, **kw: None,
        "emit": lambda self, *a, **kw: None,
    })
    sys.modules["gi"] = _gi_mock
    sys.modules["gi.repository"] = _gi_mock.repository

from pipemix.config import ConfigManager
from pipemix.linux import pactl_backend
from pipemix.linux.controller import Controller
from pipemix.models import AudioDevice, BackendError, BackendHealth, BackendStatus, DeviceKind, VirtualSink


def mkdev(mac: str, sink: str | None = None, kind=DeviceKind.BLUETOOTH) -> AudioDevice:
    return AudioDevice(id=mac, name="Dev", sink=sink, kind=kind, connected=sink is not None)


def _create(devices) -> VirtualSink:
    """Mirrors PactlBackend.create_sink: a hub is useless with nothing to feed."""
    if not any(d.sink for d in devices):
        raise BackendError("No resolvable sink names.")
    return VirtualSink(999, VirtualSink.make_name(), {d.sink: 1 for d in devices if d.sink})


def _fake_set_legs(s, ds):
    fresh = {d.sink for d in ds if d.sink} - set(s.legs)
    s.legs = {d.sink: 1 for d in ds if d.sink}
    return fresh


def make_backend(track_legs: bool = False) -> MagicMock:
    b = MagicMock()
    b.health.return_value = BackendStatus(BackendHealth.OK, "ok")
    b.find_orphans.return_value = []
    b.list_outputs.return_value = []
    b.list_streams.return_value = []
    b.create_sink.side_effect = _create
    if track_legs:
        b.set_legs.side_effect = _fake_set_legs
    return b


@pytest.fixture
def make_ctrl(tmp_path):
    def make(backend=None, track_legs: bool = False) -> Controller:
        c = Controller(backend or make_backend(track_legs), ConfigManager(tmp_path / "config.json"))
        # Bypass the BlueZ D-Bus monitor, which needs a real system bus.
        c.monitor = MagicMock()
        c.monitor.connected.return_value = []
        c._bg = lambda fn, *a: fn(*a)   # run jobs inline, so sync asserts hold
        c.start()
        return c
    return make


@pytest.fixture
def ctrl(make_ctrl) -> Controller:
    return make_ctrl()


@pytest.fixture
def devs():
    return mkdev("AA:BB:CC:DD:EE:01", "sink_a"), mkdev("AA:BB:CC:DD:EE:02", "sink_b")


# -- pactl fakes for the PactlBackend leg tests (test_core, test_latency_sync) --

HUB = "pipemix_test"


def node(name: str, ns: int) -> dict:
    p = {"direction": "Input", "minQuantum": 1.0, "minRate": 0, "minNs": ns}
    return {"type": "PipeWire:Interface:Node",
            "info": {"props": {"node.name": name}, "params": {"Latency": [
                p,
                {"direction": "Output", "minNs": 999_000_000},  # not Input: must be ignored
            ]}}}


class Fake:
    """pactl state: {module id: (name, args)}. Unloads are seen via _run, whatever path takes them."""

    def __init__(self, mp, lat: dict[str, int]) -> None:
        self.mods = {1: ("module-null-sink", f"sink_name={HUB}")}
        self.lat, self.next = lat, 100
        self.loads: list[list[str]] = []
        self.unloads: list[int] = []
        self.lists = 0
        mp.setattr(pactl_backend, "_run", self.run)
        mp.setattr(pactl_backend, "_load", self.load)

    def load(self, args: list[str]) -> int:
        self.loads.append(args)
        self.next += 1
        self.mods[self.next] = (args[0], " ".join(args[1:]))
        return self.next

    def run(self, args: list[str]) -> tuple[int, str, str]:
        if args == ["pw-dump"]:
            return 0, json.dumps([node(n, ns) for n, ns in self.lat.items()]), ""
        if args[-3:] == ["list", "short", "modules"]:
            self.lists += 1
            return 0, "".join(f"{i}\t{n}\t{a}\n" for i, (n, a) in self.mods.items()), ""
        if args[:2] == ["pactl", "unload-module"]:
            self.unloads.append(int(args[2]))
            return (0, "", "") if self.mods.pop(int(args[2]), None) else (1, "", "No such entity")
        return 1, "", f"unexpected {args}"

    def vanish(self, module: int) -> None:
        """The loopback unloaded itself (its sink went away)."""
        del self.mods[module]

    def reuse(self, module: int) -> None:
        """pipewire-pulse handed the dead id to someone else's module."""
        self.mods[module] = ("module-null-sink", "sink_name=someone_else")


def sink_dev(sink: str) -> AudioDevice:
    return AudioDevice(sink, sink, sink, DeviceKind.BUILTIN)


@pytest.fixture
def fake(monkeypatch) -> Fake:
    return Fake(monkeypatch, {"wired": 0, "bt": 200_000_000})


# -- Windows fakes (test_windows_*) --

def win_dev(dev_id: str, name: str = "Dev", connected: bool = True) -> AudioDevice:
    """A Windows endpoint: id *is* sink, with no resolution step."""
    return AudioDevice(id=dev_id, name=name, sink=dev_id if connected else None,
                       kind=DeviceKind.BLUETOOTH, connected=connected)


def win_backend(engine: str = "hub") -> MagicMock:
    """A hub-mode controller backend: no leader, every device is a leg."""
    b = MagicMock()
    b.health.return_value = BackendStatus(BackendHealth.OK, "ok", engine=engine)
    b.list_outputs.return_value = []
    b.get_default.return_value = "prev_default"
    b.restore_target.return_value = "prev_default"
    b.get_volume.return_value = 50
    b.leader = None
    b.apps_gen = 7

    def create(devices):
        if not devices:
            raise BackendError("No devices selected.")
        return VirtualSink(MagicMock(), VirtualSink.make_name(), {d.id: 0 for d in devices})
    b.create_sink.side_effect = create
    return b


class FakeEngine:
    """Stands in for wasapi.engine.Engine: records legs, never touches COM. Class state is
    reset per test. `start()` raises for pids in `fail_start_pids`; with `start_gate` set, the
    first start blocks (up to `start_wait`) until it is set; `join_gate` makes `join` wait on it."""

    instances: list[FakeEngine] = []
    fail_start_pids: set[int] = set()
    start_gate: threading.Event | None = None
    start_wait = 1.0
    entered = threading.Event()  # the first start() is running
    join_gate: threading.Event | None = None

    def __init__(self, source_id: str | None = None, *, pid: int | None = None) -> None:
        self.source_id = source_id
        self.pid = pid
        self.started = False
        self.stopped = False  # told to stop
        self.joined = False   # join() saw the pump exit
        self._legs: list[str] = []
        FakeEngine.instances.append(self)

    def start(self) -> None:
        if self.pid in FakeEngine.fail_start_pids:
            raise RuntimeError(f"could not activate process loopback for pid {self.pid}")
        if FakeEngine.start_gate and not FakeEngine.entered.is_set():
            FakeEngine.entered.set()
            FakeEngine.start_gate.wait(FakeEngine.start_wait)
        self.started = True

    def stop(self, wait: bool = True) -> None:
        self.stopped = True

    def join(self, timeout: float) -> bool:
        if self.stopped and (self.join_gate is None or self.join_gate.wait(timeout)):
            self.joined = True
        return self.joined

    def set_legs(self, device_ids) -> None:
        self._legs = list(device_ids)

    @property
    def legs(self) -> list[str]:
        return sorted(self._legs)


@pytest.fixture(autouse=True)
def _reset_fake_engine():
    FakeEngine.instances, FakeEngine.fail_start_pids = [], set()
    FakeEngine.start_gate, FakeEngine.start_wait, FakeEngine.join_gate = None, 1.0, None
    FakeEngine.entered = threading.Event()
