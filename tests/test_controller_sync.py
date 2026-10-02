from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


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
from pipemix.models import BackendHealth, BackendStatus
from pipemix.config import ConfigManager
import pipemix.linux.controller as controller_module
from pipemix.linux.controller import Controller
from pipemix.api import Api

A, B = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"


def _dev(i, sink=None, kind=DeviceKind.BLUETOOTH):
    return AudioDevice(id=i, name=i, sink=sink, kind=kind, connected=sink is not None)


def _create(devices):
    return VirtualSink(999, VirtualSink.make_name(), {d.sink: 1 for d in devices if d.sink})


@pytest.fixture
def glib(monkeypatch):
    # sys.modules.setdefault means test_routing's stub may be the live one, so patch per test.
    g = MagicMock()
    monkeypatch.setattr(controller_module, "GLib", g)
    return g


def _fake_set_legs(s, ds):
    fresh = {d.sink for d in ds if d.sink} - set(s.legs)
    s.legs = {d.sink: 1 for d in ds if d.sink}
    return fresh


@pytest.fixture
def ctrl(tmp_path, glib):
    b = MagicMock()
    b.health.return_value = BackendStatus(BackendHealth.OK, "ok")
    b.find_orphans.return_value = []
    b.list_outputs.return_value = []
    b.list_streams.return_value = []
    b.create_sink.side_effect = _create
    b.set_legs.side_effect = _fake_set_legs
    c = Controller(b, ConfigManager(tmp_path / "config.json"))
    c.monitor = MagicMock()
    c.monitor.connected.return_value = []
    c._bg = lambda fn, *a: fn(*a)
    c.start()
    return c


def _rechecks(glib, ctrl):
    """One-shot timeouts that end in _rebuild."""
    return [c for c in glib.timeout_add.call_args_list if any(a == ctrl._rebuild for a in c.args)]


def _legs(ctrl):
    return {d.id: d.sink for d in ctrl.backend.set_legs.call_args.args[1]}


# -- 5: _rebuild keeps targets that are still offline --

def test_5_rebuild_keeps_offline_targets(ctrl):
    a, b = _dev(A, "sink_a"), _dev(B, "sink_b")
    ctrl.devices = {A: a, B: b}
    ctrl.start_sharing([a, b])
    ctrl._on_disconnect(B)

    ctrl._rebuild()  # only A is back

    assert B in ctrl.targets

    b.connected, b.sink = True, "sink_b"
    ctrl._rebuild()

    assert set(_legs(ctrl)) == {A, B}


# -- 6: one latency re-check when a leg joins, none otherwise --

def test_6_start_schedules_one_recheck(ctrl, glib):
    a = _dev(A, "sink_a")
    ctrl.devices = {A: a}
    ctrl.start_sharing([a])

    rs = _rechecks(glib, ctrl)
    assert len(rs) == 1
    assert 1000 <= rs[0].args[0] <= 5000


def test_6_retarget_adding_schedules_one_recheck(ctrl, glib):
    a, b = _dev(A, "sink_a"), _dev(B, "sink_b")
    ctrl.devices = {A: a, B: b}
    ctrl.start_sharing([a])
    glib.reset_mock()

    ctrl.start_sharing([a, b])

    rs = _rechecks(glib, ctrl)
    assert len(rs) == 1
    assert 1000 <= rs[0].args[0] <= 5000


def test_6_no_recheck_when_targets_do_not_grow(ctrl, glib):
    a, b = _dev(A, "sink_a"), _dev(B, "sink_b")
    ctrl.devices = {A: a, B: b}
    ctrl.start_sharing([a, b])
    assert _rechecks(glib, ctrl), "precondition: start schedules a re-check"
    glib.reset_mock()

    ctrl._rebuild()             # same set
    ctrl.start_sharing([a, b])  # same set
    ctrl.start_sharing([a])     # shrinks

    assert not _rechecks(glib, ctrl)


# -- 10: sink churn mid-session re-resolves Bluetooth targets --

def test_10_hotplug_follows_a_recreated_bt_sink(ctrl):
    a = _dev(A, "bluez_output.X.1")
    ctrl.devices = {A: a}
    ctrl.start_sharing([a])
    ctrl.backend.set_legs.reset_mock()
    ctrl.backend.resolve_bt_sink.return_value = "bluez_output.X.2"

    ctrl._hotplug()  # wired set unchanged

    ctrl.backend.resolve_bt_sink.assert_any_call(A)
    ctrl.backend.set_legs.assert_called()
    assert _legs(ctrl) == {A: "bluez_output.X.2"}


def test_10_hotplug_restores_a_missing_bt_leg(ctrl):
    a = _dev(A, "bluez_output.X.1")
    ctrl.devices = {A: a}
    ctrl.start_sharing([a])
    ctrl.session.sink.legs.clear()  # the leg unloaded itself
    ctrl.backend.set_legs.reset_mock()
    ctrl.backend.resolve_bt_sink.return_value = "bluez_output.X.1"

    ctrl._hotplug()

    ctrl.backend.set_legs.assert_called()
    assert _legs(ctrl) == {A: "bluez_output.X.1"}


def test_10_hotplug_outside_session_leaves_bt_alone(ctrl):
    ctrl.devices = {A: _dev(A, "bluez_output.X.1")}
    ctrl.backend.resolve_bt_sink.return_value = "bluez_output.X.2"

    ctrl._hotplug()

    ctrl.backend.resolve_bt_sink.assert_not_called()
    ctrl.backend.set_legs.assert_not_called()
    assert ctrl.session.state == SessionState.IDLE


# -- 8: Api applies the selection under the controller lock --

def test_8_apply_selection_holds_the_lock(ctrl):
    api = Api(ctrl)
    a, b = _dev(A, "sink_a"), _dev(B, "sink_b")
    ctrl.devices = {A: a, B: b}
    api.toggle_device(A, True)
    api.start_sharing()
    held = []
    ctrl.start_sharing = lambda devs: held.append(ctrl._lock._is_owned())
    ctrl.stop_sharing = lambda: held.append(ctrl._lock._is_owned())

    api.toggle_device(B, True)   # start_sharing
    api.toggle_device(A, False)
    api.toggle_device(B, False)  # stop_sharing

    assert held and all(held)


def test_8_toggle_waits_for_the_lock(ctrl):
    api = Api(ctrl)
    ctrl.devices = {A: _dev(A, "sink_a")}
    api._selected[A] = True
    with ctrl._lock:
        t = threading.Thread(target=api.toggle_device, args=(A, False))
        t.start()
        time.sleep(0.1)
        # A routing change in flight: the toggle must queue behind it, so ticks apply in order.
        assert api._selected[A] is True
    t.join(2)
    assert api._selected[A] is False
