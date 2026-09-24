"""Tests for audio-routing logic in the Controller.

The Controller owns all routing decisions; the UI is dumb. We mock the
PactlBackend and the GLib main loop so these run headless, no PipeWire needed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# GObject / GLib are C extensions that may not be installed in a CI runner.
# Stub them just enough for the Controller module to import.
_gi_mock = MagicMock()


class _FakeGObject:
    Object = type("Object", (), {
        "__init__": lambda self, *a, **kw: None,
        "emit": lambda self, *a, **kw: None,
    })
    SignalFlags = type("SignalFlags", (), {"RUN_FIRST": 0})()


class _FakeGLib:
    SOURCE_REMOVE = False

    @staticmethod
    def timeout_add(*a, **kw):
        pass


_gi_mock.repository.GObject = _FakeGObject
_gi_mock.repository.GLib = _FakeGLib

sys.modules.setdefault("gi", _gi_mock)
sys.modules.setdefault("gi.repository", _gi_mock.repository)

from pipemix.linux.models import AudioDevice, DeviceKind, SessionState, VirtualSink
from pipemix.linux.services.backend import BackendError, BackendHealth, BackendStatus
from pipemix.linux.services.config.config_manager import ConfigManager
import pipemix.linux.controller as controller_module
from pipemix.linux.controller import Controller
from pipemix.linux.ui.api import Api


def _dev(mac: str, name: str = "Dev", sink: str | None = None, kind=DeviceKind.BLUETOOTH) -> AudioDevice:
    return AudioDevice(id=mac, name=name, sink=sink, kind=kind, connected=sink is not None)


def _backend() -> MagicMock:
    b = MagicMock()
    b.health.return_value = BackendStatus(BackendHealth.OK, "ok")
    b.find_orphans.return_value = []
    b.list_outputs.return_value = []
    b.resolve_bt_sinks.return_value = {}
    b.list_streams.return_value = []
    b.get_volume.return_value = 50
    b.create_sink.side_effect = _fake_create
    return b


def _fake_create(devices) -> VirtualSink:
    """Mirrors PactlBackend.create_sink: a hub is useless with nothing to feed."""
    if not any(d.sink for d in devices):
        raise BackendError("No resolvable sink names.")
    return VirtualSink(999, VirtualSink.make_name(), {d.sink: 1 for d in devices if d.sink})


def _ctrl(tmp_path: Path, backend=None) -> Controller:
    b = backend or _backend()
    cfg = ConfigManager(tmp_path / "config.json")
    c = Controller(b, cfg)
    # Bypass BlueZ D-Bus monitor — it needs a real system bus.
    c.monitor = MagicMock()
    c.monitor.connected.return_value = []
    c.start()
    return c


# -- One device still plays through the hub, so a toggle never re-routes --

def test_single_device_uses_the_hub(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    dev = _dev("AA:BB:CC:DD:EE:01", sink="alsa_out.usb")
    ctrl.start_sharing([dev])

    assert ctrl.session.state == SessionState.ACTIVE
    assert ctrl.session.sink is not None
    ctrl.backend.set_default.assert_called_with(ctrl.session.sink.name)


# -- Multi-device sharing (virtual sink) --

def test_multi_device_creates_virtual_sink(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    d1 = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    d2 = _dev("AA:BB:CC:DD:EE:02", sink="sink_b")
    ctrl.start_sharing([d1, d2])

    assert ctrl.session.state == SessionState.ACTIVE
    assert ctrl.session.sink is not None
    ctrl.backend.create_sink.assert_called_once()


# -- Stop sharing restores default --

def test_stop_restores_default(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    ctrl.backend.get_default.return_value = "original_sink"
    dev = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    ctrl.start_sharing([dev])
    ctrl.stop_sharing()

    assert ctrl.session.state == SessionState.IDLE
    ctrl.backend.set_default.assert_called_with("original_sink")


# -- Stop destroys the virtual sink --

def test_stop_destroys_virtual_sink(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    d1 = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    d2 = _dev("AA:BB:CC:DD:EE:02", sink="sink_b")
    ctrl.start_sharing([d1, d2])

    sink = ctrl.session.sink
    ctrl.stop_sharing()

    ctrl.backend.destroy_sink.assert_called_with(sink)


def test_shutdown_hands_streams_back_to_default(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    b = ctrl.backend
    b.get_default.return_value = "sink_default"
    d1 = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    d2 = _dev("AA:BB:CC:DD:EE:02", sink="sink_b")
    ctrl.devices = {d1.id: d1, d2.id: d2}
    ctrl.route_stream(42, [d1.id, d2.id])
    ctrl.route_stream(43, [d1.id])
    hub = ctrl.hubs[42]

    ctrl.stop()

    names = [c[0] for c in b.mock_calls]
    b.move_streams.assert_called_once_with("sink_default")
    assert names.index("move_streams") < names.index("destroy_sink")
    b.destroy_sink.assert_called_with(hub)


# -- Empty device list is a no-op --

def test_start_with_no_devices_is_noop(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    ctrl.start_sharing([])

    assert ctrl.session.state == SessionState.IDLE
    ctrl.backend.create_sink.assert_not_called()


# -- Device without sink raises --

def test_single_device_without_sink_raises(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    dev = _dev("AA:BB:CC:DD:EE:01", sink=None)
    try:
        ctrl.start_sharing([dev])
        assert False, "Should have raised"
    except BackendError:
        pass
    assert ctrl.session.state == SessionState.IDLE


# -- Volume routing --

def test_device_volume_forwarded(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    dev = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    ctrl.devices[dev.id] = dev
    ctrl.set_device_volume(dev.id, 75)

    ctrl.backend.set_volume.assert_called_with("sink_a", 75)


def test_master_volume_during_session(tmp_path: Path) -> None:
    """Two outputs: master rides the hub and each device keeps its own level."""
    ctrl = _ctrl(tmp_path)
    d1 = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    d2 = _dev("AA:BB:CC:DD:EE:02", sink="sink_b")
    ctrl.start_sharing([d1, d2])
    ctrl.backend.set_volume.reset_mock()

    ctrl.set_master_volume(80)
    ctrl.backend.set_volume.assert_called_with(ctrl.session.sink.name, 80)
    assert (d1.volume, d2.volume) == (50, 50), "device levels must not follow master"


# -- One output: the master fader and that device's fader are one control --

def test_master_follows_a_lone_device(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
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


def test_lone_device_drags_master(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    dev = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    ctrl.devices = {dev.id: dev}
    ctrl.start_sharing([dev])

    ctrl.set_device_volume(dev.id, 35)
    assert ctrl.master_volume == 35, "master did not follow the device"


def test_hub_steps_aside_for_one_output(tmp_path: Path) -> None:
    """Hub at 100 for one output, at master for two, or the levels multiply."""
    ctrl = _ctrl(tmp_path)
    d1 = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    d2 = _dev("AA:BB:CC:DD:EE:02", sink="sink_b")
    ctrl.devices = {d1.id: d1, d2.id: d2}
    d1.volume = 40

    ctrl.start_sharing([d1])
    ctrl.backend.set_volume.assert_any_call(ctrl.session.sink.name, 100)
    assert ctrl.master_volume == 40, "master should adopt the lone device level"

    ctrl.backend.set_volume.reset_mock()
    ctrl.start_sharing([d1, d2])
    ctrl.backend.set_volume.assert_any_call(ctrl.session.sink.name, 40)


# -- The page learns who is in the session from the device push --

def test_sharing_repushes_devices(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    ctrl.emit = MagicMock()
    pushed = lambda: [c.args[0] for c in ctrl.emit.call_args_list].count("devices-changed")

    ctrl.start_sharing([_dev("AA:BB:CC:DD:EE:01", sink="sink_a")])
    assert pushed() == 1
    ctrl.stop_sharing()
    assert pushed() == 2


# -- Stream routing override --

def test_route_stream_records_override(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    d = _dev("AA:BB:CC:DD:EE:01", sink="sink_x")
    ctrl.devices = {d.id: d}
    ctrl.route_stream(42, [d.id])

    assert ctrl.overrides[42] == [d.id]
    ctrl.backend.move_stream.assert_called_with(42, "sink_x")
    ctrl.backend.create_sink.assert_not_called()


def test_route_stream_to_several_gets_its_own_hub(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    d1 = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    d2 = _dev("AA:BB:CC:DD:EE:02", sink="sink_b")
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


def test_app_hub_follows_master(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    d1 = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    d2 = _dev("AA:BB:CC:DD:EE:02", sink="sink_b")
    ctrl.devices = {d1.id: d1, d2.id: d2}
    ctrl.start_sharing([d1, d2])
    ctrl.set_master_volume(30)

    ctrl.route_stream(42, [d1.id, d2.id])
    hub = ctrl.hubs[42]
    ctrl.backend.set_volume.assert_called_with(hub.name, 30)   # not left at 100

    ctrl.set_master_volume(70)
    ctrl.backend.set_volume.assert_called_with(hub.name, 70)


def test_app_hub_follows_disconnects_and_ends_with_its_stream(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    d1 = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    d2 = _dev("AA:BB:CC:DD:EE:02", sink="sink_b")
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

def test_orphan_cleanup_on_start(tmp_path: Path) -> None:
    b = _backend()
    orphan = VirtualSink(123, "pipemix_deadbeef")
    b.find_orphans.return_value = [orphan]
    ctrl = _ctrl(tmp_path, backend=b)

    b.destroy_sink.assert_called_with(orphan)


# -- Preset save / apply round-trip --

def test_preset_save_and_apply(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    pid = ctrl.save_preset("Movie Mode", ["AA:BB", "CC:DD"])

    assert pid in ctrl.presets
    assert ctrl.presets[pid]["devices"] == ["AA:BB", "CC:DD"]


# -- Bluetooth disconnect mid-session rebuilds on remaining --

def test_bt_disconnect_rebuilds(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    d1 = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    d2 = _dev("AA:BB:CC:DD:EE:02", sink="sink_b")
    ctrl.devices = {d1.id: d1, d2.id: d2}
    ctrl.start_sharing([d1, d2])
    ctrl.backend.create_sink.reset_mock()

    ctrl._on_disconnect("AA:BB:CC:DD:EE:01")

    assert ctrl.session.state in (SessionState.ACTIVE, SessionState.REPAIRING)


# -- Refresh --

def test_refresh_keeps_session(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    dev = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    ctrl.start_sharing([dev])
    sink = ctrl.session.sink
    ctrl.backend.destroy_sink.reset_mock()

    ctrl.refresh()

    ctrl.backend.destroy_sink.assert_not_called()
    assert ctrl.session.state == SessionState.ACTIVE
    assert ctrl.session.sink is sink
    assert ctrl.targets == {dev.id}


# -- Hotplug: react to a wired output appearing, ignore Bluetooth churn --

def test_hotplug_ignores_unchanged_wired_set(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    ctrl.monitor.connected.reset_mock()

    ctrl._hotplug()  # list_outputs() still returns [], same as at start()

    ctrl.monitor.connected.assert_not_called()  # refresh() never ran


def test_hotplug_refreshes_on_a_new_wired_output(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    usb = _dev("usb1", sink="alsa_usb", kind=DeviceKind.USB)
    ctrl.backend.list_outputs.return_value = [usb]
    ctrl.monitor.connected.reset_mock()

    ctrl._hotplug()

    ctrl.monitor.connected.assert_called_once()  # refresh() ran
    assert usb.id in ctrl.devices


if __name__ == "__main__":
    import tempfile

    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            with tempfile.TemporaryDirectory() as tmp:
                fn(Path(tmp))
            print(f"  ✓  {name}")
    print("\nAll tests passed.")


# -- A toggle moves a leg; it must never tear the hub down --

def test_toggle_keeps_the_hub(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    d1 = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    d2 = _dev("AA:BB:CC:DD:EE:02", sink="sink_b")
    ctrl.start_sharing([d1])
    hub = ctrl.session.sink
    ctrl.backend.create_sink.reset_mock()

    ctrl.start_sharing([d1, d2])            # stage the second output

    assert ctrl.session.sink is hub
    ctrl.backend.create_sink.assert_not_called()
    ctrl.backend.destroy_sink.assert_not_called()
    ctrl.backend.set_legs.assert_called_with(hub, [d1, d2])


def test_disconnect_drops_one_leg(tmp_path: Path) -> None:
    """A dropout used to rebuild the whole session; now it is one leg."""
    ctrl = _ctrl(tmp_path)
    d1 = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    d2 = _dev("AA:BB:CC:DD:EE:02", sink="sink_b")
    ctrl.devices = {d1.id: d1, d2.id: d2}
    ctrl.start_sharing([d1, d2])
    hub = ctrl.session.sink

    ctrl._on_disconnect(d2.id)

    assert ctrl.session.sink is hub
    ctrl.backend.destroy_sink.assert_not_called()
    ctrl.backend.set_legs.assert_called_with(hub, [d1])
    assert ctrl.session.state == SessionState.ACTIVE


def test_wired_output_drops_and_restores_its_leg(tmp_path: Path) -> None:
    """No BlueZ event for a wired output, so the hotplug check drops and restores its leg."""
    ctrl = _ctrl(tmp_path)
    u1 = _dev("alsa_usb", sink="alsa_usb", kind=DeviceKind.USB)
    u2 = _dev("alsa_hdmi", sink="alsa_hdmi", kind=DeviceKind.HDMI)
    u2.volume = 30
    ctrl.devices = {u1.id: u1, u2.id: u2}
    ctrl.start_sharing([u1, u2])
    hub = ctrl.session.sink
    ctrl.backend.list_outputs.return_value = [u1]

    ctrl._hotplug()

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


def test_lone_wired_output_waits_to_reconnect(tmp_path: Path) -> None:
    """Pulled out with nothing else playing: repairing, which the page shows as reconnecting."""
    ctrl = _ctrl(tmp_path)
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

def test_mark_coalesces_a_burst(tmp_path: Path, monkeypatch) -> None:
    ctrl = _ctrl(tmp_path)
    fake_glib = MagicMock()
    monkeypatch.setattr(controller_module, "GLib", fake_glib)

    ctrl._mark("streams")
    ctrl._mark("sinks")

    fake_glib.timeout_add.assert_called_once()
    assert ctrl._pending == {"streams", "sinks"}


def test_output_is_ticked_again_when_it_rejoins(tmp_path: Path) -> None:
    """Unplugging unticks it; the session taking it back on replug must tick it again."""
    ctrl = _ctrl(tmp_path)
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
