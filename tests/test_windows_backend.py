"""
Tests for WasapiBackend — pure logic, faked engine and wasapi calls, no COM.

`wasapi/policy.py`, `wasapi/volume.py` and `wasapi/sessions.py` are a sibling
Phase-3 brief's files and may not exist yet when this runs. Real modules are
used if present; otherwise a bare stand-in is injected into `sys.modules` so
`pipemix.windows.backend` stays importable here, exactly like `test_routing.py`
stubs `gi` for the Linux controller. Either way, the backend's own module-level
names (`Engine`, `list_outputs`, `default_output_id`, `_policy`, `_volume`,
`_sessions`, `_capture_endpoints`) are what every test below patches, so the
behaviour under test never depends on which one is real.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


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
    get_mute=lambda sink: False,
    set_mute=lambda sink, mute: None,
)
_ensure_stub(
    "pipemix.windows.wasapi.sessions",
    list_streams=lambda: [],
    set_stream_mute=lambda stream_id, mute: None,
    set_stream_volume=lambda stream_id, volume: None,
)

from pipemix.models import AudioDevice, DeviceKind, VirtualSink
import pipemix.windows.backend as backend_mod
from pipemix.windows.backend import WasapiBackend


def _dev(id_: str, name: str = "Dev", connected: bool = True) -> AudioDevice:
    return AudioDevice(id=id_, name=name, sink=id_, kind=DeviceKind.USB, connected=connected)


class _FakeEngine:
    """Stands in for wasapi.engine.Engine: records legs, never touches COM."""

    instances: list["_FakeEngine"] = []

    def __init__(self, source_id: str) -> None:
        self.source_id = source_id
        self.started = False
        self.stopped = False
        self._legs: list[str] = []
        _FakeEngine.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def set_legs(self, device_ids) -> None:
        self._legs = list(device_ids)

    @property
    def legs(self) -> list[str]:
        return sorted(self._legs)


def _backend(monkeypatch, *, hub: bool = False, default: str | None = None) -> WasapiBackend:
    """A backend with the engine and every wasapi.* call faked."""
    _FakeEngine.instances = []
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

    monkeypatch.setattr(backend_mod, "default_output_id", lambda: default)

    state = {"default": default}
    monkeypatch.setattr(backend_mod._policy, "get_default", lambda: state["default"])

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
    # re-evaluate until create_sink runs.
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


def test_hub_mode_includes_every_selected_device_as_a_leg(monkeypatch):
    b = _backend(monkeypatch, hub=True, default="original")
    devices = [_dev("dev_a"), _dev("dev_b")]
    sink = b.create_sink(devices)
    engine = sink.module
    assert engine.source_id == "cable_out"
    assert engine.legs == ["dev_a", "dev_b"]
    assert sink.legs == {"dev_a": 0, "dev_b": 0}
    assert b.leader is None


def test_create_sink_leaves_the_default_alone(monkeypatch):
    # Switching the Windows default is the Controller's job (`_route` does it
    # via `sink.name`), exactly as on Linux. The backend doing it too meant it
    # happened *before* `_level_hub` had set the level, so the first moment of
    # audio could arrive at whatever volume that endpoint was sitting at.
    b = _backend(monkeypatch, hub=True, default="original")
    sink = b.create_sink([_dev("dev_a")])
    assert backend_mod._policy.get_default() == "original"
    assert sink.name == "cable_in"   # but it names where the Controller should point


# -- the sink's name has to be a real endpoint --

# The Controller feeds `session.sink.name` straight back to `set_default`,
# `set_volume` and `move_streams` (that is what `active_sink()` hands out), so
# on Windows it must name an endpoint WASAPI knows. Naming it `pipemix_<uuid>`
# the way Linux does makes every one of those calls fail with E_INVALIDARG at
# the moment a session starts — which is exactly what it did.

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
    assert backend_mod._policy.get_default() == "dev_a"  # elected leader

    b.destroy_sink(sink)

    assert backend_mod._policy.get_default() == "original"
    assert sink.module.stopped is True
    assert sink.legs == {}
    assert b.leader is None


def test_destroy_sink_never_raises_when_engine_stop_fails(monkeypatch):
    b = _backend(monkeypatch, hub=False, default="original")
    sink = b.create_sink([_dev("dev_a")])

    def _boom():
        raise RuntimeError("endpoint already gone")
    sink.module.stop = _boom

    b.destroy_sink(sink)  # must not raise
