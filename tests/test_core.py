from __future__ import annotations

import json
from pathlib import Path

from conftest import HUB, sink_dev as _dev
from pipemix.models import AudioDevice, DeviceKind, VirtualSink, path_to_mac, sink_to_mac
from pipemix.linux import pactl_backend
from pipemix.linux.pactl_backend import PactlBackend, _event_kind, _kind, _parse_inputs
from pipemix.config import ConfigManager

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


def test_macs() -> None:
    assert sink_to_mac("bluez_output.61_C5_02_3A_59_49.1") == "61:C5:02:3A:59:49"
    assert sink_to_mac("alsa_output.pci-0000_00_1f.3.analog-stereo") is None
    assert path_to_mac("/org/bluez/hci0/dev_61_C5_02_3A_59_49") == "61:C5:02:3A:59:49"
    assert path_to_mac("/org/bluez/hci0") is None


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


def test_leg_delays_pw_dump_fails(fake, monkeypatch) -> None:
    # no latency data means every leg gets the plain floor.
    real = pactl_backend._run
    monkeypatch.setattr(pactl_backend, "_run", lambda args: (1, "", "no pw-dump") if args == ["pw-dump"] else real(args))

    sink = VirtualSink(1, HUB)
    PactlBackend().set_legs(sink, [_dev("wired"), _dev("bt")])
    assert sink.delays == {"wired": 30, "bt": 30}


def test_latencies_malformed_json(monkeypatch) -> None:
    # pw-dump's schema is undocumented: an odd-but-valid shape must degrade to {},
    # never raise, like _sink_names().
    backend = PactlBackend()
    for raw in ('{"foo": "bar"}', '"hello world"', "[null]", "not json {",
                json.dumps([{"type": "PipeWire:Interface:Node", "info": {
                    "props": {"node.name": "x"}, "params": {"Latency": [1, 2, 3]}}}])):
        monkeypatch.setattr(pactl_backend, "_run", lambda args, raw=raw: (0, raw, ""))
        assert backend._latencies() == {}


def test_leg_delays_transient_failure(fake, monkeypatch) -> None:
    # pw-dump fails after legs are already aligned: don't tear down what's correct.
    backend = PactlBackend()
    sink = VirtualSink(1, HUB)
    wired, bt = _dev("wired"), _dev("bt")

    backend.set_legs(sink, [wired, bt])
    assert sink.delays == {"wired": 230, "bt": 230}

    real = pactl_backend._run
    monkeypatch.setattr(pactl_backend, "_run", lambda args: (1, "", "pw-dump timed out") if args == ["pw-dump"] else real(args))
    n_loads = len(fake.loads)
    backend.set_legs(sink, [wired, bt])
    assert len(fake.loads) == n_loads, "already-aligned legs must not reload on a transient failure"
    assert sink.delays == {"wired": 230, "bt": 230}


def test_device_identity() -> None:
    # Devices are compared by stable id, so a reconnect with a new sink name is the same device.
    a = AudioDevice("61:C5:02:3A:59:49", "Buds", "bluez_output.61_C5_02_3A_59_49.1", DeviceKind.BLUETOOTH)
    b = AudioDevice("61:C5:02:3A:59:49", "Buds", "bluez_output.61_C5_02_3A_59_49.2", DeviceKind.BLUETOOTH)
    assert a == b and len({a, b}) == 1


def test_sink_names() -> None:
    # Crash recovery finds orphans by this prefix, and must never match a real sink.
    name = VirtualSink.make_name()
    assert name.startswith("pipemix_") and name != VirtualSink.make_name()


def test_config_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = ConfigManager(path)
    cfg.data["devices"]["61:C5:02:3A:59:49"] = 'Boat "Airdopes"'
    assert cfg.save_preset("Movie Mode", ["61:C5:02:3A:59:49"]) == "movie_mode"
    cfg.data["last_preset"] = "movie_mode"
    cfg.save()

    reloaded = ConfigManager(path)
    assert reloaded.data["devices"]["61:C5:02:3A:59:49"] == 'Boat "Airdopes"'
    assert reloaded.data["presets"]["movie_mode"]["devices"] == ["61:C5:02:3A:59:49"]

    assert reloaded.data["last_preset"] == "movie_mode"

    reloaded.delete_preset("movie_mode")
    reloaded.delete_preset("never_existed")  # must not raise
    assert reloaded.data["presets"] == {}


def test_legacy_toml(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[devices]\n"61:C5:02:3A:59:49" = "Boat Airdopes"\n\n'
        '[presets.movie]\nname = "Movie Mode"\ndevices = ["61:C5:02:3A:59:49"]\n'
    )
    cfg = ConfigManager(tmp_path / "config.json")
    assert cfg.data["devices"]["61:C5:02:3A:59:49"] == "Boat Airdopes"
    assert cfg.data["presets"]["movie"]["name"] == "Movie Mode"


def test_missing_config(tmp_path: Path) -> None:
    assert ConfigManager(tmp_path / "nope.json").data == {
        "devices": {}, "presets": {}, "last_preset": None, "prev_default": None,
        "pinned_apps": [],
    }
