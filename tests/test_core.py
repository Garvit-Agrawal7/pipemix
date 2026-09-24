from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipemix.linux.models import AudioDevice, DeviceKind, VirtualSink, path_to_mac, sink_to_mac
from pipemix.linux.services.backend import pactl_backend
from pipemix.linux.services.backend.pactl_backend import PactlBackend, _kind, _parse_inputs, _parse_sinks
from pipemix.linux.services.config.config_manager import ConfigManager

SINKS = """Sink #46
\tState: RUNNING
\tName: bluez_output.61_C5_02_3A_59_49.1
\tDescription: Boat Airdopes
\tOwner Module: 12
\tProperties:
\t\tdevice.bus = "bluetooth"
\t\tnode.virtual = "false"

Sink #47
\tState: IDLE
\tName: pipemix_8f3a2c1d
\tDescription: PipeMix Combined
\tOwner Module: 536870913
\tProperties:
\t\tnode.virtual = "true"
"""

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


def test_parse_sinks() -> None:
    sinks = _parse_sinks(SINKS)
    assert [s["name"] for s in sinks] == ["bluez_output.61_C5_02_3A_59_49.1", "pipemix_8f3a2c1d"]
    assert sinks[0]["desc"] == "Boat Airdopes"
    assert sinks[0]["props"]["device.bus"] == "bluetooth"
    # Orphan recovery needs the module id to unload the right module.
    assert sinks[1]["module"] == 536870913


def test_parse_inputs() -> None:
    streams = _parse_inputs(SINK_INPUTS)
    assert [s["id"] for s in streams] == [101, 102]
    assert streams[0]["application.name"] == "Brave"
    assert streams[0]["sink_index"] == "46"
    assert streams[0]["mute"] is False
    assert streams[1]["mute"] is True


def _pw_node(name: str, ns: int) -> dict:
    return {
        "type": "PipeWire:Interface:Node",
        "info": {
            "props": {"node.name": name},
            "params": {"Latency": [
                {"direction": "Output", "minNs": 999_000_000},  # not Input: must be ignored
                {"direction": "Input", "minQuantum": 1.0, "maxQuantum": 1.0,
                 "minRate": 0, "maxRate": 0, "minNs": ns, "maxNs": ns},
            ]},
        },
    }


def _loaded(loads: list[list[str]], *frags: str) -> bool:
    """True if some recorded _load call carries every fragment, in any of its args."""
    return any(all(any(f in a for a in call) for f in frags) for call in loads)


def test_leg_delays(monkeypatch) -> None:
    # wired reports 0 ns, bt reports 200 ms — pw-dump is the only source of truth.
    monkeypatch.setattr(pactl_backend, "_run", lambda args: (
        0, json.dumps([_pw_node("wired", 0), _pw_node("bt", 200_000_000)]), ""
    ))

    loads: list[list[str]] = []
    ids = iter(range(1, 100))
    monkeypatch.setattr(pactl_backend, "_load", lambda args: (loads.append(args), next(ids))[1])
    unloads: list[int] = []
    monkeypatch.setattr(PactlBackend, "_unload", lambda self, module: unloads.append(module))

    backend = PactlBackend()
    sink = VirtualSink(1, "pipemix_test")
    wired = AudioDevice("w", "Wired", "wired", DeviceKind.BUILTIN)
    bt = AudioDevice("b", "BT", "bt", DeviceKind.BLUETOOTH)
    ghost = AudioDevice("g", "Ghost", None, DeviceKind.UNKNOWN)  # disconnected: no sink to route to

    # 1. one device (plus a disconnected one, which must be ignored) → base delay
    backend.set_legs(sink, [wired, ghost])
    assert _loaded(loads, "sink=wired", "latency_msec=60")
    assert sink.delays["wired"] == 60
    assert None not in sink.legs

    # 2. a slower device joins: the fast leg reloads to match it, old module unloaded
    wired_module = sink.legs["wired"]
    backend.set_legs(sink, [wired, bt])
    assert unloads == [wired_module]
    assert _loaded(loads, "sink=wired", "latency_msec=260")
    assert _loaded(loads, "sink=bt", "latency_msec=60")
    assert sink.delays == {"wired": 260, "bt": 60}

    # 3. high-water mark: dropping bt unloads it but does not pull wired back down
    bt_module = sink.legs["bt"]
    n_loads = len(loads)
    backend.set_legs(sink, [wired])
    assert len(loads) == n_loads, "wired must not reload"
    assert unloads == [wired_module, bt_module]
    assert sink.delays["wired"] == 260
    assert "bt" not in sink.legs


