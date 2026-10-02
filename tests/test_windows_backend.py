from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import ModuleType



def _ensure_stub(name: str, **attrs) -> None:
    try:
        __import__(name)
    except ImportError:
        mod = ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod


class _StubAppRouter:
    available = True

    def route(self, pid, device_id):
        pass


_ensure_stub(
    "pipemix.windows.wasapi.policy",
    set_default=lambda sink: None,
    get_default=lambda: None,
    AppRouter=_StubAppRouter,
)
_ensure_stub(
    "pipemix.windows.wasapi.volume",
    get_volume=lambda sink: 100,
    set_volume=lambda sink, volume: None,
    set_mute=lambda sink, mute: None,
)
_ensure_stub(
    "pipemix.windows.wasapi.sessions",
    list_streams=lambda: [],
    set_stream_mute=lambda stream_id, mute: None,
)

from pipemix.models import AudioDevice, DeviceKind, VirtualSink
import pipemix.windows.backend as backend_mod
from pipemix.windows.backend import WasapiBackend


def _dev(id_: str, name: str = "Dev", connected: bool = True) -> AudioDevice:
    return AudioDevice(id=id_, name=name, sink=id_, kind=DeviceKind.USB, connected=connected)


class _FakeEngine:
    """Stands in for wasapi.engine.Engine: records legs, never touches COM.

    `start()` raises for pids in `fail_start_pids`, a per-test knob like
    `_FakeAppRouter.fail_clear`.
    """

    instances: list["_FakeEngine"] = []
    fail_start_pids: set[int] = set()

    def __init__(self, source_id: str | None = None, *, pid: int | None = None) -> None:
        if (source_id is None) == (pid is None):
            raise ValueError("exactly one of source_id/pid is required")
        self.source_id = source_id
        self.pid = pid
        self.started = False
        self.stopped = False  # told to stop (any stop call)
        self.joined = False   # a joining stop() returned
        self._legs: list[str] = []
        _FakeEngine.instances.append(self)

    def start(self) -> None:
        if self.pid is not None and self.pid in _FakeEngine.fail_start_pids:
            raise RuntimeError(f"could not activate process loopback for pid {self.pid}")
        self.started = True

    def stop(self, wait: bool = True) -> None:
        self.stopped = True
        if wait:
            self.joined = True

    def set_legs(self, device_ids) -> None:
        self._legs = list(device_ids)

    @property
    def legs(self) -> list[str]:
        return sorted(self._legs)


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


def _backend(monkeypatch, *, hub: bool = False, default: str | None = None) -> WasapiBackend:
    """A backend with the engine and every wasapi.* call faked."""
    _FakeEngine.instances = []
    _FakeEngine.fail_start_pids = set()
    monkeypatch.setattr(backend_mod, "Engine", _FakeEngine)

    # list_outputs takes include_virtual: the cable is hidden from the outputs
    # a user picks from, and only the VB-CABLE probe asks to see it.
    if hub:
        monkeypatch.setattr(
            backend_mod, "list_outputs",
            lambda include_virtual=False: (
                [_dev("cable_in", "CABLE Input (VB-Audio Virtual Cable)")]
                if include_virtual else []
            ),
        )
        monkeypatch.setattr(
            backend_mod, "_capture_endpoints",
            lambda: [("cable_out", "CABLE Output (VB-Audio Virtual Cable)")],
        )
    else:
        monkeypatch.setattr(backend_mod, "list_outputs", lambda include_virtual=False: [])
        monkeypatch.setattr(backend_mod, "_capture_endpoints", lambda: [])

    state = {"default": default}
    monkeypatch.setattr(backend_mod, "default_output_id", lambda: state["default"])

    def _set_default(sink):
        state["default"] = sink
    monkeypatch.setattr(backend_mod._policy, "set_default", _set_default)

    return WasapiBackend()


# -- health() / engine detection --

def test_health_reports_hub_only_when_both_cable_endpoints_present(monkeypatch):
    b = _backend(monkeypatch, hub=True)
    assert b.health().engine == "hub"


def test_health_reports_leader_when_cable_input_missing(monkeypatch):
    b = _backend(monkeypatch, hub=False)
    monkeypatch.setattr(backend_mod, "list_outputs", lambda: [])
    monkeypatch.setattr(
        backend_mod, "_capture_endpoints",
        lambda: [("cable_out", "CABLE Output (VB-Audio Virtual Cable)")],
    )
    b._status = None
    assert b.health().engine == "leader"


