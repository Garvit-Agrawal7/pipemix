from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipemix.models import AudioDevice, DeviceKind, SessionState, VirtualSink
from pipemix.linux.services.backend import BackendError, BackendHealth, BackendStatus
from pipemix.linux.services.config.config_manager import ConfigManager
from pipemix.windows import controller as controller_module
from pipemix.windows.controller import Controller

_REAL_POLL = Controller._poll_apps


@pytest.fixture(autouse=True)
def _no_poll(monkeypatch):
    """The poll thread exits at once, so tests drive `_sync_apps` themselves.
    Tests about the poll itself put `_REAL_POLL` back."""
    monkeypatch.setattr(Controller, "_poll_apps", lambda self, stop, wake: None)


def _dev(dev_id: str, name: str = "Dev", connected: bool = True) -> AudioDevice:
    """A Windows endpoint: id *is* sink, with no resolution step."""
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
    b.restore_target.return_value = "prev_default"
    b.get_volume.return_value = 50
    b.leader = None
    b.apps_gen = 7
    b.create_sink.side_effect = _fake_create
    return b


def _leader_backend() -> MagicMock:
    """A leader-mode backend with the real lifecycle: `create_sink` elects the first
    device and excludes it from the legs; `destroy_sink` clears the election."""
    b = MagicMock()
    b.health.return_value = BackendStatus(BackendHealth.OK, "ok", engine="leader")
    b.find_orphans.return_value = []
    b.list_outputs.return_value = []
    b.get_default.return_value = "prev_default"
    b.restore_target.return_value = "prev_default"
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

    # list_outputs reports the endpoint at once and its id is stable, so
    # `_on_connect` must resolve it in one shot, not a retry loop.
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


# -- Volume: unmute param must be accepted, and applied on the non-solo master path --

def test_set_device_volume_accepts_unmute_param(tmp_path: Path) -> None:
    b = _backend()
    ctrl = _ctrl(tmp_path, backend=b)
    d1 = _dev("EP1")
    ctrl.devices = {d1.id: d1}

    ctrl.set_device_volume(d1.id, 75, unmute=True)

    b.set_mute.assert_called_once_with(d1.sink, False)
    b.set_volume.assert_called_once_with(d1.sink, 75)
    assert ctrl.devices[d1.id].volume == 75


def test_set_master_volume_unmutes_hub_when_asked(tmp_path: Path) -> None:
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}
    ctrl.start_sharing([d1, d2])
    b.set_mute.reset_mock()
    b.set_volume.reset_mock()

    ctrl.set_master_volume(60, unmute=True)

    b.set_mute.assert_called_once_with(ctrl.active_sink(), False)
    b.set_volume.assert_called_once_with(ctrl.active_sink(), 60)


def test_set_master_volume_without_unmute_does_not_touch_mute(tmp_path: Path) -> None:
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}
    ctrl.start_sharing([d1, d2])
    b.set_mute.reset_mock()
    b.set_volume.reset_mock()

    ctrl.set_master_volume(60)

    b.set_mute.assert_not_called()
    b.set_volume.assert_called_once_with(ctrl.active_sink(), 60)


def test_streams_and_route_stream_speak_the_api_shape(tmp_path: Path) -> None:
    b = _backend(engine="hub")
    b.list_streams.return_value = [
        {"id": 42, "name": "App", "sink": "EP1", "mute": False, "exe": "C:\\App.exe"},
    ]
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}

    assert ctrl.streams()[0]["devices"] is None
    ctrl.route_stream(42, [d2.id])
    b.move_stream.assert_called_with(42, "EP2")
    assert ctrl.streams()[0]["devices"] == ["EP2"]

    try:
        ctrl.route_stream(42, [d1.id, d2.id])
        raise AssertionError("fan-out per app should be refused")
    except BackendError:
        pass

    ctrl.route_stream(42, None)                 # clear the pin
    b.move_stream.assert_called_with(42, None)
    assert ctrl.streams()[0]["devices"] is None

