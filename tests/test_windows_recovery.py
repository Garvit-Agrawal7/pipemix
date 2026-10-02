from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock


from pipemix.models import AudioDevice, DeviceKind, VirtualSink
from pipemix.models import BackendError, BackendHealth, BackendStatus
from pipemix.config import ConfigManager
from pipemix.windows.controller import Controller


def _dev(dev_id: str, name: str = "Dev", connected: bool = True) -> AudioDevice:
    return AudioDevice(id=dev_id, name=name, sink=dev_id if connected else None,
                        kind=DeviceKind.BLUETOOTH, connected=connected)


def _fake_create(devices: list[AudioDevice]) -> VirtualSink:
    if not devices:
        raise BackendError("No devices selected.")
    return VirtualSink(MagicMock(), VirtualSink.make_name(), {d.id: 0 for d in devices})


def _backend(engine: str = "hub") -> MagicMock:
    b = MagicMock()
    b.health.return_value = BackendStatus(BackendHealth.OK, "ok", engine=engine)
    b.find_orphans.return_value = []
    b.list_outputs.return_value = []
    b.get_default.return_value = "prev_default"
    b.restore_target.return_value = "prev_default"
    b.get_volume.return_value = 50
    b.leader = None
    b.create_sink.side_effect = _fake_create
    return b


def _ctrl(tmp_path: Path, backend=None, config: ConfigManager | None = None) -> Controller:
    """The Controller without `start()`, so tests drive it step by step and can
    inspect the persisted config in between."""
    b = backend or _backend()
    cfg = config or ConfigManager(tmp_path / "config.json")
    c = Controller(b, cfg)
    c.monitor = MagicMock()
    c.monitor.connected.return_value = []
    return c


# -- Startup: a leftover prev_default means the last run crashed --

def test_startup_restores_a_stranded_default(tmp_path: Path) -> None:
    b = _backend()
    b.list_outputs.return_value = [_dev("EP1"), _dev("EP2")]
    cfg = ConfigManager(tmp_path / "config.json")
    cfg.data["prev_default"] = "EP1"
    ctrl = _ctrl(tmp_path, backend=b, config=cfg)

    ctrl.clean_orphans()

    b.set_default.assert_called_once_with("EP1")
    assert cfg.data["prev_default"] is None
    # And it actually hit disk, so a second load sees the clear too.
    reloaded = ConfigManager(cfg.path)
    assert reloaded.data["prev_default"] is None


def test_startup_clears_a_stranded_default_for_a_missing_endpoint(tmp_path: Path) -> None:
    b = _backend()
    b.list_outputs.return_value = [_dev("EP2")]          # EP1 got unplugged
    cfg = ConfigManager(tmp_path / "config.json")
    cfg.data["prev_default"] = "EP1"
    ctrl = _ctrl(tmp_path, backend=b, config=cfg)

    ctrl.clean_orphans()

    b.set_default.assert_not_called()
    assert cfg.data["prev_default"] is None


def test_startup_with_no_prev_default_attempts_nothing(tmp_path: Path) -> None:
    b = _backend()
    b.list_outputs.return_value = [_dev("EP1")]
    cfg = ConfigManager(tmp_path / "config.json")
    assert cfg.data["prev_default"] is None
    ctrl = _ctrl(tmp_path, backend=b, config=cfg)

    ctrl.clean_orphans()

    b.set_default.assert_not_called()
    assert cfg.data["prev_default"] is None


# -- Clean stop leaves nothing behind --

def test_clean_stop_leaves_no_prev_default_for_next_startup(tmp_path: Path) -> None:
    b = _backend()
    d1 = _dev("EP1")
    b.list_outputs.return_value = [d1]
    cfg = ConfigManager(tmp_path / "config.json")
    ctrl = _ctrl(tmp_path, backend=b, config=cfg)

    ctrl.start_sharing([d1])
    assert cfg.data["prev_default"] == "prev_default"      # persisted before the switch

    ctrl.stop_sharing()

    assert cfg.data["prev_default"] is None
    reloaded = ConfigManager(cfg.path)
    assert reloaded.data["prev_default"] is None


# -- Ordering: persist before changing the default --

def test_start_sharing_persists_prev_default_before_changing_it(tmp_path: Path) -> None:
    calls: list[str] = []
    b = _backend()
    d1 = _dev("EP1")
    b.list_outputs.return_value = [d1]

    class _RecordingConfig(ConfigManager):
        def save(self) -> None:
            calls.append(f"save:{self.data.get('prev_default')!r}")
            super().save()

    cfg = _RecordingConfig(tmp_path / "config.json")
    b.set_default.side_effect = lambda sink: calls.append(f"set_default:{sink}")
    ctrl = _ctrl(tmp_path, backend=b, config=cfg)

    ctrl.start_sharing([d1])

    save_index = calls.index("save:'prev_default'")
    set_default_index = next(i for i, c in enumerate(calls) if c.startswith("set_default:"))
    assert save_index < set_default_index


# -- restore_target replaces get_default when recording what to restore --

def test_start_sharing_records_restore_target_not_get_default(tmp_path: Path) -> None:
    b = _backend()
    d1 = _dev("EP1")
    b.list_outputs.return_value = [d1]
    b.get_default.return_value = "CABLE Input"          # the hub is already default
    b.restore_target.return_value = "real_device"        # what should actually come back
    ctrl = _ctrl(tmp_path, backend=b)

    ctrl.start_sharing([d1])

    b.restore_target.assert_called_once_with([d1])
    assert ctrl.prev_default == "real_device"
    assert ctrl.config.data["prev_default"] == "real_device"


# -- Startup self-heal: no session, but the default is still our own hub --

def test_startup_heals_a_stranded_hub_default_with_no_session(tmp_path: Path) -> None:
    b = _backend()
    b.list_outputs.return_value = [_dev("EP1")]
    b.get_default.return_value = "CABLE Input"
    b.restore_target.return_value = "EP1"
    ctrl = _ctrl(tmp_path, backend=b)

    ctrl.clean_orphans()

    b.set_default.assert_called_once_with("EP1")


def test_startup_heal_is_a_noop_when_default_already_matches(tmp_path: Path) -> None:
    b = _backend()
    b.list_outputs.return_value = [_dev("EP1")]
    b.get_default.return_value = "EP1"
    b.restore_target.return_value = "EP1"
    ctrl = _ctrl(tmp_path, backend=b)

    ctrl.clean_orphans()

    b.set_default.assert_not_called()


# -- Startup self-heal also un-pins apps a previous run left pinned --

def test_clean_orphans_unpins_a_running_app_left_pinned(tmp_path: Path) -> None:
    b = _backend()
    b.list_outputs.return_value = [_dev("EP1")]
    b.list_streams.return_value = [
        {"id": 42, "name": "App", "sink": "EP1", "mute": False, "exe": "C:\\App.exe"},
    ]
    cfg = ConfigManager(tmp_path / "config.json")
    cfg.data["pinned_apps"] = ["C:\\App.exe"]
    ctrl = _ctrl(tmp_path, backend=b, config=cfg)

    ctrl.clean_orphans()

    b.move_stream.assert_called_once_with(42, None)
    assert cfg.data["pinned_apps"] == []