def test_health_reports_leader_when_cable_output_missing(monkeypatch):
    b = _backend(monkeypatch, hub=False)
    monkeypatch.setattr(
        backend_mod, "list_outputs",
        lambda: [_dev("cable_in", "CABLE Input (VB-Audio Virtual Cable)")],
    )
    monkeypatch.setattr(backend_mod, "_capture_endpoints", lambda: [])
    b._status = None
    assert b.health().engine == "leader"


def test_health_is_cached_across_calls(monkeypatch):
    b = _backend(monkeypatch, hub=True)
    first = b.health()
    # Even if the probe would now answer differently, health() must not
    # re-evaluate until reprobe runs (only before a fresh session).
    monkeypatch.setattr(backend_mod, "list_outputs", lambda: [])
    monkeypatch.setattr(backend_mod, "_capture_endpoints", lambda: [])
    assert b.health() is first


# -- leader election --

def test_leader_election_prefers_current_default(monkeypatch):
    b = _backend(monkeypatch, hub=False, default="dev_b")
    devices = [_dev("dev_a"), _dev("dev_b")]
    assert b._elect_leader(devices) == "dev_b"


def test_leader_election_falls_back_to_first_connected(monkeypatch):
    b = _backend(monkeypatch, hub=False, default="not_in_devices")
    devices = [_dev("dev_a"), _dev("dev_b")]
    assert b._elect_leader(devices) == "dev_a"


def test_leader_election_skips_disconnected(monkeypatch):
    b = _backend(monkeypatch, hub=False, default="not_in_devices")
    devices = [_dev("dev_a", connected=False), _dev("dev_b")]
    assert b._elect_leader(devices) == "dev_b"


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


def test_hub_mode_creates_no_engine_and_covers_every_device_in_legs(monkeypatch):
    # Hub mode starts no engine (per-app routing replaces it), so sink.module is
    # None, but sink.legs still lists every selected device for the UI.
    b = _backend(monkeypatch, hub=True, default="original")
    devices = [_dev("dev_a"), _dev("dev_b")]
    sink = b.create_sink(devices)
    assert sink.module is None
    assert _FakeEngine.instances == []
    assert sink.legs == {"dev_a": 0, "dev_b": 0}
    assert b.leader is None


def test_create_sink_leaves_the_default_alone(monkeypatch):
    # Switching the Windows default is the Controller's job, not create_sink's.
    b = _backend(monkeypatch, hub=True, default="original")
    sink = b.create_sink([_dev("dev_a")])
    assert backend_mod.default_output_id() == "original"
    assert sink.name == "cable_in"   # but it names where the Controller should point


# -- the sink's name has to be a real endpoint --

# The Controller passes `session.sink.name` to `set_default`, `set_volume` and
# `move_stream`, so it must be a real endpoint; a Linux-style `pipemix_<uuid>`
# would fail each with E_INVALIDARG.

def test_hub_mode_names_the_sink_after_cable_input(monkeypatch):
    b = _backend(monkeypatch, hub=True, default="original")
    sink = b.create_sink([_dev("dev_a")])
    assert sink.name == "cable_in"


def test_leader_mode_names_the_sink_after_the_leader(monkeypatch):
    b = _backend(monkeypatch, default="dev_b")
    sink = b.create_sink([_dev("dev_a"), _dev("dev_b")])
    assert sink.name == "dev_b" == b.leader


def test_sink_name_is_never_a_generated_label(monkeypatch):
    b = _backend(monkeypatch, hub=True, default="original")
    sink = b.create_sink([_dev("dev_a")])
    assert not sink.name.startswith("pipemix_")


# -- destroy_sink restores the previous default --

def test_destroy_sink_restores_previous_default(monkeypatch):
    b = _backend(monkeypatch, hub=False, default="original")
    sink = b.create_sink([_dev("dev_a"), _dev("dev_b")])
    # The Controller is what points Windows at the session, so stand in for it.
    b.set_default(sink.name)
    assert backend_mod.default_output_id() == "dev_a"  # elected leader

    b.destroy_sink(sink)

    assert backend_mod.default_output_id() == "original"
    assert sink.module.stopped is True
    assert sink.legs == {}
    assert b.leader is None


