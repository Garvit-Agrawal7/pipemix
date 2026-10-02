"""Tests for the macOS Controller — session lifecycle, hotplug, crash recovery.

The backend is a MagicMock and the device monitor is injected, so nothing
here touches Core Audio and the suite runs on any platform.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipemix.models import AudioDevice, DeviceKind, SessionState, VirtualSink
from pipemix.linux.services.backend import BackendError, BackendHealth, BackendStatus
from pipemix.linux.services.config.config_manager import ConfigManager
from pipemix.macos.controller import Controller

HUB = "com.pipemix.hub"


def _dev(dev_id: str, connected: bool = True, kind=DeviceKind.BLUETOOTH) -> AudioDevice:
    return AudioDevice(id=dev_id, name=dev_id, sink=dev_id if connected else None,
                       kind=kind, connected=connected)


def _backend(outputs: list[AudioDevice] | None = None) -> MagicMock:
    b = MagicMock()
    b.health.return_value = BackendStatus(BackendHealth.OK, "ok", engine="aggregate")
    b.find_orphans.return_value = []
    # Fresh objects per call, like the real backend: the Controller mutates
    # the ones it holds when a device drops out.
    ids = [d.id for d in outputs or []]
    b.list_outputs.side_effect = lambda: [_dev(i) for i in ids]
    b.list_streams.return_value = []
    b.get_default.return_value = "speakers"
    b.restore_target.return_value = "speakers"
    b.get_volume.return_value = 70
    b.create_sink.side_effect = lambda devs: VirtualSink(1, HUB, {d.id: 0 for d in devs})
    return b


def _ctrl(tmp_path: Path, backend=None) -> Controller:
    c = Controller(backend or _backend(), ConfigManager(tmp_path / "config.json"),
                   monitor=MagicMock())
    c.start()
    return c


def _share(ctrl: Controller, *ids: str) -> list[AudioDevice]:
    devs = [_dev(i) for i in ids]
    ctrl.devices = {d.id: d for d in devs}
    ctrl.start_sharing(devs)
    return devs


# -- Lifecycle --

def test_start_makes_the_hub_the_default_and_remembers_the_old_one(tmp_path: Path) -> None:
    b = _backend()
    ctrl = _ctrl(tmp_path, b)
    _share(ctrl, "a", "b")

    assert ctrl.session.state == SessionState.ACTIVE
    b.set_default.assert_called_with(HUB)
    assert ctrl.config.data["prev_default"] == "speakers"
    assert ctrl.targets == {"a", "b"}


def test_retarget_moves_legs_without_a_new_hub(tmp_path: Path) -> None:
    b = _backend()
    ctrl = _ctrl(tmp_path, b)
    a, bb = _share(ctrl, "a", "b")
    c = _dev("c")
    ctrl.devices["c"] = c

    ctrl.start_sharing([a, c])

    b.create_sink.assert_called_once()
    b.destroy_sink.assert_not_called()
    legs = b.set_legs.call_args.args[1]
    assert [d.id for d in legs] == ["a", "c"]
    assert ctrl.targets == {"a", "c"}


def test_stop_restores_default_and_destroys_hub(tmp_path: Path) -> None:
    b = _backend()
    ctrl = _ctrl(tmp_path, b)
    _share(ctrl, "a", "b")

    ctrl.stop_sharing()

    b.set_default.assert_called_with("speakers")
    b.destroy_sink.assert_called_once()
    assert ctrl.session.sink is None
    assert ctrl.session.state == SessionState.IDLE
    assert ctrl.config.data["prev_default"] is None


def test_failed_start_unwinds(tmp_path: Path) -> None:
    b = _backend()
    b.create_sink.side_effect = BackendError("nope")
    ctrl = _ctrl(tmp_path, b)
    try:
        _share(ctrl, "a")
    except BackendError:
        pass
    assert ctrl.session.sink is None
    assert ctrl.session.state == SessionState.IDLE


def test_quit_after_every_output_dropped_still_cleans_up(tmp_path: Path) -> None:
    b = _backend()
    ctrl = _ctrl(tmp_path, b)
    _share(ctrl, "a")
    ctrl._on_disconnect("a")
    assert ctrl.session.state == SessionState.REPAIRING

    ctrl.stop()

    b.destroy_sink.assert_called_once()
    b.set_default.assert_called_with("speakers")


# -- Volume --

def test_two_outputs_put_master_on_the_hub(tmp_path: Path) -> None:
    b = _backend()
    ctrl = _ctrl(tmp_path, b)
    ctrl.master_volume = 40
    _share(ctrl, "a", "b")
    b.set_volume.assert_any_call(HUB, 40)

    ctrl.set_master_volume(65)
    b.set_volume.assert_called_with(HUB, 65)


def test_lone_output_carries_the_level_itself(tmp_path: Path) -> None:
    b = _backend()
    ctrl = _ctrl(tmp_path, b)
    (a,) = _share(ctrl, "a")
    b.set_volume.assert_any_call(HUB, 100)

    ctrl.set_master_volume(30)
    b.set_volume.assert_called_with("a", 30)
    assert a.volume == 30


# -- Hotplug --

def test_disconnect_drops_one_leg_and_keeps_playing(tmp_path: Path) -> None:
    b = _backend()
    ctrl = _ctrl(tmp_path, b)
    _share(ctrl, "a", "b")

    ctrl._on_disconnect("b")

    assert ctrl.session.state == SessionState.ACTIVE
    assert [d.id for d in b.set_legs.call_args.args[1]] == ["a"]
    assert ctrl.targets == {"a", "b"}       # still wanted back
    b.destroy_sink.assert_not_called()


def test_reconnect_feeds_the_device_again(tmp_path: Path) -> None:
    a, bb = _dev("a"), _dev("b")
    b = _backend([a, bb])
    ctrl = _ctrl(tmp_path, b)
    ctrl.start_sharing([ctrl.devices["a"], ctrl.devices["b"]])

    b.list_outputs.side_effect = lambda: [_dev("a")]
    ctrl._on_disconnect("b")
    ctrl.refresh()
    assert not ctrl.devices["b"].connected   # kept, shown as dropped out

    b.list_outputs.side_effect = lambda: [_dev("a"), _dev("b")]
    b.get_default.return_value = HUB
    ctrl._on_connect("b")

    assert sorted(d.id for d in b.set_legs.call_args.args[1]) == ["a", "b"]
    assert ctrl.session.state == SessionState.ACTIVE


def test_reconnect_after_total_dropout_takes_the_default_back(tmp_path: Path) -> None:
    b = _backend([_dev("a")])
    ctrl = _ctrl(tmp_path, b)
    ctrl.start_sharing([ctrl.devices["a"]])
    ctrl._on_disconnect("a")
    b.get_default.return_value = "speakers"   # macOS moved off the empty hub
    b.set_default.reset_mock()

    ctrl._on_connect("a")

    b.set_default.assert_called_once_with(HUB)


def test_unrelated_disconnect_leaves_session_alone(tmp_path: Path) -> None:
    b = _backend()
    ctrl = _ctrl(tmp_path, b)
    _share(ctrl, "a", "b")
    ctrl.devices["x"] = _dev("x")
    b.set_legs.reset_mock()

    ctrl._on_disconnect("x")

    b.set_legs.assert_not_called()
    assert ctrl.session.state == SessionState.ACTIVE


# -- Crash recovery --

def test_startup_removes_a_leftover_hub_and_restores_default(tmp_path: Path) -> None:
    b = _backend([_dev("speakers", kind=DeviceKind.BUILTIN)])
    orphan = VirtualSink(9, HUB)
    b.find_orphans.return_value = [orphan]
    b.get_default.return_value = HUB
    cfg = ConfigManager(tmp_path / "config.json")
    cfg.data["prev_default"] = "speakers"
    cfg.save()

    ctrl = Controller(b, ConfigManager(tmp_path / "config.json"), monitor=MagicMock())
    ctrl.start()

    b.set_default.assert_any_call("speakers")
    b.destroy_sink.assert_called_once_with(orphan)
    assert ctrl.config.data["prev_default"] is None


def test_no_per_app_routing(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    assert ctrl.streams() == []
    try:
        ctrl.route_stream(1, ["a"])
        raise AssertionError("route_stream should refuse on macOS")
    except BackendError:
        pass


# -- Master level --

def test_master_starts_at_100_and_is_remembered(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    assert ctrl.master_volume == 100
    ctrl.set_master_volume(35)
    ctrl.stop()

    again = Controller(_backend(), ConfigManager(tmp_path / "config.json"), monitor=MagicMock())
    assert again.master_volume == 35


# -- Volume keys --

def test_volume_keys_only_act_while_sharing(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    assert ctrl.volume_key("up", 100 / 16) is False


def test_volume_keys_move_master_on_the_sixteenths_grid(tmp_path: Path) -> None:
    b = _backend()
    ctrl = _ctrl(tmp_path, b)
    pushed = []
    ctrl.connect("master-changed", lambda _c, v: pushed.append(v))
    _share(ctrl, "a", "b")
    ctrl.set_master_volume(50)

    assert ctrl.volume_key("up", 100 / 16)
    assert ctrl.master_volume == 56
    ctrl.volume_key("down", 100 / 16)
    assert ctrl.master_volume == 50
    b.set_volume.assert_called_with(HUB, 50)
    assert pushed == [56, 50]


def test_mute_key_toggles_back_to_the_old_level(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    _share(ctrl, "a", "b")
    ctrl.set_master_volume(60)
    ctrl.volume_key("mute", 0)
    assert ctrl.master_volume == 0
    ctrl.volume_key("mute", 0)
    assert ctrl.master_volume == 60


# -- Per-app routing --

def test_route_needs_a_session(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    try:
        ctrl.route_stream(1, ["a"])
        raise AssertionError("route_stream should refuse while idle")
    except BackendError:
        pass


def test_route_to_some_outputs_then_back_to_all(tmp_path: Path) -> None:
    b = _backend()
    ctrl = _ctrl(tmp_path, b)
    _share(ctrl, "a", "b")

    ctrl.route_stream(42, ["b", "nope"])
    b.set_app_routes.assert_called_with({42: ["b"]})

    ctrl.route_stream(42, ["a", "b"])        # every output = following the session
    b.set_app_routes.assert_called_with({})
    assert ctrl.overrides == {}


def test_a_dropped_output_leaves_the_apps_route(tmp_path: Path) -> None:
    b = _backend()
    b.list_streams.return_value = [{"id": 1, "name": "x"}, {"id": 2, "name": "y"}]
    ctrl = _ctrl(tmp_path, b)
    _share(ctrl, "a", "b", "c")
    ctrl.route_stream(1, ["b", "c"])
    ctrl.route_stream(2, ["b"])

    ctrl._on_disconnect("b")

    assert ctrl.overrides == {1: ["c"]}      # 2 had only b: it follows again
    b.set_app_routes.assert_called_with({1: ["c"]})


def test_stop_sharing_unroutes_every_app(tmp_path: Path) -> None:
    b = _backend()
    ctrl = _ctrl(tmp_path, b)
    _share(ctrl, "a", "b")
    ctrl.route_stream(1, ["a"])

    ctrl.stop_sharing()

    assert ctrl.overrides == {}
    b.set_app_routes.assert_called_with({})


def test_streams_carry_route_and_problem(tmp_path: Path) -> None:
    b = _backend()
    b.list_streams.return_value = [
        {"id": 1, "name": "Music", "sink": HUB, "endpoint": HUB, "active": True, "mute": False},
        {"id": 2, "name": "Safari", "sink": HUB, "endpoint": HUB, "active": True, "mute": False},
    ]
    b.route_problem.return_value = "waiting for you to allow System Audio Recording for PipeMix"
    ctrl = _ctrl(tmp_path, b)
    _share(ctrl, "a", "b")
    ctrl.route_stream(1, ["a"])

    music, safari = ctrl.streams()

    assert music["devices"] == ["a"] and music["stuck"] and "allow" in music["hint"]
    assert safari["devices"] is None and not safari["stuck"]
