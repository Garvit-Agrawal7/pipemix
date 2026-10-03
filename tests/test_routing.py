from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from conftest import make_backend, mkdev as _dev
from pipemix.models import DeviceKind, SessionState, VirtualSink
from pipemix.models import BackendError
import pipemix.linux.controller as controller_module
from pipemix.api import Api


# -- One device still plays through the hub, so a toggle never re-routes --

def test_single_device_uses_the_hub(ctrl) -> None:
    dev = _dev("AA:BB:CC:DD:EE:01", sink="alsa_out.usb")
    ctrl.start_sharing([dev])

    assert ctrl.session.state == SessionState.ACTIVE
    assert ctrl.session.sink is not None
    ctrl.backend.set_default.assert_called_with(ctrl.session.sink.name)


# -- Stop sharing destroys the hub and restores default --

def test_stop_destroys_hub_and_restores_default(ctrl) -> None:
    ctrl.backend.get_default.return_value = "original_sink"
    dev = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    ctrl.start_sharing([dev])
    sink = ctrl.session.sink
    ctrl.stop_sharing()

    assert ctrl.session.state == SessionState.IDLE
    ctrl.backend.set_default.assert_called_with("original_sink")
    ctrl.backend.destroy_sink.assert_called_with(sink)


def test_shutdown_hands_streams_back_to_default(ctrl, devs) -> None:
    b = ctrl.backend
    b.get_default.return_value = "sink_default"
    d1, d2 = devs
    ctrl.devices = {d1.id: d1, d2.id: d2}
    ctrl.route_stream(42, [d1.id, d2.id])
    ctrl.route_stream(43, [d1.id])
    hub = ctrl.hubs[42]

    ctrl.stop()

    names = [c[0] for c in b.mock_calls]
    b.move_streams.assert_called_once_with("sink_default")
    assert names.index("move_streams") < names.index("destroy_sink")
    b.destroy_sink.assert_called_with(hub)


def test_stop_sharing_moves_streams_before_hubs_go(ctrl, devs) -> None:
    b = ctrl.backend
    b.get_default.return_value = "original_sink"
    d1, d2 = devs
    ctrl.devices = {d1.id: d1, d2.id: d2}
    ctrl.start_sharing([d1, d2])
    ctrl.route_stream(42, [d1.id, d2.id])
    ctrl.route_stream(43, [d1.id])
    b.reset_mock()

    ctrl.stop_sharing()

    names = [c[0] for c in b.mock_calls]
    b.move_streams.assert_called_once_with("original_sink", exclude=[43])
    assert names.index("move_streams") < names.index("destroy_sink")


# -- Empty device list is a no-op --

def test_start_with_no_devices_is_noop(ctrl) -> None:
    ctrl.start_sharing([])

    assert ctrl.session.state == SessionState.IDLE
    ctrl.backend.create_sink.assert_not_called()


def test_failed_start_tears_down_the_hub(ctrl) -> None:
    """The hub exists before the default switches, so a failure there must not leak it."""
    ctrl.backend.set_default.side_effect = BackendError("no such sink")
    dev = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    with pytest.raises(BackendError):
        ctrl.start_sharing([dev])

    ctrl.backend.destroy_sink.assert_called_once()
    assert ctrl.backend.destroy_sink.call_args.args[0].name.startswith("pipemix_")
    assert ctrl.session.sink is None
    assert ctrl.session.state == SessionState.IDLE


# -- Volume routing --

def test_master_volume_during_session(ctrl, devs) -> None:
    """Two outputs: master rides the hub and each device keeps its own level."""
    d1, d2 = devs
    ctrl.start_sharing([d1, d2])
    ctrl.backend.set_volume.reset_mock()

    ctrl.set_master_volume(80)
    ctrl.backend.set_volume.assert_called_with(ctrl.session.sink.name, 80)
    assert (d1.volume, d2.volume) == (50, 50), "device levels must not follow master"


# -- One output: the master fader and that device's fader are one control --

def test_master_follows_a_lone_device(ctrl) -> None:
    dev = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    ctrl.devices = {dev.id: dev}
    ctrl.start_sharing([dev])

    ctrl.set_master_volume(80)
    assert dev.volume == 80, "the device did not follow master"
    # The level lands on the device; the hub stays out of the way so the two
    # do not multiply.
    ctrl.backend.set_volume.assert_any_call("sink_a", 80)
    hub = ctrl.session.sink.name
    assert (hub, 80) not in [c[0] for c in ctrl.backend.set_volume.call_args_list], \
        "the hub took the level too, so it would be applied twice"


