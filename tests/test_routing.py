"""Tests for audio-routing logic in the Controller.

The Controller owns all routing decisions; the UI is dumb. We mock the
PactlBackend and the GLib main loop so these run headless, no PipeWire needed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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

from pipemix.models import AudioDevice, DeviceKind, SessionState, VirtualSink
from pipemix.services.backend import BackendError, BackendHealth, BackendStatus
from pipemix.services.config.config_manager import ConfigManager
from pipemix.controller import Controller


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
    b.create_sink.side_effect = lambda devs: VirtualSink(999, VirtualSink.make_name())
    return b


def _ctrl(tmp_path: Path, backend=None) -> Controller:
    b = backend or _backend()
    cfg = ConfigManager(tmp_path / "config.json")
    c = Controller(b, cfg)
    # Bypass BlueZ D-Bus monitor — it needs a real system bus.
    c.monitor = MagicMock()
    c.monitor.connected.return_value = []
    c.start()
    return c


# -- Single-device sharing (no virtual sink needed) --

def test_single_device_no_virtual_sink(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    dev = _dev("AA:BB:CC:DD:EE:01", sink="alsa_out.usb")
    ctrl.start_sharing([dev])

    assert ctrl.session.state == SessionState.ACTIVE
    assert ctrl.session.sink is None
    ctrl.backend.set_default.assert_called_with("alsa_out.usb")
    ctrl.backend.create_sink.assert_not_called()


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
    ctrl = _ctrl(tmp_path)
    dev = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    ctrl.start_sharing([dev])
    ctrl.backend.set_volume.reset_mock()

    ctrl.set_master_volume(80)
    ctrl.backend.set_volume.assert_called_with("sink_a", 80)


# -- Stream routing override --

def test_route_stream_records_override(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    ctrl.route_stream(42, "sink_x")

    assert ctrl.overrides[42] == "sink_x"
    ctrl.backend.move_stream.assert_called_with(42, "sink_x")


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


def test_reconnect_resolves_sink_and_rebuilds(tmp_path: Path) -> None:
    ctrl = _ctrl(tmp_path)
    d1 = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    d2 = _dev("AA:BB:CC:DD:EE:02", sink="sink_b")
    ctrl.devices = {d1.id: d1, d2.id: d2}
    ctrl.start_sharing([d1, d2])

    # Device A disconnects — session falls back to B alone.
    ctrl._on_disconnect("AA:BB:CC:DD:EE:01")
    assert d1.connected is False
    assert d1.sink is None

    # Device A reconnects — _on_connect should mark it connected and resolve.
    ctrl.backend.resolve_bt_sink.return_value = "sink_a_new"
    ctrl.backend.create_sink.reset_mock()
    ctrl._on_connect("AA:BB:CC:DD:EE:01")

    assert d1.connected is True
    assert d1.sink == "sink_a_new"
    # Session should rebuild to include both devices again.
    ctrl.backend.create_sink.assert_called_once()
    assert ctrl.session.state == SessionState.ACTIVE


if __name__ == "__main__":
    import tempfile

    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            with tempfile.TemporaryDirectory() as tmp:
                fn(Path(tmp))
            print(f"  ✓  {name}")
    print("\nAll tests passed.")


# -- Toggling an output must not rip down the combined sink first --

def test_swap_creates_before_destroying(tmp_path: Path) -> None:
    """A rebuild keeps the old sink up until the new one has the streams."""
    ctrl = _ctrl(tmp_path)
    d1 = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    d2 = _dev("AA:BB:CC:DD:EE:02", sink="sink_b")
    d3 = _dev("AA:BB:CC:DD:EE:03", sink="sink_c")
    ctrl.start_sharing([d1, d2])
    first = ctrl.session.sink

    order = []
    ctrl.backend.create_sink.side_effect = lambda devs: (
        order.append("create") or VirtualSink(1000, "pipemix_second")
    )
    ctrl.backend.destroy_sink.side_effect = lambda s: order.append("destroy")

    ctrl.start_sharing([d1, d2, d3])

    assert order == ["create", "destroy"], order
    assert ctrl.backend.destroy_sink.call_args[0][0] is first
    assert ctrl.session.sink.name == "pipemix_second"


def test_toggle_with_same_sinks_is_a_noop(tmp_path: Path) -> None:
    """A device with no sink changes the selection, not the routing."""
    ctrl = _ctrl(tmp_path)
    d1 = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    d2 = _dev("AA:BB:CC:DD:EE:02", sink="sink_b")
    offline = _dev("AA:BB:CC:DD:EE:03", sink=None)
    ctrl.start_sharing([d1, d2])
    sink = ctrl.session.sink
    ctrl.backend.create_sink.reset_mock()
    ctrl.backend.destroy_sink.reset_mock()

    ctrl.start_sharing([d1, d2, offline])

    assert ctrl.session.sink is sink
    ctrl.backend.create_sink.assert_not_called()
    ctrl.backend.destroy_sink.assert_not_called()


def test_reconnect_does_not_leak_the_previous_sink(tmp_path: Path) -> None:
    """_rebuild used to overwrite session.sink and leak the module behind it."""
    ctrl = _ctrl(tmp_path)
    d1 = _dev("AA:BB:CC:DD:EE:01", sink="sink_a")
    d2 = _dev("AA:BB:CC:DD:EE:02", sink="sink_b")
    ctrl.devices = {d1.id: d1, d2.id: d2}
    ctrl.start_sharing([d1, d2])

    ctrl._on_disconnect(d2.id)          # falls back to d1 alone, no combined sink
    reduced = ctrl.session.sink
    d2.connected, d2.sink = True, "sink_b"
    ctrl.backend.destroy_sink.reset_mock()
    ctrl._rebuild()

    assert ctrl.session.state == SessionState.ACTIVE
    for call in ctrl.backend.destroy_sink.call_args_list:
        assert call[0][0] is not ctrl.session.sink
    if reduced:
        ctrl.backend.destroy_sink.assert_any_call(reduced)
