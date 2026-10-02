"""Tests for CoreAudioBackend against a fake Core Audio.

`pipemix.macos.coreaudio` only loads the frameworks on first call, so it
imports anywhere; every function the backend reaches is swapped for one
backed by a dict of devices, and the suite needs no audio hardware.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipemix.models import AudioDevice, DeviceKind
from pipemix.macos import coreaudio as ca
from pipemix.macos import devices as devmod
from pipemix.macos.backend import HUB_UID, CoreAudioBackend


class FakeHAL:
    def __init__(self) -> None:
        # uid -> (object id, transport)
        self.devices = {
            "spk": (10, ca.TRANSPORT_BUILTIN),
            "bt":  (11, ca.TRANSPORT_BLUETOOTH),
            "usb": (12, ca.TRANSPORT_USB),
        }
        self.volume: dict[int, float] = {10: 0.5, 11: 0.5, 12: 0.5}
        self.default = 10
        self.aggs: dict[int, tuple[str, list[str], str]] = {}
        self.next_id = 100
        self.fail_live_update = False

    def install(self, mp: pytest.MonkeyPatch) -> None:
        mp.setattr(ca, "device_for_uid", self.device_for_uid)
        mp.setattr(ca, "transport", lambda d: next(
            (t for i, t in self.devices.values() if i == d), ca.TRANSPORT_AGGREGATE))
        mp.setattr(ca, "uid", self.uid)
        mp.setattr(ca, "get_volume", lambda d: self.volume.get(d))
        mp.setattr(ca, "set_volume", self.set_volume)
        mp.setattr(ca, "set_mute", lambda d, m: True)
        mp.setattr(ca, "default_output", lambda: self.default)
        mp.setattr(ca, "set_default_output", lambda d: setattr(self, "default", d))
        mp.setattr(ca, "create_aggregate", self.create_aggregate)
        mp.setattr(ca, "set_subdevices", self.set_subdevices)
        mp.setattr(ca, "destroy_aggregate", lambda d: self.aggs.pop(d))
        mp.setattr(devmod, "hub_devices", lambda: [(i, a[0]) for i, a in self.aggs.items()])

    def device_for_uid(self, u: str) -> int | None:
        if u in self.devices:
            return self.devices[u][0]
        return next((i for i, a in self.aggs.items() if a[0] == u), None)

    def uid(self, d: int) -> str:
        for u, (i, _) in self.devices.items():
            if i == d:
                return u
        return self.aggs[d][0]

    def set_volume(self, d: int, level: float) -> bool:
        self.volume[d] = round(level, 4)
        return True

    def create_aggregate(self, u, name, subs, main) -> int:
        self.next_id += 1
        self.aggs[self.next_id] = (u, list(subs), main)
        return self.next_id

    def set_subdevices(self, agg, subs, main) -> None:
        if self.fail_live_update:
            raise ca.CoreAudioError("set grup", -1)
        self.aggs[agg] = (self.aggs[agg][0], list(subs), main)

    def hub(self) -> tuple[str, list[str], str]:
        (agg,) = self.aggs.values()
        return agg

    def vol(self, u: str) -> float:
        return self.volume[self.devices[u][0]]


@pytest.fixture
def hal(monkeypatch: pytest.MonkeyPatch) -> FakeHAL:
    h = FakeHAL()
    h.install(monkeypatch)
    return h


def _dev(u: str) -> AudioDevice:
    return AudioDevice(id=u, name=u, sink=u, kind=DeviceKind.UNKNOWN, connected=True)


def test_hub_is_clocked_by_wired_hardware_not_bluetooth(hal: FakeHAL) -> None:
    b = CoreAudioBackend()
    b.create_sink([_dev("bt"), _dev("spk")])
    _, subs, main = hal.hub()
    assert main == "spk"
    assert subs == ["spk", "bt"]


def test_master_scales_each_leg_and_rows_keep_their_own_level(hal: FakeHAL) -> None:
    b = CoreAudioBackend()
    b.set_volume("spk", 80)
    b.set_volume("bt", 40)
    sink = b.create_sink([_dev("spk"), _dev("bt")])

    b.set_volume(sink.name, 50)
    assert hal.vol("spk") == 0.4
    assert hal.vol("bt") == 0.2
    assert b.get_volume("spk") == 80     # the row still reads its own level
    assert b.get_volume(sink.name) == 50
    assert sink.name.startswith(HUB_UID + ".")

    b.set_volume("bt", 100)              # a row moves under the master
    assert hal.vol("bt") == 0.5


def test_dropping_a_leg_puts_it_back_at_its_own_level(hal: FakeHAL) -> None:
    b = CoreAudioBackend()
    b.set_volume("spk", 80)
    b.set_volume("usb", 60)
    sink = b.create_sink([_dev("spk"), _dev("usb")])
    b.set_volume(sink.name, 50)

    b.set_legs(sink, [_dev("spk")])

    assert hal.hub()[1] == ["spk"]
    assert hal.vol("usb") == 0.6
    assert hal.vol("spk") == 0.4


def test_legs_change_live_on_the_same_hub(hal: FakeHAL) -> None:
    b = CoreAudioBackend()
    sink = b.create_sink([_dev("spk")])
    agg = sink.module

    b.set_legs(sink, [_dev("spk"), _dev("usb")])

    assert sink.module == agg
    assert hal.hub()[1] == ["spk", "usb"]


def test_failed_live_update_rebuilds_and_keeps_the_default(hal: FakeHAL) -> None:
    b = CoreAudioBackend()
    sink = b.create_sink([_dev("spk")])
    b.set_default(sink.name)
    hal.fail_live_update = True

    b.set_legs(sink, [_dev("spk"), _dev("bt")])

    assert hal.hub()[1] == ["spk", "bt"]
    assert hal.default == sink.module
    assert hal.uid(hal.default) == sink.name


def test_create_replaces_a_leftover_hub(hal: FakeHAL) -> None:
    hal.create_aggregate(HUB_UID, "PipeMix", ["spk"], "spk")
    b = CoreAudioBackend()
    b.create_sink([_dev("usb")])
    assert len(hal.aggs) == 1
    assert hal.hub()[1] == ["usb"]


def test_destroy_restores_levels_and_removes_the_hub(hal: FakeHAL) -> None:
    b = CoreAudioBackend()
    b.set_volume("spk", 80)
    b.set_volume("bt", 70)
    sink = b.create_sink([_dev("spk"), _dev("bt")])
    b.set_volume(sink.name, 50)

    b.destroy_sink(sink)

    assert hal.aggs == {}
    assert hal.vol("spk") == 0.8
    assert hal.vol("bt") == 0.7


def test_restore_target_skips_a_hub_default(hal: FakeHAL) -> None:
    b = CoreAudioBackend()
    assert b.restore_target([]) == "spk"
    sink = b.create_sink([_dev("bt")])
    b.set_default(sink.name)
    assert b.restore_target([_dev("bt")]) == "bt"


def test_device_kinds() -> None:
    assert devmod.device_kind(ca.TRANSPORT_BUILTIN) == DeviceKind.BUILTIN
    assert devmod.device_kind(ca.TRANSPORT_BLUETOOTHLE) == DeviceKind.BLUETOOTH
    assert devmod.device_kind(ca.TRANSPORT_DISPLAYPORT) == DeviceKind.HDMI
    assert devmod.device_kind(ca.TRANSPORT_VIRTUAL) == DeviceKind.UNKNOWN
