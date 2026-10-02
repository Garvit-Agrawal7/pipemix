"""Latency + sync fixes (plan items noted per test). Headless: pactl is faked."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


from pipemix.models import AudioDevice, DeviceKind, VirtualSink
from pipemix.linux import pactl_backend
from pipemix.linux.pactl_backend import PactlBackend, _kind

HUB = "pipemix_test"


def _node(name: str, ns: int, **lat) -> dict:
    p = {"direction": "Input", "minQuantum": 1.0, "minRate": 0, "minNs": ns, **lat}
    return {"type": "PipeWire:Interface:Node",
            "info": {"props": {"node.name": name}, "params": {"Latency": [p]}}}


class Fake:
    """pactl state: {module id: (name, args)}. Unloads are seen via _run, whatever path takes them."""

    def __init__(self, mp, lat: dict[str, int]) -> None:
        self.mods = {1: ("module-null-sink", f"sink_name={HUB}")}
        self.lat, self.next = lat, 100
        self.loads: list[list[str]] = []
        self.unloads: list[int] = []
        self.lists = 0
        mp.setattr(pactl_backend, "_run", self.run)
        mp.setattr(pactl_backend, "_load", self.load)

    def load(self, args: list[str]) -> int:
        self.loads.append(args)
        self.next += 1
        self.mods[self.next] = (args[0], " ".join(args[1:]))
        return self.next

    def run(self, args: list[str]) -> tuple[int, str, str]:
        if args == ["pw-dump"]:
            return 0, json.dumps([_node(n, ns) for n, ns in self.lat.items()]), ""
        if args[-3:] == ["list", "short", "modules"]:
            self.lists += 1
            return 0, "".join(f"{i}\t{n}\t{a}\n" for i, (n, a) in self.mods.items()), ""
        if args[:2] == ["pactl", "unload-module"]:
            self.unloads.append(int(args[2]))
            return (0, "", "") if self.mods.pop(int(args[2]), None) else (1, "", "No such entity")
        return 1, "", f"unexpected {args}"

    def vanish(self, module: int) -> None:
        """The loopback unloaded itself (its sink went away)."""
        del self.mods[module]

    def reuse(self, module: int) -> None:
        """pipewire-pulse handed the dead id to someone else's module."""
        self.mods[module] = ("module-null-sink", "sink_name=someone_else")


def _dev(sink: str) -> AudioDevice:
    return AudioDevice(sink, sink, sink, DeviceKind.BUILTIN)


def _prop(call: list[str], key: str) -> str:
    return next((a for a in call if a.startswith(key + "=")), "")


@pytest.fixture
def fake(monkeypatch) -> Fake:
    return Fake(monkeypatch, {"wired": 0, "bt": 200_000_000})


def _loops(f: Fake) -> list[list[str]]:
    return [c for c in f.loads if c[0] == "module-loopback"]


def test_1_dont_move(fake) -> None:
    PactlBackend().set_legs(VirtualSink(1, HUB), [_dev("wired")])
    (call,) = _loops(fake)
    assert "sink_dont_move=true" in call and "source_dont_move=true" in call, call


def test_2_node_latency_pinned(fake) -> None:
    PactlBackend().set_legs(VirtualSink(1, HUB), [_dev("wired")])
    (call,) = _loops(fake)
    si, so = _prop(call, "sink_input_properties"), _prop(call, "source_output_properties")
    assert f"media.name={HUB}" in si and "node.latency=1024/48000" in si, call
    assert "node.latency=1024/48000" in so, call


def test_3_margin_30(fake) -> None:
    assert pactl_backend.LOOPBACK_LATENCY_MS == 30
    sink = VirtualSink(1, HUB)
    PactlBackend().set_legs(sink, [_dev("wired"), _dev("bt")])
    assert sink.delays == {"wired": 230, "bt": 230}
    assert all("latency_msec=230" in c for c in _loops(fake))