def test_lone_device_drags_master(ctrl) -> None:
    dev = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    ctrl.devices = {dev.id: dev}
    ctrl.start_sharing([dev])

    ctrl.set_device_volume(dev.id, 35)
    assert ctrl.master_volume == 35, "master did not follow the device"


def test_hub_steps_aside_for_one_output(ctrl, devs) -> None:
    """Hub at 100 for one output, at master for two, or the levels multiply."""
    d1, d2 = devs
    ctrl.devices = {d1.id: d1, d2.id: d2}
    d1.volume = 40

    ctrl.start_sharing([d1])
    ctrl.backend.set_volume.assert_any_call(ctrl.session.sink.name, 100)
    assert ctrl.master_volume == 40, "master should adopt the lone device level"

    ctrl.backend.set_volume.reset_mock()
    ctrl.start_sharing([d1, d2])
    ctrl.backend.set_volume.assert_any_call(ctrl.session.sink.name, 40)


# -- The page learns who is in the session from the device push --

def test_sharing_repushes_devices(ctrl) -> None:
    ctrl.emit = MagicMock()
    pushed = lambda: [c.args[0] for c in ctrl.emit.call_args_list].count("devices-changed")

    ctrl.start_sharing([_dev("AA:BB:CC:DD:EE:01", sink="sink_a")])
    assert pushed() == 1
    ctrl.stop_sharing()
    assert pushed() == 2


# -- Stream routing override --

def test_route_stream_records_override(ctrl) -> None:
    d = _dev("AA:BB:CC:DD:EE:01", sink="sink_x")
    ctrl.devices = {d.id: d}
    ctrl.route_stream(42, [d.id])

    assert ctrl.overrides[42] == [d.id]
    ctrl.backend.move_stream.assert_called_with(42, "sink_x")
    ctrl.backend.create_sink.assert_not_called()


def test_route_stream_to_several_gets_its_own_hub(ctrl, devs) -> None:
    d1, d2 = devs
    d3 = _dev("AA:BB:CC:DD:EE:03", sink="sink_c")
    ctrl.devices = {d.id: d for d in (d1, d2, d3)}
    b = ctrl.backend

    ctrl.route_stream(42, [d1.id, d2.id])
    hub = ctrl.hubs[42]
    b.move_stream.assert_called_with(42, hub.name)

    ctrl.route_stream(42, [d1.id, d3.id])     # a toggle moves a leg, not the hub
    assert ctrl.hubs[42] is hub
    b.set_legs.assert_called_with(hub, [d1, d3])
    assert b.create_sink.call_count == 1

    b.get_default.return_value = "sink_default"
    ctrl.route_stream(42, None)               # back to the session
    b.move_stream.assert_called_with(42, "sink_default")
    b.destroy_sink.assert_called_with(hub)
    assert 42 not in ctrl.hubs and 42 not in ctrl.overrides


def test_app_hub_follows_master(ctrl, devs) -> None:
    d1, d2 = devs
    ctrl.devices = {d1.id: d1, d2.id: d2}
    ctrl.start_sharing([d1, d2])
    ctrl.set_master_volume(30)

    ctrl.route_stream(42, [d1.id, d2.id])
    hub = ctrl.hubs[42]
    ctrl.backend.set_volume.assert_called_with(hub.name, 30)   # not left at 100

    ctrl.set_master_volume(70)
    ctrl.backend.set_volume.assert_called_with(hub.name, 70)


def test_app_hub_follows_disconnects_and_ends_with_its_stream(ctrl, devs) -> None:
    d1, d2 = devs
    ctrl.devices = {d1.id: d1, d2.id: d2}
    ctrl.route_stream(42, [d1.id, d2.id])
    hub = ctrl.hubs[42]

    ctrl._on_disconnect(d2.id)
    ctrl.backend.set_legs.assert_called_with(hub, [d1])
    assert ctrl.overrides[42] == [d1.id, d2.id]   # d2 is let back in on reconnect

    ctrl.backend.list_streams.return_value = [{"id": 7, "name": "x", "sink": "s", "mute": False}]
    assert ctrl.streams() == [{"id": 7, "name": "x", "sink": "s", "mute": False, "devices": None}]
    ctrl.backend.destroy_sink.assert_called_with(hub)
    assert not ctrl.hubs and not ctrl.overrides