def test_destroy_sink_never_raises_when_engine_stop_fails(monkeypatch):
    b = _backend(monkeypatch, hub=False, default="original")
    sink = b.create_sink([_dev("dev_a")])

    def _boom(*_a, **_k):
        raise RuntimeError("endpoint already gone")
    sink.module.stop = _boom

    b.destroy_sink(sink)  # must not raise


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
    monkeypatch.setattr(
        backend_mod, "list_outputs",
        lambda include_virtual=False: (
            [_dev("cable_in", "CABLE Input (VB-Audio Virtual Cable)")]
            if include_virtual else [_dev("real_dev")]
        ),
    )
    # dev "cable_in" itself and a disconnected device don't qualify.
    result = b.restore_target([_dev("cable_in"), _dev("dev_b", connected=False)])
    assert result == "real_dev"


# -- create_sink must never strand CABLE Input as the restore target --

def test_create_sink_never_records_cable_input_as_prev_default(monkeypatch):
    b = _backend(monkeypatch, hub=True, default="cable_in")
    b.create_sink([_dev("dev_a")])
    assert b._prev_default != "cable_in"
    assert b._prev_default == "dev_a"


# -- per-app routes to the hub are undone; manual routes elsewhere are not --

def test_destroy_sink_unpins_apps_routed_to_the_hub_but_not_manual_moves(monkeypatch):
    b = _backend(monkeypatch, hub=True, default="original")
    fake_router = _FakeAppRouter()
    b._app_router = fake_router
    sink = b.create_sink([_dev("dev_a")])

    b.move_stream(1, sink.name)
    b.move_stream(2, sink.name)
    assert b._routed == {1, 2}

    # The user manually points app 2 at a real device — leave that alone.
    b.move_stream(2, "dev_a")
    assert b._routed == {1}

    b.destroy_sink(sink)

    assert (1, None) in fake_router.calls
    assert not any(pid == 2 and target is None for pid, target in fake_router.calls)
    assert b._routed == set()
    assert b._hub is None


def test_destroy_sink_never_raises_when_clearing_a_route_fails(monkeypatch):
    b = _backend(monkeypatch, hub=True, default="original")
    fake_router = _FakeAppRouter()
    fake_router.fail_clear = {1}
    b._app_router = fake_router
    sink = b.create_sink([_dev("dev_a")])
    b.move_stream(1, sink.name)
    assert b._routed == {1}

    b.destroy_sink(sink)  # route(1, None) raises internally — must not propagate

    assert b._routed == set()
    assert b._hub is None


# -- move_stream(pid, None) clears the pin through the router --

def test_move_stream_with_none_clears_the_pin(monkeypatch):
    b = _backend(monkeypatch, hub=True, default="original")
    fake_router = _FakeAppRouter()
    b._app_router = fake_router

    b.move_stream(1, None)

    assert (1, None) in fake_router.calls


# -- per-app routing: leader mode is unchanged --

def test_leader_mode_still_starts_exactly_one_engine(monkeypatch):
    b = _backend(monkeypatch, hub=False, default="dev_a")
    devices = [_dev("dev_a"), _dev("dev_b")]
    b.create_sink(devices)
    assert len(_FakeEngine.instances) == 1
    assert _FakeEngine.instances[0].source_id == "dev_a"


# -- set_app_routes: one Engine(pid=...) per app actively playing into the
# hub, reconciled against whatever routes the Controller asks for this poll --

def _hub_session(monkeypatch) -> WasapiBackend:
    """A backend with a hub session open, as set_app_routes needs."""
    b = _backend(monkeypatch, hub=True, default="original")
    b.create_sink([_dev("dev_a")])
    return b


def test_set_app_routes_starts_one_engine_per_new_pid(monkeypatch):
    b = _hub_session(monkeypatch)
    b.set_app_routes({1: ["dev_a", "dev_b"]})
    assert len(_FakeEngine.instances) == 1
    engine = _FakeEngine.instances[0]
    assert engine.pid == 1
    assert engine.started is True
    assert engine.legs == ["dev_a", "dev_b"]


def test_set_app_routes_same_routes_again_creates_nothing_new(monkeypatch):
    b = _hub_session(monkeypatch)
    b.set_app_routes({1: ["dev_a"]})
    assert len(_FakeEngine.instances) == 1

    b.set_app_routes({1: ["dev_a"]})

    assert len(_FakeEngine.instances) == 1  # no second Engine() built


