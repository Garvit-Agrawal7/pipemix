"""Tests for the macOS-only pieces around the Controller: Bluetooth battery
parsing, volume-key decoding, and the backend's per-app tap worker.

Nothing here touches Core Audio, TCC or AppKit; the router is a fake.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipemix.linux.services.backend import BackendError
from pipemix.macos import apps as apps_mod
from pipemix.macos import battery, media_keys
from pipemix.macos import coreaudio as ca
from pipemix.macos.backend import CoreAudioBackend


# -- Battery --

REPORT = {"SPBluetoothDataType": [{
    "device_connected": [
        {"JBL Go 4": {"device_address": "90:F2:60:8A:C3:A1", "device_batteryLevelMain": "70%"}},
        {"AirPods Pro": {"device_address": "80:95:3A:DD:10:1E",
                         "device_batteryLevelLeft": "55%", "device_batteryLevelRight": "40%",
                         "device_batteryLevelCase": "10%"}},
        {"Mouse": {"device_address": "13:05:AA:00:0C:55"}},
    ],
    "device_not_connected": [
        {"Bose": {"device_address": "AC:BF:71:3E:6D:71", "device_batteryLevelMain": "90%"}},
    ],
}]}


def test_battery_parse_takes_main_or_the_lower_bud() -> None:
    levels = battery.parse(REPORT)
    assert levels == {"90f2608ac3a1": 70, "80953add101e": 40}


def test_battery_address_from_coreaudio_uid() -> None:
    assert battery.address_of("90-F2-60-8A-C3-A1:output") == "90f2608ac3a1"
    assert battery.address_of("BuiltInSpeakerDevice") is None


def test_battery_read_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise OSError("no system_profiler")
    monkeypatch.setattr(battery.subprocess, "run", boom)
    assert battery.read() == {}


# -- Volume keys --

def _data1(code: int, down: bool = True) -> int:
    return (code << 16) | ((0xA if down else 0xB) << 8)


def test_volume_keys_decode() -> None:
    assert media_keys.decode(_data1(0)) == ("up", 100 / 16)
    assert media_keys.decode(_data1(1)) == ("down", 100 / 16)
    assert media_keys.decode(_data1(7)) == ("mute", 0)
    assert media_keys.decode(_data1(0, down=False)) is None
    assert media_keys.decode(_data1(16)) is None          # play/pause: not ours
    fine = media_keys.FLAG_SHIFT | media_keys.FLAG_OPTION
    assert media_keys.decode(_data1(0), fine) == ("up", 100 / 64)


# -- The tap worker --

class FakeRouter:
    def __init__(self) -> None:
        self.routes: dict[int, object] = {}
        self.calls: list[tuple] = []
        self.fail: set[int] = set()

    def apply(self, pid, devices, mute):
        self.calls.append((pid, devices, mute))
        if pid in self.fail:
            raise ca.CoreAudioError("tap", -1)
        if devices or mute:
            self.routes[pid] = object()
        else:
            self.routes.pop(pid, None)

    def clear(self, pid):
        self.routes.pop(pid, None)

    def clear_all(self):
        self.routes.clear()


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> tuple[CoreAudioBackend, FakeRouter]:
    monkeypatch.setattr(ca, "taps_supported", lambda: True)
    monkeypatch.setattr(apps_mod, "permission", lambda: apps_mod.PERMISSION_GRANTED)
    monkeypatch.setattr("pipemix.macos.tapcopy.load", lambda: None)
    b = CoreAudioBackend()
    r = FakeRouter()
    b._router = r
    return b, r


def _settle(b: CoreAudioBackend) -> None:
    for _ in range(100):
        if not b._wake.is_set() and not b._busy:
            time.sleep(0.02)
            if not b._wake.is_set() and not b._busy:
                return
        time.sleep(0.01)


def test_routes_are_applied_off_the_callers_thread(backend) -> None:
    b, r = backend
    b.set_app_routes({42: ["spk"]})
    _settle(b)
    assert (42, ["spk"], False) in r.calls
    assert 42 in r.routes

    b.set_app_routes({})
    _settle(b)
    assert 42 not in r.routes


def test_mute_wins_over_a_route_and_survives_unrouting(backend) -> None:
    b, r = backend
    b.set_app_routes({7: ["spk"]})
    b.set_stream_mute(7, True)
    _settle(b)
    assert r.calls[-1] == (7, ["spk"], True)
    b.set_app_routes({})
    _settle(b)
    assert r.calls[-1] == (7, None, True)
    assert 7 in r.routes


def test_a_failed_capture_becomes_the_apps_hint(backend) -> None:
    b, r = backend
    r.fail.add(9)
    b.set_app_routes({9: ["spk"]})
    _settle(b)
    assert "could not capture" in b.route_problem(9, True)


def test_denied_permission_is_refused_up_front(backend, monkeypatch) -> None:
    b, _ = backend
    monkeypatch.setattr(apps_mod, "permission", lambda: apps_mod.PERMISSION_DENIED)
    with pytest.raises(BackendError, match="System Audio Recording"):
        b.set_app_routes({1: ["spk"]})
    with pytest.raises(BackendError):
        b.set_stream_mute(1, True)
    b.set_stream_mute(1, False)   # unmuting never needs permission


def test_quit_apps_are_forgotten(backend, monkeypatch) -> None:
    b, r = backend
    b.set_app_routes({5: ["spk"]})
    _settle(b)
    monkeypatch.setattr(apps_mod, "playing", lambda keep=frozenset(): [])
    assert b.list_streams() == []
    _settle(b)
    assert 5 not in r.routes
    assert b._app_routes == {}


def test_clear_apps_releases_everything(backend) -> None:
    b, r = backend
    b.set_app_routes({1: ["a"], 2: ["b"]})
    b.set_stream_mute(3, True)
    _settle(b)
    b.clear_apps()
    _settle(b)
    assert r.routes == {}