# -- Orphan cleanup --

def test_orphan_cleanup_on_start(make_ctrl) -> None:
    b = make_backend()
    orphan = VirtualSink(123, "pipemix_deadbeef")
    b.find_orphans.return_value = [orphan]
    make_ctrl(b)

    b.destroy_sink.assert_called_with(orphan)


# -- Refresh --

def test_refresh_keeps_session(ctrl) -> None:
    dev = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    ctrl.start_sharing([dev])
    sink = ctrl.session.sink
    ctrl.backend.destroy_sink.reset_mock()

    ctrl.refresh()

    ctrl.backend.destroy_sink.assert_not_called()
    assert ctrl.session.state == SessionState.ACTIVE
    assert ctrl.session.sink is sink
    assert ctrl.targets == {dev.id}


# -- A toggle moves a leg; it must never tear the hub down --

def test_toggle_keeps_the_hub(ctrl, devs) -> None:
    d1, d2 = devs
    ctrl.start_sharing([d1])
    hub = ctrl.session.sink
    ctrl.backend.create_sink.reset_mock()

    ctrl.start_sharing([d1, d2])            # stage the second output

    assert ctrl.session.sink is hub
    ctrl.backend.create_sink.assert_not_called()
    ctrl.backend.destroy_sink.assert_not_called()
    ctrl.backend.set_legs.assert_called_with(hub, [d1, d2])


def test_disconnect_drops_one_leg(ctrl, devs) -> None:
    """A dropout used to rebuild the whole session; now it is one leg."""
    d1, d2 = devs
    ctrl.devices = {d1.id: d1, d2.id: d2}
    ctrl.start_sharing([d1, d2])
    hub = ctrl.session.sink

    ctrl._on_disconnect(d2.id)

    assert ctrl.session.sink is hub
    ctrl.backend.destroy_sink.assert_not_called()
    ctrl.backend.set_legs.assert_called_with(hub, [d1])
    assert ctrl.session.state == SessionState.ACTIVE


def test_wired_output_drops_and_restores_its_leg(ctrl) -> None:
    """No BlueZ event for a wired output, so the hotplug check drops and restores its leg."""
    u1 = _dev("alsa_usb", sink="alsa_usb", kind=DeviceKind.USB)
    u2 = _dev("alsa_hdmi", sink="alsa_hdmi", kind=DeviceKind.HDMI)
    u2.volume = 30
    ctrl.devices = {u1.id: u1, u2.id: u2}
    ctrl.start_sharing([u1, u2])
    hub = ctrl.session.sink
    ctrl.backend.list_outputs.return_value = [u1]
    ctrl.monitor.connected.reset_mock()

    ctrl._hotplug()

    ctrl.monitor.connected.assert_called()  # a changed wired set refreshes
    ctrl.backend.destroy_sink.assert_not_called()
    ctrl.backend.set_legs.assert_called_with(hub, [u1])
    assert ctrl.session.devices == [u1]
    assert ctrl.session.state == SessionState.ACTIVE
    # Still listed, offline, like a dropped Bluetooth device.
    assert not ctrl.devices[u2.id].connected and u2.id in ctrl.targets

    # The offline entry must not look like a fresh unplug on every event.
    ctrl.monitor.connected.reset_mock()
    ctrl._hotplug()
    ctrl.monitor.connected.assert_not_called()

    # Plugged back in: it comes back as a new object, at its own level.
    ctrl.backend.list_outputs.return_value = [u1, _dev(u2.id, sink="alsa_hdmi", kind=DeviceKind.HDMI)]

    ctrl._hotplug()

    ctrl.backend.destroy_sink.assert_not_called()
    assert {d.id for d in ctrl.session.devices} == {u1.id, u2.id}
    assert set(ctrl.backend.set_legs.call_args.args[1]) == {u1, u2}
    assert ctrl.devices[u2.id].connected and ctrl.devices[u2.id].volume == 30