def test_set_app_routes_changed_ids_calls_set_legs_on_the_same_engine(monkeypatch):
    b = _hub_session(monkeypatch)
    b.set_app_routes({1: ["dev_a"]})
    engine = _FakeEngine.instances[0]

    b.set_app_routes({1: ["dev_a", "dev_b"]})

    assert len(_FakeEngine.instances) == 1  # same engine, not recreated
    assert engine.legs == ["dev_a", "dev_b"]


def test_set_app_routes_ignores_leg_order_when_comparing_ids(monkeypatch):
    b = _hub_session(monkeypatch)
    b.set_app_routes({1: ["dev_a", "dev_b"]})
    engine = _FakeEngine.instances[0]
    engine.set_legs = lambda ids: (_ for _ in ()).throw(
        AssertionError("set_legs must not be called when the id set is unchanged")
    )

    b.set_app_routes({1: ["dev_b", "dev_a"]})  # same set, different order


def test_set_app_routes_dropped_pid_stops_its_engine(monkeypatch):
    b = _hub_session(monkeypatch)
    b.set_app_routes({1: ["dev_a"], 2: ["dev_b"]})
    engine1, engine2 = _FakeEngine.instances

    b.set_app_routes({2: ["dev_b"]})

    assert engine1.stopped is True
    assert engine2.stopped is False

    # Re-adding pid 1 later must start a *new* engine, not reuse the stopped one.
    b.set_app_routes({1: ["dev_a"], 2: ["dev_b"]})
    assert len(_FakeEngine.instances) == 3


def test_set_app_routes_unchanged_pid_touches_nothing(monkeypatch):
    b = _hub_session(monkeypatch)
    b.set_app_routes({1: ["dev_a"]})
    engine = _FakeEngine.instances[0]
    engine.set_legs = lambda ids: (_ for _ in ()).throw(
        AssertionError("set_legs must not be called when routes are unchanged")
    )

    b.set_app_routes({1: ["dev_a"]})  # identical routes — must not touch set_legs


def test_set_app_routes_a_failing_pid_is_logged_and_others_still_start(monkeypatch):
    b = _hub_session(monkeypatch)
    _FakeEngine.fail_start_pids = {1}

    b.set_app_routes({1: ["dev_a"], 2: ["dev_b"]})  # must not raise

    started_pids = {e.pid for e in _FakeEngine.instances if e.started}
    assert started_pids == {2}


def test_set_app_routes_a_failed_pid_is_not_retried_while_still_requested(monkeypatch):
    b = _hub_session(monkeypatch)
    _FakeEngine.fail_start_pids = {1}
    b.set_app_routes({1: ["dev_a"]})
    assert len(_FakeEngine.instances) == 1  # one attempt

    b.set_app_routes({1: ["dev_a"]})  # same route requested again
    assert len(_FakeEngine.instances) == 1  # not retried

    b.set_app_routes({})  # pid leaves the routes entirely
    _FakeEngine.fail_start_pids = set()  # now it would succeed
    b.set_app_routes({1: ["dev_a"]})  # requested again -> a fresh attempt
    assert len(_FakeEngine.instances) == 2


def test_destroy_sink_stops_every_app_engine(monkeypatch):
    b = _backend(monkeypatch, hub=True, default="original")
    sink = b.create_sink([_dev("dev_a")])
    b.set_app_routes({1: ["dev_a"], 2: ["dev_a"]})
    app_engines = list(_FakeEngine.instances)
    assert len(app_engines) == 2

    b.destroy_sink(sink)

    assert all(e.stopped for e in app_engines)


def test_destroy_sink_never_raises_when_an_app_engine_stop_fails(monkeypatch):
    b = _backend(monkeypatch, hub=True, default="original")
    sink = b.create_sink([_dev("dev_a")])
    b.set_app_routes({1: ["dev_a"], 2: ["dev_a"]})
    engine1, engine2 = _FakeEngine.instances

    def _boom(*_a, **_k):
        raise RuntimeError("endpoint already gone")
    engine1.stop = _boom

    b.destroy_sink(sink)  # must not raise

    assert engine2.stopped is True
    b.close(1)
    assert engine2.joined is True


