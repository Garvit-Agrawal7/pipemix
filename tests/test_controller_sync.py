from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from conftest import mkdev as _dev
from pipemix.models import SessionState
import pipemix.linux.controller as controller_module
from pipemix.api import Api

A, B = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"


@pytest.fixture
def glib(monkeypatch):
    g = MagicMock()
    monkeypatch.setattr(controller_module, "GLib", g)
    return g


@pytest.fixture
def ctrl(make_ctrl, glib):
    return make_ctrl(track_legs=True)


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

def test_6_start_and_retarget_add_schedule_one_recheck(ctrl, glib):
    a, b = _dev(A, "sink_a"), _dev(B, "sink_b")
    ctrl.devices = {A: a, B: b}
    ctrl.start_sharing([a])

    rs = _rechecks(glib, ctrl)
    assert len(rs) == 1
    assert 1000 <= rs[0].args[0] <= 5000
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


def test_10_hotplug_outside_session_leaves_bt_alone(ctrl):
    ctrl.devices = {A: _dev(A, "bluez_output.X.1")}
    ctrl.backend.resolve_bt_sink.return_value = "bluez_output.X.2"

    ctrl._hotplug()

    ctrl.backend.resolve_bt_sink.assert_not_called()
    ctrl.backend.set_legs.assert_not_called()
    assert ctrl.session.state == SessionState.IDLE


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