# -- Stuck: PipeMix accepted a route, but the app hasn't reopened its audio --

def test_stuck_pinned_app_playing_on_the_wrong_endpoint(tmp_path: Path) -> None:
    b = _backend(engine="hub")
    b.list_streams.return_value = [
        {"id": 42, "name": "App", "sink": "EP1", "endpoint": "EP1", "active": True,
         "mute": False, "exe": "C:\\App.exe"},
    ]
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}
    ctrl.route_stream(42, [d2.id])              # pinned to EP2, still playing on EP1

    assert ctrl.streams()[0]["stuck"] is True

    b.list_streams.return_value[0]["endpoint"] = "EP2"
    assert ctrl.streams()[0]["stuck"] is False


def test_stuck_unpinned_app_while_sharing_follows_the_hub(tmp_path: Path) -> None:
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}
    ctrl.start_sharing([d1, d2])
    hub = ctrl.active_sink()

    b.list_streams.return_value = [
        {"id": 42, "name": "App", "sink": hub, "endpoint": hub, "active": True,
         "mute": False, "exe": "C:\\App.exe"},
    ]
    assert ctrl.streams()[0]["stuck"] is False

    b.list_streams.return_value[0]["endpoint"] = "EP1"
    assert ctrl.streams()[0]["stuck"] is True


def test_stuck_never_true_without_an_active_stream_or_session(tmp_path: Path) -> None:
    b = _backend(engine="hub")
    b.list_streams.return_value = [
        {"id": 42, "name": "App", "sink": "EP1", "endpoint": "EP1", "active": False,
         "mute": False, "exe": "C:\\App.exe"},
    ]
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}
    ctrl.start_sharing([d1, d2])
    ctrl.route_stream(42, [d2.id])

    # Pinned elsewhere, session up, but the stream itself is idle.
    assert ctrl.streams()[0]["stuck"] is False

    # No session and no pin: nothing to expect, so nothing is stuck either.
    ctrl2 = _ctrl(tmp_path / "idle", backend=_backend(engine="hub"))
    ctrl2.backend.list_streams.return_value = [
        {"id": 7, "name": "App2", "sink": "EP1", "endpoint": "EP1", "active": True,
         "mute": False, "exe": "C:\\App2.exe"},
    ]
    assert ctrl2.streams()[0]["stuck"] is False



# -- Per-app pins: no blanket move_streams, and pins are tracked by exe --

def test_start_sharing_never_calls_move_streams(tmp_path: Path) -> None:
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1 = _dev("EP1")
    ctrl.devices = {d1.id: d1}

    ctrl.start_sharing([d1])

    b.move_streams.assert_not_called()


def test_route_stream_records_and_clears_pinned_app(tmp_path: Path) -> None:
    b = _backend(engine="hub")
    b.list_streams.return_value = [
        {"id": 42, "name": "App", "sink": "EP1", "mute": False, "exe": "C:\\App.exe"},
    ]
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}

    ctrl.route_stream(42, [d2.id])
    assert ctrl.config.data["pinned_apps"] == ["C:\\App.exe"]

    ctrl.route_stream(42, None)
    assert ctrl.config.data["pinned_apps"] == []


def test_stop_sharing_unpins_a_live_pinned_app(tmp_path: Path) -> None:
    # Leader mode: under contract D a hub session's route_stream no longer pins,
    # so this pin-lifecycle test uses the mode that still does.
    b = _leader_backend()
    b.list_streams.return_value = [
        {"id": 42, "name": "App", "sink": "EP1", "mute": False, "exe": "C:\\App.exe"},
    ]
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}
    ctrl.start_sharing([d1, d2])
    ctrl.route_stream(42, [d2.id])
    assert ctrl.config.data["pinned_apps"] == ["C:\\App.exe"]
    b.move_stream.reset_mock()

    ctrl.stop_sharing()

    b.move_stream.assert_called_with(42, None)
    assert ctrl.config.data["pinned_apps"] == []