def test_set_legs_on_a_hub_sink_with_no_engine_module_works(monkeypatch):
    b = _backend(monkeypatch, hub=True, default="original")
    sink = b.create_sink([_dev("dev_a")])
    assert sink.module is None

    b.set_legs(sink, [_dev("dev_a"), _dev("dev_b")])  # must not raise

    assert sink.legs == {"dev_a": 0, "dev_b": 0}


# -- set_app_routes never holds a lock across Engine.start/stop --

class _SlowEngine(_FakeEngine):
    """start() blocks until `release` is set; stop() records whether the
    backend's lock was free at the time."""
    entered = threading.Event()
    release = threading.Event()
    backend: WasapiBackend | None = None
    stop_saw_lock_free: list[bool] = []

    def start(self) -> None:
        _SlowEngine.entered.set()
        _SlowEngine.release.wait(1)
        super().start()

    def stop(self, wait: bool = True) -> None:
        lock = _SlowEngine.backend._apps_lock
        free = lock.acquire(blocking=False)
        if free:
            lock.release()
        # Only the signalling calls: a reaper's join may race _stop_engines
        # registering it under _apps_lock, which is harmless.
        if not wait:
            _SlowEngine.stop_saw_lock_free.append(free)
        super().stop(wait)


def _slow_session(monkeypatch) -> WasapiBackend:
    b = _hub_session(monkeypatch)
    monkeypatch.setattr(backend_mod, "Engine", _SlowEngine)
    _SlowEngine.entered = threading.Event()
    _SlowEngine.release = threading.Event()
    _SlowEngine.backend = b
    _SlowEngine.stop_saw_lock_free = []
    return b


def test_destroy_sink_does_not_wait_for_an_in_flight_start(monkeypatch):
    b = _slow_session(monkeypatch)
    sink = VirtualSink(None, "cable_in")
    t = threading.Thread(target=b.set_app_routes, args=({1: ["dev_a"]},))
    t.start()
    assert _SlowEngine.entered.wait(1)

    done = threading.Event()
    threading.Thread(target=lambda: (b.destroy_sink(sink), done.set())).start()
    assert done.wait(0.5)                    # returned while start() is still blocked

    _SlowEngine.release.set()
    t.join(1)
    engine = _FakeEngine.instances[-1]
    assert engine.started and engine.stopped  # the late engine was not adopted
    assert b._apps == {}


def test_set_app_routes_after_destroy_sink_builds_nothing(monkeypatch):
    b = _hub_session(monkeypatch)
    gen = b.apps_gen
    b.destroy_sink(VirtualSink(None, "cable_in"))

    b.set_app_routes({1: ["dev_a"]})             # no session open
    b.set_app_routes({1: ["dev_a"]}, gen=gen)    # routes from the old session

    assert _FakeEngine.instances == []
    assert b._apps == {}


def test_stale_gen_is_ignored_after_a_restart(monkeypatch):
    b = _hub_session(monkeypatch)
    old = b.apps_gen
    b.destroy_sink(VirtualSink(None, "cable_in"))
    b.create_sink([_dev("dev_a")])

    b.set_app_routes({1: ["dev_a"]}, gen=old)
    assert _FakeEngine.instances == []
    b.set_app_routes({1: ["dev_a"]}, gen=b.apps_gen)
    assert len(_FakeEngine.instances) == 1


def test_engine_stop_runs_with_no_backend_lock_held(monkeypatch):
    b = _slow_session(monkeypatch)
    _SlowEngine.release.set()
    b.set_app_routes({1: ["dev_a"], 2: ["dev_a"]})
    b.set_app_routes({2: ["dev_a"]})              # pid 1 dropped -> stop
    b.destroy_sink(VirtualSink(None, "cable_in"))  # pid 2 -> stop
    b.close(1)                                     # and both reapers' joins

    assert _SlowEngine.stop_saw_lock_free == [True, True]


def test_dropped_engine_stops_before_a_new_one_starts(monkeypatch):
    b = _hub_session(monkeypatch)
    b.set_app_routes({1: ["dev_a"]})
    engine1 = _FakeEngine.instances[0]
    seen = []
    orig_start = _FakeEngine.start

    def start(self):
        seen.append(engine1.stopped)
        orig_start(self)
    monkeypatch.setattr(_FakeEngine, "start", start)

    b.set_app_routes({2: ["dev_a"]})  # pid 1 drops, pid 2 appears in the same poll

    assert seen == [True]