def _live(fake) -> tuple[PactlBackend, VirtualSink]:
    b, sink = PactlBackend(), VirtualSink(1, HUB)
    b.set_legs(sink, [_dev("wired"), _dev("bt")])
    fake.loads.clear()
    fake.lists = 0
    return b, sink


def test_4a_vanished_leg_reloaded(fake) -> None:
    b, sink = _live(fake)
    dead = sink.legs["bt"]
    fake.vanish(dead)
    b.set_legs(sink, [_dev("wired"), _dev("bt")])
    assert [_prop(c, "sink") for c in _loops(fake)] == ["sink=bt"]
    assert sink.legs["bt"] != dead and sink.legs["bt"] in fake.mods
    assert dead not in fake.unloads


def test_4b_reused_id_kept_by_set_legs(fake) -> None:
    b, sink = _live(fake)
    stale = sink.legs["bt"]
    fake.vanish(stale)
    fake.reuse(stale)
    b.set_legs(sink, [_dev("wired")])
    assert stale not in fake.unloads
    assert fake.mods[stale] == ("module-null-sink", "sink_name=someone_else")
    assert "bt" not in sink.legs and "bt" not in sink.delays


def test_4b_reused_id_kept_by_destroy_sink(fake) -> None:
    b, sink = _live(fake)
    stale, live = sink.legs["bt"], sink.legs["wired"]
    fake.vanish(stale)
    fake.reuse(stale)
    b.destroy_sink(sink)
    assert stale not in fake.unloads
    assert live in fake.unloads and 1 in fake.unloads


def test_4c_live_leg_left_alone(fake) -> None:
    b, sink = _live(fake)
    legs = dict(sink.legs)
    b.set_legs(sink, [_dev("wired"), _dev("bt")])
    assert fake.loads == [] and fake.unloads == [] and sink.legs == legs


def test_4d_one_module_list_per_call(fake) -> None:
    # Exactly one: fewer can't detect a vanished or reused leg, more is the per-leg regression.
    b, sink = _live(fake)
    b.set_legs(sink, [_dev("wired"), _dev("bt")])
    assert fake.lists == 1
    fake.lists = 0
    b.destroy_sink(sink)
    assert fake.lists == 1


def test_7_latencies_per_entry(monkeypatch) -> None:
    good, bad = _node("good", 5_000_000), _node("bad", 0)
    del bad["info"]["params"]["Latency"][0]["minNs"]
    monkeypatch.setattr(pactl_backend, "_run", lambda a: (0, json.dumps([good, bad]), ""))
    assert PactlBackend()._latencies()["good"] == 5_000_000
    monkeypatch.setattr(pactl_backend, "_run", lambda a: (0, "not json {", ""))
    assert PactlBackend()._latencies() == {}


def test_9_usb_iec958_is_usb() -> None:
    assert _kind("alsa_output.usb---_KTMicro_--_KT_USB_Audio_202503071003-00.iec958-stereo") == DeviceKind.USB
    assert _kind("alsa_output.pci-0000_01_00.1.hdmi-stereo") == DeviceKind.HDMI
    assert _kind("alsa_output.pci-0000_00_1f.3.iec958-stereo") == DeviceKind.HDMI


def test_4e_reused_hub_id_kept_by_destroy_sink(fake) -> None:
    # pipewire-pulse restarted: the hub is gone and its id went to someone else.
    b, sink = _live(fake)
    fake.reuse(1)
    b.destroy_sink(sink)
    assert 1 not in fake.unloads


def test_4f_orphan_loopback_unloaded(fake) -> None:
    # clean_orphans hands destroy_sink each leftover loopback as its own VirtualSink.
    fake.mods[11] = ("module-loopback", f"source={HUB}.monitor sink=x sink_input_properties=media.name={HUB}")
    PactlBackend().destroy_sink(VirtualSink(11, HUB))
    assert 11 in fake.unloads
