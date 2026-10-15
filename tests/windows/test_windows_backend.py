from __future__ import annotations

import threading
import time

import pytest

from conftest import FakeEngine, win_dev as _dev
from pipemix.models import VirtualSink
import pipemix.windows.backend as backend_mod
from pipemix.windows.backend import WasapiBackend


class _FakeAppRouter:
    """Stands in for wasapi.policy.AppRouter: records every route call so
    tests can assert which pids got pinned to the hub and unpinned again."""

    available = True

    def __init__(self) -> None:
        self.calls: list[tuple[int, str | None]] = []
        self.fail_clear: set[int] = set()

    def route(self, pid: int, device_id: str | None) -> None:
        if device_id is None and pid in self.fail_clear:
            raise RuntimeError("process already exited")
        self.calls.append((pid, device_id))


CABLE_IN = _dev("cable_in", "CABLE Input (VB-Audio Virtual Cable)")
CABLE_OUT = _dev("cable_out", "CABLE Output (VB-Audio Virtual Cable)")


def _fake_outputs(monkeypatch, render=(), capture=(), real=()) -> None:
    """Fake `list_outputs`: the cable is hidden from the outputs a user picks
    from, and only the VB-CABLE probe asks to see it (`include_virtual`)."""
    def fake(include_virtual=False, flow="eRender"):
        if flow == "eCapture":
            return list(capture) if include_virtual else []
        return list(render) + list(real) if include_virtual else list(real)
    monkeypatch.setattr(backend_mod, "list_outputs", fake)


def _backend(monkeypatch, *, hub: bool = False, default: str | None = None) -> WasapiBackend:
    """A backend with the engine and every wasapi.* call faked."""
    monkeypatch.setattr(backend_mod, "Engine", FakeEngine)

    _fake_outputs(monkeypatch, render=[CABLE_IN] if hub else [], capture=[CABLE_OUT] if hub else [])

    state = {"default": default}
    monkeypatch.setattr(backend_mod, "default_output_id", lambda: state["default"])

    def _set_default(sink):
        state["default"] = sink
    monkeypatch.setattr(backend_mod._policy, "set_default", _set_default)

    return WasapiBackend()


# -- health() / engine detection --

@pytest.mark.parametrize("render, capture, engine", [
    ([CABLE_IN], [CABLE_OUT], "hub"),
    ([], [CABLE_OUT], "leader"),
    ([CABLE_IN], [], "leader"),
])
def test_health_reports_hub_only_when_both_cable_endpoints_present(monkeypatch, render, capture, engine):
    b = _backend(monkeypatch, hub=False)
    _fake_outputs(monkeypatch, render=render, capture=capture)
    assert b.health().engine == engine


def test_health_is_cached_across_calls(monkeypatch):
    b = _backend(monkeypatch, hub=True)
    first = b.health()
    # Even if the probe would now answer differently, health() must not
    # re-evaluate until reprobe runs (only before a fresh session).
    _fake_outputs(monkeypatch)
    assert b.health() is first


# -- leader election --

@pytest.mark.parametrize("default, devices, expected", [
    ("dev_b", [_dev("dev_a"), _dev("dev_b")], "dev_b"),                        # the current default
    ("not_in_devices", [_dev("dev_a"), _dev("dev_b")], "dev_a"),               # else the first connected
    ("not_in_devices", [_dev("dev_a", connected=False), _dev("dev_b")], "dev_b"),  # skipping disconnected
])
def test_leader_election(monkeypatch, default, devices, expected):
    b = _backend(monkeypatch, hub=False, default=default)
    assert b._elect_leader(devices) == expected


# -- leader excluded from legs in leader mode, included in hub mode --

def test_leader_mode_excludes_leader_from_legs(monkeypatch):
    b = _backend(monkeypatch, hub=False, default="dev_a")
    devices = [_dev("dev_a"), _dev("dev_b")]
    sink = b.create_sink(devices)
    engine = sink.module
    assert engine.source_id == "dev_a"
    assert engine.legs == ["dev_b"]
    assert sink.legs == {"dev_b": 0}
    assert b.leader == "dev_a"
    # The Controller passes `sink.name` to `set_default`, `set_volume` and
    # `move_stream`, so it must be a real endpoint, not a Linux-style `pipemix_<uuid>`.
    assert sink.name == b.leader
    assert FakeEngine.instances == [engine]