def test_set_legs_sent_once_while_engine_legs_lag(monkeypatch):
    b = _hub_session(monkeypatch)
    b.set_app_routes({1: ["dev_a", "dev_b"]})
    engine = _FakeEngine.instances[0]
    calls = []
    engine.set_legs = lambda ids: calls.append(list(ids))   # legs never "open"

    for _ in range(3):
        b.set_app_routes({1: ["dev_a", "dev_b"]})
    assert calls == []                                       # already sent at start

    for _ in range(3):
        b.set_app_routes({1: ["dev_a"]})
    assert calls == [["dev_a"]]


# -- A restart never waits on the old session's slow start (no lock spans set_app_routes) --

class _GateEngine(_FakeEngine):
    """The first start() blocks until `release` (a hung process-loopback activation);
    later ones start at once."""
    entered = threading.Event()
    release = threading.Event()

    def start(self) -> None:
        if not _GateEngine.entered.is_set():
            _GateEngine.entered.set()
            _GateEngine.release.wait(1)
        super().start()


def _restart_during_slow_start(monkeypatch):
    """Old session's poll is stuck in Engine(pid=1).start(); the session restarts and
    the new poll adopts its own pid 1 engine. Returns (backend, old poll thread)."""
    b = _hub_session(monkeypatch)
    monkeypatch.setattr(backend_mod, "Engine", _GateEngine)
    _GateEngine.entered = threading.Event()
    _GateEngine.release = threading.Event()
    old = b.apps_gen
    t = threading.Thread(target=b.set_app_routes, args=({1: ["dev_a"]}, old))
    t.start()
    assert _GateEngine.entered.wait(1)
    b.destroy_sink(VirtualSink(None, "cable_in"))
    b.create_sink([_dev("dev_a")])

    t0 = time.monotonic()
    b.set_app_routes({1: ["dev_b"]}, gen=b.apps_gen)
    assert time.monotonic() - t0 < 0.5           # not queued behind the hung start
    return b, t


def test_restart_adopts_its_engines_while_the_old_start_hangs(monkeypatch):
    b, t = _restart_during_slow_start(monkeypatch)
    stale, fresh = _FakeEngine.instances
    assert b._apps == {1: fresh} and fresh.started and fresh.legs == ["dev_b"]

    _GateEngine.release.set()
    t.join(1)
    assert stale.started and stale.stopped       # signalled, never adopted
    assert b._apps == {1: fresh} and not fresh.stopped
    assert b._app_legs == {1: frozenset({"dev_b"})} and b._failed_apps == set()
    b.close(1)
    assert stale.joined


def test_a_stale_start_failing_late_is_not_recorded_as_a_new_failure(monkeypatch):
    b, t = _restart_during_slow_start(monkeypatch)
    _FakeEngine.fail_start_pids = {1}            # the hung start now fails

    _GateEngine.release.set()
    t.join(1)
    assert b._failed_apps == set()
    assert list(b._apps) == [1] and b._app_legs == {1: frozenset({"dev_b"})}


def test_a_stale_gen_never_touches_the_new_sessions_engines(monkeypatch):
    b = _hub_session(monkeypatch)
    old = b.apps_gen
    b.destroy_sink(VirtualSink(None, "cable_in"))
    b.create_sink([_dev("dev_a")])
    b.set_app_routes({1: ["dev_a"]}, gen=b.apps_gen)
    fresh = _FakeEngine.instances[0]
    calls = []
    fresh.set_legs = lambda ids: calls.append(list(ids))

    b.set_app_routes({1: ["dev_b"]}, gen=old)    # would change its legs
    b.set_app_routes({}, gen=old)                # would drop it

    assert calls == [] and not fresh.stopped
    assert b._apps == {1: fresh} and len(_FakeEngine.instances) == 1


# -- Stopping never waits for a pump to exit: engines are signalled, then reaped --

class _SlowJoinEngine(_FakeEngine):
    """The joining stop() blocks until `release` is set, like a wedged pump."""
    release = threading.Event()

    def stop(self, wait: bool = True) -> None:
        if wait:
            _SlowJoinEngine.release.wait(1)
        super().stop(wait)