def test_pinned_app_survives_until_seen_again_under_a_new_pid(tmp_path: Path) -> None:
    # Leader mode: see the comment on test_stop_sharing_unpins_a_live_pinned_app.
    b = _leader_backend()
    b.list_streams.return_value = [
        {"id": 42, "name": "App", "sink": "EP1", "mute": False, "exe": "C:\\App.exe"},
    ]
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}
    ctrl.start_sharing([d1, d2])
    ctrl.route_stream(42, [d2.id])

    b.list_streams.return_value = []            # the app closed before stop_sharing
    ctrl.stop_sharing()
    assert ctrl.config.data["pinned_apps"] == ["C:\\App.exe"]  # no live pid to clear it through

    # It reopens under a new pid.
    b.list_streams.return_value = [
        {"id": 99, "name": "App", "sink": "EP1", "mute": False, "exe": "C:\\App.exe"},
    ]
    b.move_stream.reset_mock()
    ctrl.streams()

    b.move_stream.assert_called_with(99, None)
    assert ctrl.config.data["pinned_apps"] == []


def test_streams_does_not_sweep_a_currently_overridden_app(tmp_path: Path) -> None:
    # Leader mode: see the comment on test_stop_sharing_unpins_a_live_pinned_app.
    b = _leader_backend()
    b.list_streams.return_value = [
        {"id": 42, "name": "App", "sink": "EP1", "mute": False, "exe": "C:\\App.exe"},
    ]
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}
    ctrl.start_sharing([d1, d2])
    ctrl.route_stream(42, [d2.id])
    b.move_stream.reset_mock()

    ctrl.streams()

    b.move_stream.assert_not_called()
    assert ctrl.config.data["pinned_apps"] == ["C:\\App.exe"]


def test_start_and_stop_push_devices_so_the_page_sees_targets(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path, backend=_backend(engine="hub"))
    d1 = _dev("EP1")
    ctrl.devices = {d1.id: d1}
    pushed = []
    ctrl.connect("devices-changed", lambda _c, devs: pushed.append(set(ctrl.targets)))

    ctrl.start_sharing([d1])
    assert pushed[-1] == {"EP1"}
    ctrl.stop_sharing()
    assert pushed[-1] == set()


# -- Contract D: per-app capture in hub mode --
#
# Hub sessions never pin: route_stream records `overrides`, and `_sync_apps`
# reconciles them via `backend.set_app_routes` (called directly below, or by the
# 1 s poll). Leader mode keeps the pin path.

def test_hub_route_stream_fans_out_without_pinning(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(controller_module, "APP_POLL_S", 999)
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}
    ctrl.start_sharing([d1, d2])
    hub = ctrl.active_sink()
    b.list_streams.return_value = [
        {"id": 42, "name": "App", "sink": hub, "endpoint": hub, "active": True,
         "mute": False, "exe": "C:\\App.exe"},
    ]

    ctrl.route_stream(42, [d1.id, d2.id])

    b.move_stream.assert_not_called()                  # no pin — a live fan-out
    assert set(ctrl.overrides[42]) == {d1.id, d2.id}

    b.set_app_routes.reset_mock()
    ctrl._sync_apps()

    assert b.set_app_routes.call_count == 1
    routes = b.set_app_routes.call_args[0][0]
    assert set(routes[42]) == {d1.id, d2.id}


def test_hub_route_stream_none_falls_back_to_session_devices(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(controller_module, "APP_POLL_S", 999)
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}
    ctrl.start_sharing([d1, d2])
    hub = ctrl.active_sink()
    b.list_streams.return_value = [
        {"id": 42, "name": "App", "sink": hub, "endpoint": hub, "active": True,
         "mute": False, "exe": "C:\\App.exe"},
    ]
    ctrl.route_stream(42, [d1.id])
    assert ctrl.overrides.get(42) == [d1.id]

    ctrl.route_stream(42, None)                         # clear the override

    assert 42 not in ctrl.overrides
    b.set_app_routes.reset_mock()
    ctrl._sync_apps()

    routes = b.set_app_routes.call_args[0][0]
    assert set(routes[42]) == {d1.id, d2.id}            # follows every session device


