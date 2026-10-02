from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipemix.models import AudioDevice, DeviceKind, SessionState, VirtualSink
from pipemix.linux.services.backend import BackendError, BackendHealth, BackendStatus
from pipemix.linux.services.config.config_manager import ConfigManager
from pipemix.windows import controller as controller_module
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
    b.restore_target.return_value = "prev_default"
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
    # Leader mode, not hub: under contract D a hub-mode route_stream with an
    # active session no longer pins at all (it fans out via overrides), so
    # this pin-lifecycle test moves to the mode that still pins.
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


# -- Contract D: per-app capture in hub mode (PER-APP-ROUTING.md Phase 1) --
#
# In hub mode a session no longer pins apps at all: route_stream just records
# which device ids an app wants (`overrides`), and `_sync_apps` reconciles
# that against `backend.set_app_routes({pid: [device ids]})` on its own —
# directly (tests below call it), or off the 1 s poll thread while a hub
# session is active. Leader mode is untouched and keeps the pin path.

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

    # Still playing into the hub, which is exactly right in hub mode — the
    # override is a capture-side routing choice, not where the app must play.
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
    # A pin left behind by a previous idle/leader-mode run, keyed by exe —
    # `streams()` would normally learn `_exe` itself, but this test only
    # needs the leftover-pin-clearing branch, so it seeds it directly.
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

    ctrl._on_disconnect(d1.id)

    assert ctrl.overrides[42] == [d2.id]
    assert 43 not in ctrl.overrides                      # emptied out entirely
    b.set_app_routes.assert_called()                     # re-synced after the drop


def test_retarget_resyncs_apps(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(controller_module, "APP_POLL_S", 999)
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1, d2 = _dev("EP1"), _dev("EP2")
    ctrl.devices = {d.id: d for d in (d1, d2)}
    ctrl.start_sharing([d1])
    b.set_app_routes.reset_mock()

    ctrl.start_sharing([d1, d2])                         # session already up -> _retarget

    b.set_app_routes.assert_called()


def test_start_sharing_hub_starts_the_poll_thread(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(controller_module, "APP_POLL_S", 999)
    b = _backend(engine="hub")
    ctrl = _ctrl(tmp_path, backend=b)
    d1 = _dev("EP1")
    ctrl.devices = {d1.id: d1}

    ctrl.start_sharing([d1])

    assert ctrl._app_poll is not None and ctrl._app_poll.is_alive()

    ctrl.stop_sharing()

    assert ctrl._app_poll_stop is not None and ctrl._app_poll_stop.is_set()


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


if __name__ == "__main__":
    import tempfile

    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            with tempfile.TemporaryDirectory() as tmp:
                fn(Path(tmp))
            print(f"  \u2713  {name}")
    print("\nAll tests passed.")
