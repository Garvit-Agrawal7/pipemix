from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from pipemix.models import AudioDevice, BackendError, BackendHealth, BackendStatus, DeviceKind, VirtualSink


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