def test_sync_apps_only_routes_active_streams_on_the_hub(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(controller_module, "APP_POLL_S", 999)
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}
    ctrl.start_sharing([d1, d2])
    hub = ctrl.active_sink()
    b.list_streams.return_value = [
        {"id": 1, "name": "OnHub", "sink": hub, "endpoint": hub, "active": True,
         "mute": False, "exe": "a.exe"},
        {"id": 2, "name": "Idle", "sink": hub, "endpoint": hub, "active": False,
         "mute": False, "exe": "b.exe"},
        {"id": 3, "name": "Elsewhere", "sink": "EP1", "endpoint": "EP1", "active": True,
         "mute": False, "exe": "c.exe"},
    ]
    b.set_app_routes.reset_mock()

    ctrl._sync_apps()

    routes = b.set_app_routes.call_args[0][0]
    assert set(routes.keys()) == {1}


def test_sync_apps_emits_streams_changed_only_on_change(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(controller_module, "APP_POLL_S", 999)
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1 = _dev("EP1")
    ctrl.devices = {d1.id: d1}
    ctrl.start_sharing([d1])
    hub = ctrl.active_sink()
    b.list_streams.return_value = [
        {"id": 1, "name": "App", "sink": hub, "endpoint": hub, "active": True,
         "mute": False, "exe": "a.exe"},
    ]
    seen = []
    ctrl.connect("streams-changed", lambda _c, out: seen.append(out))

    ctrl._sync_apps()
    assert len(seen) == 1

    ctrl._sync_apps()                                   # nothing changed
    assert len(seen) == 1

    b.list_streams.return_value[0]["active"] = False
    ctrl._sync_apps()
    assert len(seen) == 2


def test_stuck_in_hub_mode_ignores_overrides(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(controller_module, "APP_POLL_S", 999)
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}
    ctrl.start_sharing([d1, d2])
    hub = ctrl.active_sink()
    b.list_streams.return_value = [
        {"id": 42, "name": "App", "sink": hub, "endpoint": hub, "active": True,
         "mute": False, "exe": "a.exe"},
    ]
    ctrl.route_stream(42, [d2.id])                       # overridden to EP2 only

    # Playing into the hub is right in hub mode: the override is capture-side.
    assert ctrl.streams()[0]["stuck"] is False

    b.list_streams.return_value[0]["endpoint"] = "EP1"   # bypassed the hub entirely
    assert ctrl.streams()[0]["stuck"] is True


def test_leader_mode_route_stream_still_pins_and_rejects_fanout(tmp_path: Path) -> None:
    b = _leader_backend()
    b.list_streams.return_value = [
        {"id": 42, "name": "App", "sink": "EP1", "mute": False, "exe": "C:\\App.exe"},
    ]
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2, d3 = _dev("EP1"), _dev("EP2"), _dev("EP3")
    ctrl.devices = {d.id: d for d in (d1, d2, d3)}
    ctrl.start_sharing([d1, d2, d3])

    ctrl.route_stream(42, [d2.id])

    b.move_stream.assert_called_with(42, "EP2")
    assert ctrl.overrides[42] == [d2.id]

    try:
        ctrl.route_stream(42, [d2.id, d3.id])
        raise AssertionError("fan-out per app should still be refused in leader mode")
    except BackendError:
        pass


def test_hub_route_stream_clears_a_leftover_pinned_apps_entry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(controller_module, "APP_POLL_S", 999)
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}
    ctrl.start_sharing([d1, d2])
    # A leftover pin from an idle/leader-mode run, keyed by exe; seeded directly
    # since only the pin-clearing branch matters here.
    ctrl.config.data["pinned_apps"] = ["C:\\App.exe"]
    ctrl._exe[42] = "C:\\App.exe"

    ctrl.route_stream(42, [d2.id])

    b.move_stream.assert_called_with(42, None)
    assert ctrl.config.data["pinned_apps"] == []