def test_lone_wired_output_waits_to_reconnect(ctrl) -> None:
    """Pulled out with nothing else playing: repairing, which the page shows as reconnecting."""
    u = _dev("alsa_usb", sink="alsa_usb", kind=DeviceKind.USB)
    ctrl.devices = {u.id: u}
    ctrl.start_sharing([u])
    hub = ctrl.session.sink
    ctrl.backend.list_outputs.return_value = []

    ctrl._hotplug()

    assert ctrl.session.state == SessionState.REPAIRING
    assert ctrl.session.sink is hub
    assert not ctrl.devices[u.id].connected and u.id in ctrl.targets

    ctrl.backend.list_outputs.return_value = [_dev(u.id, sink="alsa_usb", kind=DeviceKind.USB)]

    ctrl._hotplug()

    assert ctrl.session.state == SessionState.ACTIVE
    assert [d.id for d in ctrl.session.devices] == [u.id]


# -- _mark coalesces a burst of pactl events into one _pw_changed --

def test_mark_coalesces_a_burst(ctrl, monkeypatch) -> None:
    fake_glib = MagicMock()
    monkeypatch.setattr(controller_module, "GLib", fake_glib)

    ctrl._mark("streams")
    ctrl._mark("sinks")

    fake_glib.timeout_add.assert_called_once()
    assert ctrl._pending == {"streams", "sinks"}


def test_output_is_ticked_again_when_it_rejoins(ctrl) -> None:
    """Unplugging unticks it; the session taking it back on replug must tick it again."""
    api = Api(ctrl)
    u = _dev("alsa_usb", sink="alsa_usb", kind=DeviceKind.USB)
    ctrl.devices = {u.id: u}
    api.toggle_device(u.id, True)
    api.start_sharing()
    ctrl.backend.list_outputs.return_value = []

    ctrl._hotplug()

    assert not api._devices_payload()[0]["selected"]

    ctrl.backend.list_outputs.return_value = [_dev(u.id, sink="alsa_usb", kind=DeviceKind.USB)]

    ctrl._hotplug()

    assert api._devices_payload()[0]["selected"]


# -- Toggling in a 4th output must not re-touch the three already joined --

def test_toggle_fourth_output_sends_minimal_pactl(ctrl, devs) -> None:
    d1, d2 = devs
    d3 = _dev("AA:BB:CC:DD:EE:03", sink="sink_c")
    d4 = _dev("AA:BB:CC:DD:EE:04", sink="sink_d")
    ctrl.start_sharing([d1, d2, d3])
    hub = ctrl.session.sink.name
    ctrl.backend.set_mute.reset_mock()
    ctrl.backend.set_volume.reset_mock()

    ctrl.start_sharing([d1, d2, d3, d4])

    ctrl.backend.set_mute.assert_called_once_with(d4.sink, False)
    assert ctrl.backend.set_volume.call_args_list == [call(d4.sink, d4.volume), call(hub, 50)]


# -- A drag sends only the latest tick --

def test_volume_ticks_latest_wins(ctrl) -> None:
    dev = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    ctrl.devices[dev.id] = dev
    jobs = []
    ctrl._bg = lambda fn, *a: jobs.append((fn, a))

    ctrl.set_device_volume(dev.id, 10)
    ctrl.set_device_volume(dev.id, 20)
    ctrl.set_device_volume(dev.id, 30)
    assert len(jobs) == 1

    for fn, args in jobs:
        fn(*args)

    ctrl.backend.set_mute.assert_called_once_with(dev.sink, False)
    ctrl.backend.set_volume.assert_called_once_with(dev.sink, 30)


# -- A stale queued tick must not undo a routing change that landed after it --

def test_stale_tick_cannot_undo_routing(ctrl, devs) -> None:
    d1, d2 = devs
    ctrl.devices = {d1.id: d1, d2.id: d2}
    ctrl.start_sharing([d1, d2])
    hub = ctrl.session.sink.name
    jobs = []
    ctrl._bg = lambda fn, *a: jobs.append((fn, a))

    ctrl.set_master_volume(30)   # queues a hub tick, not sent yet
    ctrl.start_sharing([d1])     # 2 -> 1: hub must land on 100

    for fn, args in jobs:
        fn(*args)

    hub_calls = [c.args for c in ctrl.backend.set_volume.call_args_list if c.args[0] == hub]
    assert hub_calls[-1] == (hub, 100)
