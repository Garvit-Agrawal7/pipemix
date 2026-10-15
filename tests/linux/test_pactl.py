from __future__ import annotations

import json

from conftest import HUB, sink_dev as _dev
from pipemix.models import AudioDevice, DeviceKind, VirtualSink
from pipemix.linux import pactl_backend
from pipemix.linux.pactl_backend import PactlBackend, _event_kind, _kind, _parse_inputs


SINK_INPUTS = """Sink Input #101
\tSink: 46
\tMute: no
\tProperties:
\t\tapplication.name = "Brave"
\t\tmedia.name = "YouTube"
\t\tmedia.class = "Stream/Output/Audio"

Sink Input #102
\tSink: 46
\tMute: yes
\tProperties:
\t\tapplication.name = "Spotify"
\t\tmedia.name = "Playback"
\t\tmedia.class = "Stream/Output/Audio"
"""


def test_kind() -> None:
    assert _kind("bluez_output.61_C5_02_3A_59_49.1") == DeviceKind.BLUETOOTH
    assert _kind("alsa_output.pci-0000_01_00.1.hdmi-stereo") == DeviceKind.HDMI
    assert _kind("alsa_output.usb-Generic_USB_Audio-00.analog-stereo") == DeviceKind.USB
    assert _kind("alsa_output.pci-0000_00_1f.3.analog-stereo") == DeviceKind.BUILTIN
    assert _kind("alsa_output.usb---_KTMicro_--_KT_USB_Audio_202503071003-00.iec958-stereo") == DeviceKind.USB
    assert _kind("alsa_output.pci-0000_00_1f.3.iec958-stereo") == DeviceKind.HDMI


def test_list_outputs(monkeypatch) -> None:
    sinks = [
        {"name": "alsa_output.pci-0000_00_1f.3.analog-stereo", "description": "Speakers",
         "properties": {}, "volume": {"front-left": {"value_percent": "99%"}}},
        {"name": "bluez_output.AA_BB_CC_DD_EE_FF.1", "description": "Boat Airdopes",
         "properties": {}, "volume": {"mono": {"value_percent": "50%"}}},
        {"name": "pipemix_8f3a2c1d", "properties": {}, "volume": {}},
        {"name": "virtual_thing", "properties": {"node.virtual": "true"}, "volume": {}},
    ]
    monkeypatch.setattr(pactl_backend, "_run", lambda args: (0, json.dumps(sinks), ""))
    devices = PactlBackend().list_outputs()

    assert [(d.id, d.name, d.kind, d.volume) for d in devices] == [
        ("alsa_output.pci-0000_00_1f.3.analog-stereo", "Speakers", DeviceKind.BUILTIN, 99),
        ("AA:BB:CC:DD:EE:FF", "Boat Airdopes", DeviceKind.BLUETOOTH, 50),
    ]


def test_event_kind() -> None:
    # A pactl call we made ourselves shows up as a client event — must be ignored,
    # or watch() would retrigger itself forever.
    assert _event_kind("Event 'new' on client #9484") is None
    assert _event_kind("Event 'new' on sink-input #5") == "streams"
    assert _event_kind("Event 'remove' on sink #3") == "sinks"
    assert _event_kind("Event 'change' on sink #3") is None  # volume, not hotplug


def test_parse_inputs() -> None:
    streams = _parse_inputs(SINK_INPUTS)
    assert [s["id"] for s in streams] == [101, 102]
    assert streams[0]["application.name"] == "Brave"
    assert streams[0]["sink_index"] == "46"
    assert streams[0]["mute"] is False
    assert streams[1]["mute"] is True


def _loaded(loads: list[list[str]], *frags: str) -> bool:
    """True if some recorded _load call carries every fragment, in any of its args."""
    return any(all(any(f in a for a in call) for f in frags) for call in loads)


def test_leg_delays(fake) -> None:
    # wired reports 0 ns, bt reports 200 ms: pw-dump is the only source of truth.
    backend = PactlBackend()
    sink = VirtualSink(1, HUB)
    wired, bt = _dev("wired"), _dev("bt")
    ghost = AudioDevice("g", "Ghost", None, DeviceKind.UNKNOWN)  # disconnected: no sink to route to

    # 1. one device (plus a disconnected one, which must be ignored) -> base delay
    backend.set_legs(sink, [wired, ghost])
    assert _loaded(fake.loads, "sink=wired", "latency_msec=30")
    assert sink.delays["wired"] == 30
    assert None not in sink.legs

    # 2. a slower device joins: the fast leg reloads to match it, old module unloaded
    wired_module = sink.legs["wired"]
    backend.set_legs(sink, [wired, bt])
    assert fake.unloads == [wired_module]
    assert _loaded(fake.loads, "sink=wired", "latency_msec=230")
    assert _loaded(fake.loads, "sink=bt", "latency_msec=230")
    assert sink.delays == {"wired": 230, "bt": 230}

    # 3. high-water mark: dropping bt unloads it but does not pull wired back down
    bt_module = sink.legs["bt"]
    n_loads = len(fake.loads)
    backend.set_legs(sink, [wired])
    assert len(fake.loads) == n_loads, "wired must not reload"
    assert fake.unloads == [wired_module, bt_module]
    assert sink.delays["wired"] == 230
    assert "bt" not in sink.legs


def test_latencies_malformed_json(monkeypatch) -> None:
    # pw-dump's schema is undocumented: an odd-but-valid shape must degrade to {},
    # never raise, like _sink_names().
    backend = PactlBackend()
    for raw in ('{"foo": "bar"}', '"hello world"', "[null]", "not json {",
                json.dumps([{"type": "PipeWire:Interface:Node", "info": {
                    "props": {"node.name": "x"}, "params": {"Latency": [1, 2, 3]}}}])):
        monkeypatch.setattr(pactl_backend, "_run", lambda args, raw=raw: (0, raw, ""))
        assert backend._latencies() == {}