def test_on_disconnect_drops_device_from_override_lists(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(controller_module, "APP_POLL_S", 999)
    b = _backend(engine="hub")
    # Both pids must stay live, or streams() prunes their overrides first.
    b.list_streams.return_value = [
        {"id": 42, "name": "A", "sink": "EP1", "mute": False, "exe": "C:\\A.exe"},
        {"id": 43, "name": "B", "sink": "EP1", "mute": False, "exe": "C:\\B.exe"},
    ]
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2, d3 = _dev("EP1"), _dev("EP2"), _dev("EP3")
    ctrl.devices = {d.id: d for d in (d1, d2, d3)}
    ctrl.start_sharing([d1, d2, d3])
    ctrl.overrides[42] = [d1.id, d2.id]
    ctrl.overrides[43] = [d1.id]
    b.set_app_routes.reset_mock()

    ctrl._app_wake.clear()

    ctrl._on_disconnect(d1.id)

    assert ctrl.overrides[42] == [d2.id]
    assert 43 not in ctrl.overrides                      # emptied out entirely
    b.set_app_routes.assert_not_called()                 # never under the lock...
    assert ctrl._app_wake.is_set()                       # ...the poll re-syncs instead
    ctrl._sync_apps()
    b.set_app_routes.assert_called()


def test_retarget_resyncs_apps(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(controller_module, "APP_POLL_S", 999)
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}
    ctrl.start_sharing([d1])
    b.set_app_routes.reset_mock()
    ctrl._app_wake.clear()

    ctrl.start_sharing([d1, d2])                         # session already up -> _retarget

    b.set_app_routes.assert_not_called()
    assert ctrl._app_wake.is_set()
    ctrl._sync_apps()
    b.set_app_routes.assert_called()
    assert b.set_app_routes.call_args[1] == {"gen": 7}


def test_start_sharing_hub_starts_the_poll_thread(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(controller_module, "APP_POLL_S", 999)
    monkeypatch.setattr(Controller, "_poll_apps", _REAL_POLL)
    b = _backend(engine="hub")
    synced = threading.Event()
    b.set_app_routes.side_effect = lambda *a, **k: synced.set()
    ctrl = _ctrl(tmp_path, backend=b)
    d1 = _dev("EP1")
    ctrl.devices = {d1.id: d1}
    b.list_streams.return_value = []

    ctrl.start_sharing([d1])

    assert ctrl._app_poll is not None and ctrl._app_poll.is_alive()
    assert synced.wait(1)                                # first sync is immediate

    ctrl.stop_sharing()

    assert ctrl._app_poll_stop is not None and ctrl._app_poll_stop.is_set()
    ctrl._app_poll.join(1)
    assert not ctrl._app_poll.is_alive()                 # woken, not left in a 999 s wait


def _blocking_hub(tmp_path: Path, monkeypatch):
    """A hub session whose real poll thread is stuck inside set_app_routes
    (a slow Engine.start) until the test sets `release`."""
    monkeypatch.setattr(controller_module, "APP_POLL_S", 999)
    monkeypatch.setattr(Controller, "_poll_apps", _REAL_POLL)
    b = _backend(engine="hub")
    entered, release = threading.Event(), threading.Event()

    def _slow(*_a, **_k):
        entered.set()
        release.wait(2)
    b.set_app_routes.side_effect = _slow
    b.list_streams.return_value = []
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}
    ctrl.start_sharing([d1, d2])
    assert entered.wait(1)
    return ctrl, b, release


def test_locked_methods_dont_wait_for_an_in_flight_app_sync(tmp_path: Path, monkeypatch) -> None:
    ctrl, b, release = _blocking_hub(tmp_path, monkeypatch)
    try:
        t0 = time.monotonic()
        ctrl.route_stream(42, ["EP1"])                   # @locked
        ctrl._on_disconnect("EP2")                       # @locked
        assert time.monotonic() - t0 < 0.5
        assert ctrl.overrides[42] == ["EP1"]
    finally:
        release.set()
        ctrl.stop_sharing()
        ctrl._app_poll.join(1)


def test_stop_sharing_during_an_in_flight_sync_stops_the_poll(tmp_path: Path, monkeypatch) -> None:
    ctrl, b, release = _blocking_hub(tmp_path, monkeypatch)
    poll = ctrl._app_poll

    t0 = time.monotonic()
    ctrl.stop_sharing()                                  # must not join/wait on the poll
    assert time.monotonic() - t0 < 0.5
    b.destroy_sink.assert_called_once()

    release.set()
    poll.join(1)
    assert not poll.is_alive()
    assert ctrl.session.state == SessionState.IDLE


def test_hub_route_stream_wakes_the_poll_instead_of_syncing(tmp_path: Path, monkeypatch) -> None:
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1 = _dev("EP1")
    ctrl.devices = {d1.id: d1}
    ctrl.start_sharing([d1])
    ctrl._app_wake.clear()
    b.set_app_routes.reset_mock()

    ctrl.route_stream(42, [d1.id])

    b.set_app_routes.assert_not_called()
    assert ctrl._app_wake.is_set()


def test_sync_apps_noop_after_stop_sharing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(controller_module, "APP_POLL_S", 999)
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1 = _dev("EP1")
    ctrl.devices = {d1.id: d1}
    ctrl.start_sharing([d1])
    ctrl.stop_sharing()
    b.set_app_routes.reset_mock()

    ctrl._sync_apps()

    b.set_app_routes.assert_not_called()


# -- Idle grace: a paused app keeps its capture for APP_IDLE_S --

def _idle_setup(tmp_path: Path, monkeypatch):
    """A hub session with one app playing, and a clock the test moves."""
    monkeypatch.setattr(controller_module, "APP_POLL_S", 999)
    clock = [1000.0]
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: clock[0])
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1 = _dev("EP1")
    ctrl.devices = {d1.id: d1}
    ctrl.start_sharing([d1])
    hub = ctrl.active_sink()
    app = {"id": 42, "name": "App", "sink": hub, "endpoint": hub, "active": True,
           "mute": False, "exe": "a.exe"}
    b.list_streams.return_value = [app]
    ctrl._sync_apps()
    return ctrl, b, app, clock


