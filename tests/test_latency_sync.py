"""Latency + sync fixes (plan items noted per test). Headless: pactl is faked."""
from __future__ import annotations

import json

from conftest import HUB, Fake, node, sink_dev as _dev
from pipemix.models import VirtualSink
from pipemix.linux import pactl_backend
from pipemix.linux.pactl_backend import PactlBackend


def _prop(call: list[str], key: str) -> str:
    return next((a for a in call if a.startswith(key + "=")), "")


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
    good, bad = node("good", 5_000_000), node("bad", 0)
    del bad["info"]["params"]["Latency"][0]["minNs"]
    monkeypatch.setattr(pactl_backend, "_run", lambda a: (0, json.dumps([good, bad]), ""))
    assert PactlBackend()._latencies()["good"] == 5_000_000


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
