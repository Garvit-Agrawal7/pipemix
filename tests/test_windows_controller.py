"""Tests for the Windows Controller — leader re-election and hotplug.

Stubs the notification client and the backend the same way `test_routing.py`
stubs GObject and PactlBackend: the Controller module itself imports no GTK
or COM at module scope (only `wasapi.notify`, which is real pycaw/comtypes
and safe to construct — it only touches COM inside `start()`/`stop()`, never
in `__init__`), so no `sys.modules` stubbing is needed. `Controller.monitor`
is swapped for a `MagicMock` before `start()` so no real device enumerator is
ever registered.

Everything carried over verbatim from `linux/controller.py` (the lock, the
solo-device volume rule, presets, `_prepare`/`_adopt`) is already covered by
`test_routing.py`; these tests cover what changed: stable endpoint ids with
no retry chain, and leader re-election.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipemix.models import AudioDevice, DeviceKind, SessionState, VirtualSink
from pipemix.linux.services.backend import BackendError, BackendHealth, BackendStatus
from pipemix.linux.services.config.config_manager import ConfigManager
from pipemix.windows.controller import Controller


def _dev(dev_id: str, name: str = "Dev", connected: bool = True) -> AudioDevice:
    """A Windows endpoint: id *is* sink, per the brief — there is no separate
    resolution step."""
    return AudioDevice(id=dev_id, name=name, sink=dev_id if connected else None,
                        kind=DeviceKind.BLUETOOTH, connected=connected)


def _fake_create(devices: list[AudioDevice]) -> VirtualSink:
    if not devices:
        raise BackendError("No devices selected.")
    return VirtualSink(MagicMock(), VirtualSink.make_name(), {d.id: 0 for d in devices})


def _backend(engine: str = "hub") -> MagicMock:
    """A hub-mode backend: no leader, every device is a leg."""
    b = MagicMock()
    b.health.return_value = BackendStatus(BackendHealth.OK, "ok", engine=engine)
    b.find_orphans.return_value = []
    b.list_outputs.return_value = []
    b.get_default.return_value = "prev_default"
    b.get_volume.return_value = 50
    b.leader = None
    b.create_sink.side_effect = _fake_create
    return b


def _leader_backend() -> MagicMock:
    """A leader-mode backend: `create_sink` elects the first device as
    leader (mirrors `WasapiBackend._elect_leader`'s fallback), excludes it
    from the legs, and `destroy_sink` clears the election — same lifecycle
    as the real backend."""
    b = MagicMock()
    b.health.return_value = BackendStatus(BackendHealth.OK, "ok", engine="leader")
    b.find_orphans.return_value = []
    b.list_outputs.return_value = []
    b.get_default.return_value = "prev_default"
    b.get_volume.return_value = 50
    b.leader = None

    def _create(devices: list[AudioDevice]) -> VirtualSink:
        if not devices:
            raise BackendError("No devices selected.")
        b.leader = devices[0].id
        legs = [d for d in devices if d.id != b.leader]
        return VirtualSink(MagicMock(), VirtualSink.make_name(), {d.id: 0 for d in legs})

    def _destroy(_sink) -> None:
        b.leader = None

    b.create_sink.side_effect = _create
    b.destroy_sink.side_effect = _destroy
    return b


def _ctrl(tmp_path: Path, backend=None) -> Controller:
    b = backend or _backend()
    cfg = ConfigManager(tmp_path / "config.json")
    c = Controller(b, cfg)
    # Bypass the real notification client — it needs a live MMDevice enumerator.
    c.monitor = MagicMock()
    c.monitor.connected.return_value = []
    c.start()
    return c


# -- Leader re-election: a survivor takes over --

def test_leader_disconnect_reelects_a_survivor(tmp_path: Path) -> None:
    b = _leader_backend()
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2, d3 = _dev("EP1"), _dev("EP2"), _dev("EP3")
    ctrl.devices = {d.id: d for d in (d1, d2, d3)}

    ctrl.start_sharing([d1, d2, d3])
    old_leader = b.leader
    assert old_leader == d1.id
    b.create_sink.reset_mock()

    ctrl._on_disconnect(old_leader)

    assert ctrl.session.state == SessionState.ACTIVE
    assert b.leader is not None and b.leader != old_leader
    assert b.leader in (d2.id, d3.id)
    b.create_sink.assert_called_once()
    b.destroy_sink.assert_called_once()
    # The new session excludes the dead leader from its own targets.
    assert old_leader not in ctrl.targets


def test_leader_disconnect_with_no_survivors_errors(tmp_path: Path) -> None:
    b = _leader_backend()
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}

    ctrl.start_sharing([d1, d2])
    leader = b.leader
    other = d2.id if leader == d1.id else d1.id

    ctrl._on_disconnect(other)               # the only leg drops first
    assert ctrl.session.state == SessionState.ACTIVE

    ctrl._on_disconnect(leader)              # then the leader itself

    assert ctrl.session.state == SessionState.ERROR
    assert ctrl.session.sink is None


# -- A non-leader disconnect only rebuilds the legs --

def test_non_leader_disconnect_only_rebuilds_legs(tmp_path: Path) -> None:
    b = _leader_backend()
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2, d3 = _dev("EP1"), _dev("EP2"), _dev("EP3")
    ctrl.devices = {d.id: d for d in (d1, d2, d3)}

    ctrl.start_sharing([d1, d2, d3])
    leader = b.leader
    non_leader = next(d for d in (d1, d2, d3) if d.id != leader)
    b.create_sink.reset_mock()
    b.destroy_sink.reset_mock()

    ctrl._on_disconnect(non_leader.id)

    b.create_sink.assert_not_called()
    b.destroy_sink.assert_not_called()
    b.set_legs.assert_called_once()
    assert b.leader == leader                # the leader itself is untouched
    assert ctrl.session.state == SessionState.ACTIVE


# -- Hub mode has no leader to lose --

def test_hub_mode_disconnect_never_reelects(tmp_path: Path) -> None:
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}

    ctrl.start_sharing([d1, d2])
    sink = ctrl.session.sink
    b.create_sink.reset_mock()

    ctrl._on_disconnect(d1.id)

    b.create_sink.assert_not_called()
    b.destroy_sink.assert_not_called()
    b.set_legs.assert_called_with(sink, [d2])
    assert ctrl.session.state == SessionState.ACTIVE
    assert ctrl.session.sink is sink


# -- Reconnect: immediate, no retry chain --

def test_on_connect_readopts_without_a_retry_chain(tmp_path: Path) -> None:
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}
    ctrl.start_sharing([d1, d2])

    ctrl._on_disconnect(d2.id)
    assert ctrl.devices[d2.id].connected is False
    assert ctrl.session.state == SessionState.ACTIVE

    # The endpoint is back — list_outputs reports it active on the very next
    # call, which is all `_on_connect` gets to work with; a Windows endpoint
    # id is stable, so this must resolve in one shot, not a retry loop.
    b.list_outputs.return_value = [
        AudioDevice(id=d1.id, name=d1.name, sink=d1.id, kind=DeviceKind.BLUETOOTH, connected=True),
        AudioDevice(id=d2.id, name=d2.name, sink=d2.id, kind=DeviceKind.BLUETOOTH, connected=True),
    ]
    b.set_legs.reset_mock()
    b.list_outputs.reset_mock()

    ctrl._on_connect(d2.id)

    b.list_outputs.assert_called_once()
    assert ctrl.devices[d2.id].connected is True
    assert ctrl.devices[d2.id].sink == d2.id
    b.set_legs.assert_called_once()
    assert ctrl.session.state == SessionState.ACTIVE


def test_on_connect_of_a_non_target_does_not_rebuild(tmp_path: Path) -> None:
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1 = _dev("EP1")
    ctrl.devices = {d1.id: d1}
    ctrl.start_sharing([d1])
    b.set_legs.reset_mock()

    b.list_outputs.return_value = [
        AudioDevice(id=d1.id, name=d1.name, sink=d1.id, kind=DeviceKind.BLUETOOTH, connected=True),
        AudioDevice(id="EP-new", name="New", sink="EP-new", kind=DeviceKind.BLUETOOTH, connected=True),
    ]
    ctrl._on_connect("EP-new")

    assert "EP-new" in ctrl.devices
    b.set_legs.assert_not_called()


if __name__ == "__main__":
    import tempfile

    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            with tempfile.TemporaryDirectory() as tmp:
                fn(Path(tmp))
            print(f"  \u2713  {name}")
    print("\nAll tests passed.")