def _slow_join(monkeypatch, *, hub: bool = True) -> WasapiBackend:
    b = _backend(monkeypatch, hub=hub, default="original")
    monkeypatch.setattr(backend_mod, "Engine", _SlowJoinEngine)
    _SlowJoinEngine.release = threading.Event()
    return b


def test_destroy_sink_signals_every_engine_and_leaves_the_join_to_a_reaper(monkeypatch):
    b = _slow_join(monkeypatch)
    sink = b.create_sink([_dev("dev_a")])
    b.set_app_routes({1: ["dev_a"], 2: ["dev_a"]})
    engines = list(_FakeEngine.instances)

    t0 = time.monotonic()
    b.destroy_sink(sink)
    assert time.monotonic() - t0 < 0.5
    assert all(e.stopped and not e.joined for e in engines)  # all told, none joined yet

    _SlowJoinEngine.release.set()
    b.close(1)
    assert all(e.joined for e in engines)


def test_destroy_sink_does_not_wait_for_the_leader_engine_to_exit(monkeypatch):
    b = _slow_join(monkeypatch, hub=False)
    sink = b.create_sink([_dev("dev_a"), _dev("dev_b")])

    t0 = time.monotonic()
    b.destroy_sink(sink)
    assert time.monotonic() - t0 < 0.5
    assert sink.module.stopped and not sink.module.joined

    _SlowJoinEngine.release.set()
    b.close(1)
    assert sink.module.joined


def test_set_app_routes_does_not_wait_for_a_dropped_engine_to_exit(monkeypatch):
    b = _slow_join(monkeypatch)
    b.create_sink([_dev("dev_a")])
    b.set_app_routes({1: ["dev_a"]})
    engine1 = _FakeEngine.instances[0]
    seen = []
    orig_start = _SlowJoinEngine.start
    monkeypatch.setattr(_SlowJoinEngine, "start", lambda self: (seen.append(engine1.stopped), orig_start(self)))

    t0 = time.monotonic()
    b.set_app_routes({2: ["dev_a"]})
    assert time.monotonic() - t0 < 0.5
    assert seen == [True]               # told to stop before the new one started
    assert not engine1.joined

    _SlowJoinEngine.release.set()
    b.close(1)
    assert engine1.joined


def test_close_waits_for_reapers_up_to_its_timeout(monkeypatch):
    b = _slow_join(monkeypatch)
    b.create_sink([_dev("dev_a")])
    b.set_app_routes({1: ["dev_a"]})
    engine = _FakeEngine.instances[0]
    b.destroy_sink(VirtualSink(None, "cable_in"))

    t0 = time.monotonic()
    b.close(timeout=0.1)                # wedged: gives up at the bound
    assert time.monotonic() - t0 < 0.5
    assert not engine.joined

    _SlowJoinEngine.release.set()
    b.close(timeout=1)
    assert engine.joined


def test_close_joins_a_reaper_started_while_it_waits(monkeypatch):
    b = _slow_join(monkeypatch)
    b.create_sink([_dev("dev_a")])
    b.set_app_routes({1: ["dev_a"], 2: ["dev_a"]})
    e1, e2 = _FakeEngine.instances
    b.set_app_routes({2: ["dev_a"]})             # e1 -> the first reaper, wedged on release
    gate2 = threading.Event()
    stop2 = e2.stop
    e2.stop = lambda wait=True: (wait and gate2.wait(1), stop2(wait))
    seen = []
    closer = threading.Thread(target=lambda: (b.close(1), seen.append(e2.joined)))
    closer.start()
    time.sleep(0.05)                             # close() is now waiting on the first reaper

    b.destroy_sink(VirtualSink(None, "cable_in"))  # e2 -> a second reaper, mid-close
    _SlowJoinEngine.release.set()
    time.sleep(0.05)                             # the first reaper is done; the second still gated
    gate2.set()
    closer.join(1)
    assert seen == [True]                        # close() waited for the second one too


def test_close_warns_naming_what_it_abandons(monkeypatch, caplog):
    b = _slow_join(monkeypatch)
    b.create_sink([_dev("dev_a")])
    b.set_app_routes({7: ["dev_a"]})
    b.destroy_sink(VirtualSink(None, "cable_in"))

    b.close(timeout=0.05)

    assert "abandoning pipemix-engine-reaper (pid 7)" in caplog.text
    _SlowJoinEngine.release.set()