def test_leg_delays_pw_dump_fails(monkeypatch) -> None:
    # today's behaviour: no latency data means every leg gets the plain floor.
    monkeypatch.setattr(pactl_backend, "_run", lambda args: (1, "", "no pw-dump"))
    monkeypatch.setattr(pactl_backend, "_load", lambda args: 1)
    monkeypatch.setattr(PactlBackend, "_unload", lambda self, module: None)

    backend = PactlBackend()
    sink = VirtualSink(1, "pipemix_test")
    wired = AudioDevice("w", "Wired", "wired", DeviceKind.BUILTIN)
    bt = AudioDevice("b", "BT", "bt", DeviceKind.BLUETOOTH)

    backend.set_legs(sink, [wired, bt])
    assert sink.delays == {"wired": 60, "bt": 60}


def test_latencies_malformed_json(monkeypatch) -> None:
    # pw-dump's schema is undocumented and shells out to a process we don't control —
    # an odd-but-valid JSON shape must degrade to {}, never raise, like _sink_names().
    backend = PactlBackend()
    for raw in ('{"foo": "bar"}', '"hello world"', "[null]",
                json.dumps([{"type": "PipeWire:Interface:Node", "info": {
                    "props": {"node.name": "x"}, "params": {"Latency": [1, 2, 3]}}}])):
        monkeypatch.setattr(pactl_backend, "_run", lambda args, raw=raw: (0, raw, ""))
        assert backend._latencies() == {}


def test_leg_delays_transient_failure(monkeypatch) -> None:
    # pw-dump fails after legs are already aligned: don't tear down what's correct.
    monkeypatch.setattr(pactl_backend, "_run", lambda args: (
        0, json.dumps([_pw_node("wired", 0), _pw_node("bt", 200_000_000)]), ""
    ))
    loads: list[list[str]] = []
    monkeypatch.setattr(pactl_backend, "_load", lambda args: (loads.append(args), len(loads))[1])
    monkeypatch.setattr(PactlBackend, "_unload", lambda self, module: None)

    backend = PactlBackend()
    sink = VirtualSink(1, "pipemix_test")
    wired = AudioDevice("w", "Wired", "wired", DeviceKind.BUILTIN)
    bt = AudioDevice("b", "BT", "bt", DeviceKind.BLUETOOTH)

    backend.set_legs(sink, [wired, bt])
    assert sink.delays == {"wired": 260, "bt": 60}

    monkeypatch.setattr(pactl_backend, "_run", lambda args: (1, "", "pw-dump timed out"))
    n_loads = len(loads)
    backend.set_legs(sink, [wired, bt])
    assert len(loads) == n_loads, "already-aligned legs must not reload on a transient failure"
    assert sink.delays == {"wired": 260, "bt": 60}


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
    cfg.save_preset("movie_mode", "Movie Mode", ["61:C5:02:3A:59:49"])
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
    assert ConfigManager(tmp_path / "nope.json").data == {"devices": {}, "presets": {}, "last_preset": None}


if __name__ == "__main__":
    import tempfile

    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            with tempfile.TemporaryDirectory() as tmp:
                fn(Path(tmp)) if fn.__code__.co_argcount else fn()
            print(f"  ✓  {name}")
    print("\nAll tests passed.")