def test_hub_mode_creates_no_engine_and_covers_every_device_in_legs(monkeypatch):
    # Hub mode starts no engine (per-app routing replaces it), so sink.module is
    # None, but sink.legs still lists every selected device for the UI.
    b = _backend(monkeypatch, hub=True, default="original")
    devices = [_dev("dev_a"), _dev("dev_b")]
    sink = b.create_sink(devices)
    assert sink.module is None
    assert FakeEngine.instances == []
    assert sink.legs == {"dev_a": 0, "dev_b": 0}
    assert sink.name == "cable_in"   # where the Controller should point; switching the default is its job
    assert b.leader is None


# -- destroy_sink --

# -- restore_target --

def test_restore_target_returns_current_default_when_real_device(monkeypatch):
    b = _backend(monkeypatch, hub=False, default="dev_a")
    assert b.restore_target([_dev("dev_a")]) == "dev_a"


def test_restore_target_falls_back_to_selected_device_when_default_is_cable(monkeypatch):
    b = _backend(monkeypatch, hub=True, default="cable_in")
    result = b.restore_target([_dev("dev_a"), _dev("dev_b")])
    assert result == "dev_a"


def test_restore_target_falls_back_to_list_outputs_when_no_selected_device_qualifies(monkeypatch):
    b = _backend(monkeypatch, hub=True, default="cable_in")
    _fake_outputs(monkeypatch, render=[CABLE_IN], capture=[CABLE_OUT], real=[_dev("real_dev")])
    # dev "cable_in" itself and a disconnected device don't qualify.
    result = b.restore_target([_dev("cable_in"), _dev("dev_b", connected=False)])
    assert result == "real_dev"


def test_destroy_sink_unpins_apps_routed_to_the_hub_but_not_manual_moves(monkeypatch):
    b = _backend(monkeypatch, hub=True, default="original")
    fake_router = _FakeAppRouter()
    b._app_router = fake_router
    sink = b.create_sink([_dev("dev_a")])

    b.move_stream(1, sink.name)
    b.move_stream(2, sink.name)
    b.move_stream(2, "dev_a")  # the user points app 2 at a real device: leave it alone
    fake_router.fail_clear = {3}
    b.move_stream(3, sink.name)

    b.destroy_sink(sink)  # route(3, None) raises internally and must not propagate

    assert (1, None) in fake_router.calls
    assert not any(pid == 2 and target is None for pid, target in fake_router.calls)
    assert b._routed == set() and b._hub is None


# -- set_app_routes: one Engine(pid=...) per app actively playing into the
# hub, reconciled against whatever routes the Controller asks for this poll --

def _hub_session(monkeypatch) -> WasapiBackend:
    """A backend with a hub session open, as set_app_routes needs."""
    b = _backend(monkeypatch, hub=True, default="original")
    b.create_sink([_dev("dev_a")])
    return b


def test_set_app_routes_starts_one_engine_per_new_pid(monkeypatch):
    b = _hub_session(monkeypatch)
    b.set_app_routes({1: ["dev_a", "dev_b"]}, b.apps_gen)
    assert len(FakeEngine.instances) == 1
    engine = FakeEngine.instances[0]
    assert engine.pid == 1
    assert engine.started is True
    assert engine.legs == ["dev_a", "dev_b"]


def test_set_app_routes_changed_ids_calls_set_legs_on_the_same_engine(monkeypatch):
    b = _hub_session(monkeypatch)
    b.set_app_routes({1: ["dev_a"]}, b.apps_gen)
    engine = FakeEngine.instances[0]

    b.set_app_routes({1: ["dev_a", "dev_b"]}, b.apps_gen)

    assert len(FakeEngine.instances) == 1  # same engine, not recreated
    assert engine.legs == ["dev_a", "dev_b"]


def test_set_app_routes_dropped_pid_stops_its_engine(monkeypatch):
    b = _hub_session(monkeypatch)
    b.set_app_routes({1: ["dev_a"], 2: ["dev_b"]}, b.apps_gen)
    engine1, engine2 = FakeEngine.instances

    b.set_app_routes({2: ["dev_b"]}, b.apps_gen)

    assert engine1.stopped is True
    assert engine2.stopped is False

    # Re-adding pid 1 later must start a *new* engine, not reuse the stopped one.
    b.set_app_routes({1: ["dev_a"], 2: ["dev_b"]}, b.apps_gen)
    assert len(FakeEngine.instances) == 3


def test_set_app_routes_a_failing_pid_is_logged_and_others_still_start(monkeypatch):
    b = _hub_session(monkeypatch)
    FakeEngine.fail_start_pids = {1}

    b.set_app_routes({1: ["dev_a"], 2: ["dev_b"]}, b.apps_gen)  # must not raise

    started_pids = {e.pid for e in FakeEngine.instances if e.started}
    assert started_pids == {2}