def _routes(b) -> dict:
    return b.set_app_routes.call_args[0][0]


def test_pause_then_resume_within_grace_keeps_the_engine(tmp_path: Path, monkeypatch) -> None:
    ctrl, b, app, clock = _idle_setup(tmp_path, monkeypatch)
    b.set_app_routes.reset_mock()

    app["active"] = False
    app["endpoint"] = "EP1"          # a stale idle session can win the dedupe
    for _ in range(3):
        clock[0] += 5
        ctrl._sync_apps()
    app["active"] = True
    app["endpoint"] = ctrl.active_sink()
    ctrl._sync_apps()

    assert all(42 in c[0][0] for c in b.set_app_routes.call_args_list)
    assert _routes(b)[42] == ["EP1"]


def test_idle_past_grace_drops_the_app(tmp_path: Path, monkeypatch) -> None:
    ctrl, b, app, clock = _idle_setup(tmp_path, monkeypatch)
    app["active"] = False

    clock[0] += controller_module.APP_IDLE_S - 1
    ctrl._sync_apps()
    assert 42 in _routes(b)

    clock[0] += 2
    ctrl._sync_apps()
    assert 42 not in _routes(b)
    assert 42 not in ctrl._app_active


def test_vanished_app_drops_immediately(tmp_path: Path, monkeypatch) -> None:
    ctrl, b, app, clock = _idle_setup(tmp_path, monkeypatch)
    b.list_streams.return_value = []

    ctrl._sync_apps()

    assert _routes(b) == {}
    assert ctrl._app_active == {}


def test_app_playing_elsewhere_drops_immediately(tmp_path: Path, monkeypatch) -> None:
    ctrl, b, app, clock = _idle_setup(tmp_path, monkeypatch)
    app["endpoint"] = "EP1"          # active, but bypassing the hub

    ctrl._sync_apps()

    assert 42 not in _routes(b)


