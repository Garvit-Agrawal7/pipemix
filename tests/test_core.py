from __future__ import annotations

from pathlib import Path

from pipemix.bridge import to_json
from pipemix.models import AudioDevice, BackendHealth, BackendStatus, DeviceKind, SessionState, path_to_mac, sink_to_mac
from pipemix.config import ConfigManager


def test_macs() -> None:
    assert sink_to_mac("bluez_output.61_C5_02_3A_59_49.1") == "61:C5:02:3A:59:49"
    assert sink_to_mac("alsa_output.pci-0000_00_1f.3.analog-stereo") is None
    assert path_to_mac("/org/bluez/hci0/dev_61_C5_02_3A_59_49") == "61:C5:02:3A:59:49"
    assert path_to_mac("/org/bluez/hci0") is None


def test_device_identity() -> None:
    # Devices are compared by stable id, so a reconnect with a new sink name is the same device.
    a = AudioDevice("61:C5:02:3A:59:49", "Buds", "bluez_output.61_C5_02_3A_59_49.1", DeviceKind.BLUETOOTH)
    b = AudioDevice("61:C5:02:3A:59:49", "Buds", "bluez_output.61_C5_02_3A_59_49.2", DeviceKind.BLUETOOTH)
    assert a == b and len({a, b}) == 1


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


def test_to_json_unwraps_dataclasses_and_enums():
    device = AudioDevice(id="aa", name="Cans", sink=None, kind=DeviceKind.BLUETOOTH)
    assert to_json(device) == {
        "id": "aa", "name": "Cans", "sink": None, "kind": "bluetooth",
        "connected": False, "volume": 50,
    }
    assert to_json(SessionState.REPAIRING) == "repairing"
    assert to_json(BackendStatus(BackendHealth.DEGRADED, "hmm")) == {
        "health": "degraded", "message": "hmm", "engine": "native",
    }