def test_set_app_routes_a_failed_pid_is_not_retried_while_still_requested(monkeypatch):
    b = _hub_session(monkeypatch)
    FakeEngine.fail_start_pids = {1}
    b.set_app_routes({1: ["dev_a"]}, b.apps_gen)
    assert len(FakeEngine.instances) == 1  # one attempt

    b.set_app_routes({1: ["dev_a"]}, b.apps_gen)  # same route requested again
    assert len(FakeEngine.instances) == 1  # not retried

    b.set_app_routes({}, b.apps_gen)  # pid leaves the routes entirely
    FakeEngine.fail_start_pids = set()  # now it would succeed
    b.set_app_routes({1: ["dev_a"]}, b.apps_gen)  # requested again -> a fresh attempt
    assert len(FakeEngine.instances) == 2


# -- set_app_routes never holds a lock across Engine.start/stop --

class _LockProbe(FakeEngine):
    """stop() records whether the backend's lock was free at the time."""
    backend: WasapiBackend | None = None
    lock_free: list[bool] = []

    def stop(self, wait: bool = True) -> None:
        lock = _LockProbe.backend._apps_lock
        free = lock.acquire(blocking=False)
        if free:
            lock.release()
        _LockProbe.lock_free.append(free)
        super().stop(wait)


def test_destroy_sink_does_not_wait_for_an_in_flight_start(monkeypatch):
    b = _hub_session(monkeypatch)
    FakeEngine.start_gate = threading.Event()
    sink = VirtualSink(None, "cable_in")
    t = threading.Thread(target=b.set_app_routes, args=({1: ["dev_a"]}, b.apps_gen))
    t.start()
    assert FakeEngine.entered.wait(1)

    done = threading.Event()
    threading.Thread(target=lambda: (b.destroy_sink(sink), done.set())).start()
    assert done.wait(0.5)                    # returned while start() is still blocked

    FakeEngine.start_gate.set()
    t.join(1)
    engine = FakeEngine.instances[-1]
    assert engine.started and engine.stopped  # the late engine was not adopted
    assert b._apps == {}


def test_engine_stop_runs_with_no_backend_lock_held(monkeypatch):
    b = _hub_session(monkeypatch)
    monkeypatch.setattr(backend_mod, "Engine", _LockProbe)
    _LockProbe.backend, _LockProbe.lock_free = b, []
    b.set_app_routes({1: ["dev_a"], 2: ["dev_a"]}, b.apps_gen)
    b.set_app_routes({2: ["dev_a"]}, b.apps_gen)              # pid 1 dropped -> stop
    b.destroy_sink(VirtualSink(None, "cable_in"))  # pid 2 -> stop
    b.close(1)

    assert _LockProbe.lock_free == [True, True]


# -- A restart never waits on the old session's slow start (no lock spans set_app_routes) --

def _restart_during_slow_start(monkeypatch):
    """Old session's poll is stuck in Engine(pid=1).start(); the session restarts and
    the new poll adopts its own pid 1 engine. Returns (backend, old poll thread)."""
    b = _hub_session(monkeypatch)
    FakeEngine.start_gate = threading.Event()  # the first start hangs
    old = b.apps_gen
    t = threading.Thread(target=b.set_app_routes, args=({1: ["dev_a"]}, old))
    t.start()
    assert FakeEngine.entered.wait(1)
    b.destroy_sink(VirtualSink(None, "cable_in"))
    b.create_sink([_dev("dev_a")])

    t0 = time.monotonic()
    b.set_app_routes({1: ["dev_b"]}, gen=b.apps_gen)
    assert time.monotonic() - t0 < 0.5           # not queued behind the hung start
    return b, t


def test_restart_adopts_its_engines_while_the_old_start_hangs(monkeypatch):
    b, t = _restart_during_slow_start(monkeypatch)
    stale, fresh = FakeEngine.instances
    assert b._apps == {1: fresh} and fresh.started and fresh.legs == ["dev_b"]

    FakeEngine.start_gate.set()
    t.join(1)
    assert stale.started and stale.stopped       # signalled, never adopted
    assert b._apps == {1: fresh} and not fresh.stopped
    assert b._failed_apps == set()
    b.close(1)
    assert stale.joined


def test_a_stale_start_failing_late_is_not_recorded_as_a_new_failure(monkeypatch):
    b, t = _restart_during_slow_start(monkeypatch)
    FakeEngine.fail_start_pids = {1}            # the hung start now fails

    FakeEngine.start_gate.set()
    t.join(1)
    assert b._failed_apps == set()
    assert list(b._apps) == [1]