def test_stop_sharing_clears_idle_state(tmp_path: Path, monkeypatch) -> None:
    ctrl, b, app, clock = _idle_setup(tmp_path, monkeypatch)
    assert 42 in ctrl._app_active

    ctrl.stop_sharing()

    assert ctrl._app_active == {}


# -- A stale poll and a slow engine shutdown never hold up the next session --

def test_sync_apps_of_a_stopped_poll_touches_nothing(tmp_path: Path, monkeypatch) -> None:
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1 = _dev("EP1")
    ctrl.devices = {d1.id: d1}
    ctrl.start_sharing([d1])                             # the next session is already up
    hub = ctrl.active_sink()
    b.list_streams.return_value = [{"id": 42, "name": "App", "sink": hub, "endpoint": hub,
                                    "active": True, "mute": False, "exe": "a.exe"}]
    b.set_app_routes.reset_mock()
    pushed = []
    ctrl.connect("streams-changed", lambda *a: pushed.append(a))

    old = threading.Event()
    old.set()
    ctrl._sync_apps(old)                                 # the previous session's poll

    b.set_app_routes.assert_not_called()
    assert pushed == []

    ctrl._sync_apps(threading.Event())                   # the live poll still syncs
    b.set_app_routes.assert_called_once()
    assert len(pushed) == 1


def test_poll_stopped_during_set_app_routes_pushes_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(controller_module, "APP_POLL_S", 999)
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1 = _dev("EP1")
    ctrl.devices = {d1.id: d1}
    ctrl.start_sharing([d1])
    hub = ctrl.active_sink()
    b.list_streams.return_value = [{"id": 42, "name": "App", "sink": hub, "endpoint": hub,
                                    "active": True, "mute": False, "exe": "a.exe"}]
    ctrl._last_streams = None
    pushed = []
    ctrl.connect("streams-changed", lambda *a: pushed.append(a))
    stop = threading.Event()
    b.set_app_routes.side_effect = lambda *a, **k: stop.set()  # stop + restart mid-call

    ctrl._sync_apps(stop)

    b.set_app_routes.assert_called()
    assert pushed == []


def _real_backend(monkeypatch, engine_cls):
    """A real WasapiBackend in hub mode on `engine_cls`, with every Windows call faked."""
    from pipemix.windows import backend as backend_mod

    monkeypatch.setattr(backend_mod, "Engine", engine_cls)
    b = backend_mod.WasapiBackend()
    hub = BackendStatus(BackendHealth.OK, "ok", engine="hub")
    # Nothing here may reach the real Windows default or endpoints.
    b._probe = lambda: hub
    b._find_cable = lambda: ("cable_in", "cable_out")
    b.restore_target = lambda devices=(): "prev"
    b.get_default = lambda: "prev"
    b.list_outputs = lambda: []
    b.list_streams = lambda: []
    b.set_default = lambda sink: None
    b.set_volume = lambda sink, volume: None
    b.set_mute = lambda sink, mute: None
    b.get_volume = lambda sink: 50
    return b


def test_stop_sharing_does_not_wait_for_app_engines_to_exit(tmp_path: Path, monkeypatch) -> None:
    release = threading.Event()
    engines = []

    class _SlowJoin:
        def __init__(self, source_id=None, *, pid=None):
            self.source_id, self.pid = source_id, pid
            self.signalled = self.joined = False
            engines.append(self)

        def start(self):
            pass

        def set_legs(self, ids):
            pass

        def stop(self, wait=True):
            self.signalled = True
            if wait:
                release.wait(1)
                self.joined = True

    b = _real_backend(monkeypatch, _SlowJoin)
    ctrl = _ctrl(tmp_path, backend=b)
    d1 = _dev("EP1")
    ctrl.devices = {d1.id: d1}
    ctrl.start_sharing([d1])
    b.set_app_routes({42: ["EP1"], 43: ["EP1"]}, gen=b.apps_gen)  # as the poll would

    t0 = time.monotonic()
    ctrl.stop_sharing()
    assert time.monotonic() - t0 < 0.5
    assert ctrl.session.state == SessionState.IDLE
    assert len(engines) == 2 and all(e.signalled and not e.joined for e in engines)

    release.set()
    ctrl.stop()                                          # quit path: bounded join of the reaper
    assert all(e.joined for e in engines)


# -- Quitting while the poll is inside a slow Engine.start --

def _quit_during_start(tmp_path: Path, monkeypatch, start_s: float):
    """A hub session whose real poll is inside Engine(pid=42).start(), which returns
    after `start_s`. Returns (ctrl, engines, release)."""
    monkeypatch.setattr(controller_module, "APP_POLL_S", 999)
    monkeypatch.setattr(Controller, "_poll_apps", _REAL_POLL)
    entered, release = threading.Event(), threading.Event()
    engines = []

    class _SlowStart:
        def __init__(self, source_id=None, *, pid=None):
            self.source_id, self.pid = source_id, pid
            self.signalled = self.joined = False
            engines.append(self)

        def start(self):
            entered.set()
            release.wait(start_s)

        def set_legs(self, ids):
            pass

        def stop(self, wait=True):
            self.signalled = True
            self.joined = self.joined or wait

    b = _real_backend(monkeypatch, _SlowStart)
    b.list_streams = lambda: [{"id": 42, "name": "App", "sink": "cable_in", "endpoint": "cable_in",
                               "active": True, "mute": False, "exe": "a.exe"}]
    ctrl = _ctrl(tmp_path, backend=b)
    d1 = _dev("EP1")
    ctrl.devices = {d1.id: d1}
    ctrl.start_sharing([d1])
    assert entered.wait(1)
    return ctrl, engines, release


def test_quit_waits_for_an_in_flight_start_and_closes_its_engine(tmp_path: Path, monkeypatch) -> None:
    ctrl, engines, _release = _quit_during_start(tmp_path, monkeypatch, start_s=0.2)

    t0 = time.monotonic()
    ctrl.stop()

    assert time.monotonic() - t0 < 1
    assert len(engines) == 1 and engines[0].signalled and engines[0].joined
    assert not ctrl._app_poll.is_alive()


def test_quit_gives_up_on_a_hung_start_at_the_budget(tmp_path: Path, monkeypatch, caplog) -> None:
    monkeypatch.setattr(controller_module, "QUIT_S", 0.2)
    ctrl, engines, release = _quit_during_start(tmp_path, monkeypatch, start_s=2)
    try:
        t0 = time.monotonic()
        ctrl.stop()
        assert time.monotonic() - t0 < 0.5
        assert "abandoning 1 app poll" in caplog.text
        assert not engines[0].signalled             # still inside start(): abandoned
    finally:
        release.set()
        ctrl._app_poll.join(1)


def test_quit_joins_a_poll_left_behind_by_a_restart(tmp_path: Path, monkeypatch) -> None:
    ctrl, engines, _release = _quit_during_start(tmp_path, monkeypatch, start_s=0.2)
    old_poll = ctrl._app_poll
    ctrl.stop_sharing()
    ctrl.start_sharing([ctrl.devices["EP1"]])        # new poll; the old one still starting

    ctrl.stop()

    assert not old_poll.is_alive()
    assert engines[0].signalled and engines[0].joined



def test_quit_with_every_output_gone_still_unwinds_the_session(tmp_path: Path) -> None:
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1 = _dev("EP1")
    ctrl.devices = {d1.id: d1}
    ctrl.start_sharing([d1])
    ctrl._on_disconnect(d1.id)                           # no output left: REPAIRING
    assert ctrl.session.state == SessionState.REPAIRING

    ctrl.stop()

    b.destroy_sink.assert_called_once()
    assert ctrl._app_poll_stop.is_set()                  # so the quit never waits out the poll


if __name__ == "__main__":
    import tempfile

    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            with tempfile.TemporaryDirectory() as tmp:
                fn(Path(tmp))
            print(f"  \u2713  {name}")
    print("\nAll tests passed.")