def test_a_stale_gen_never_touches_the_new_sessions_engines(monkeypatch):
    b = _hub_session(monkeypatch)
    old = b.apps_gen
    b.destroy_sink(VirtualSink(None, "cable_in"))
    b.create_sink([_dev("dev_a")])
    b.set_app_routes({1: ["dev_a"]}, gen=b.apps_gen)
    fresh = FakeEngine.instances[0]
    calls = []
    fresh.set_legs = lambda ids: calls.append(list(ids))

    b.set_app_routes({1: ["dev_b"]}, gen=old)    # would change its legs
    b.set_app_routes({}, gen=old)                # would drop it

    assert calls == [] and not fresh.stopped
    assert b._apps == {1: fresh} and len(FakeEngine.instances) == 1


# -- Stopping never waits for a pump to exit: engines are signalled, `close` joins --

def _slow_join(monkeypatch, *, hub: bool = True) -> WasapiBackend:
    b = _backend(monkeypatch, hub=hub, default="original")
    FakeEngine.join_gate = threading.Event()  # pumps stay "running" until it is set
    return b


def test_destroy_sink_stops_the_leader_without_joining_and_forgets_it(monkeypatch):
    b = _slow_join(monkeypatch, hub=False)
    sink = b.create_sink([_dev("dev_a"), _dev("dev_b")])

    t0 = time.monotonic()
    b.destroy_sink(sink)
    assert time.monotonic() - t0 < 0.5
    assert sink.module.stopped and not sink.module.joined
    assert sink.legs == {}
    assert b.leader is None

    FakeEngine.join_gate.set()
    b.close(1)
    assert sink.module.joined


def test_destroy_sink_signals_every_engine_without_joining(monkeypatch):
    b = _slow_join(monkeypatch)
    sink = b.create_sink([_dev("dev_a")])
    b.set_app_routes({1: ["dev_a"], 2: ["dev_a"]}, b.apps_gen)
    engines = list(FakeEngine.instances)

    t0 = time.monotonic()
    b.destroy_sink(sink)
    assert time.monotonic() - t0 < 0.5
    assert all(e.stopped and not e.joined for e in engines)  # all told, none joined yet

    FakeEngine.join_gate.set()
    b.close(1)
    assert all(e.joined for e in engines)


def test_set_app_routes_does_not_wait_for_a_dropped_engine_to_exit(monkeypatch):
    b = _slow_join(monkeypatch)
    b.create_sink([_dev("dev_a")])
    b.set_app_routes({1: ["dev_a"]}, b.apps_gen)
    engine1 = FakeEngine.instances[0]
    seen = []
    orig_start = FakeEngine.start
    monkeypatch.setattr(FakeEngine, "start", lambda self: (seen.append(engine1.stopped), orig_start(self)))

    t0 = time.monotonic()
    b.set_app_routes({2: ["dev_a"]}, b.apps_gen)
    assert time.monotonic() - t0 < 0.5
    assert seen == [True]               # told to stop before the new one started
    assert not engine1.joined

    FakeEngine.join_gate.set()
    b.close(1)
    assert engine1.joined


def test_close_waits_for_pumps_up_to_its_timeout(monkeypatch):
    b = _slow_join(monkeypatch)
    b.create_sink([_dev("dev_a")])
    b.set_app_routes({1: ["dev_a"]}, b.apps_gen)
    engine = FakeEngine.instances[0]
    b.destroy_sink(VirtualSink(None, "cable_in"))

    t0 = time.monotonic()
    b.close(timeout=0.1)                # wedged: gives up at the bound
    assert time.monotonic() - t0 < 0.5
    assert not engine.joined

    FakeEngine.join_gate.set()
    b.close(timeout=1)
    assert engine.joined


def test_close_joins_an_engine_stopped_while_it_waits(monkeypatch):
    b = _slow_join(monkeypatch)
    b.create_sink([_dev("dev_a")])
    b.set_app_routes({1: ["dev_a"], 2: ["dev_a"]}, b.apps_gen)
    e1, e2 = FakeEngine.instances
    b.set_app_routes({2: ["dev_a"]}, b.apps_gen)             # e1 stopped, its pump wedged on the gate
    e2.join_gate = gate2 = threading.Event()
    seen = []
    closer = threading.Thread(target=lambda: (b.close(1), seen.append(e2.joined)))
    closer.start()
    time.sleep(0.05)                             # close() is now waiting on e1

    b.destroy_sink(VirtualSink(None, "cable_in"))  # e2 stopped mid-close
    FakeEngine.join_gate.set()
    time.sleep(0.05)                             # e1 is done; e2 still gated
    gate2.set()
    closer.join(1)
    assert seen == [True]                        # close() waited for the second one too
