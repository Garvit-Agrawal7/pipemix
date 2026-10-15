from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pipemix.config import ConfigManager
from pipemix.windows.controller import Controller
from conftest import win_backend as _backend, win_dev as _dev


def _ctrl(tmp_path: Path, backend=None, config: ConfigManager | None = None) -> Controller:
    """The Controller without `start()`, so tests drive it step by step and can
    inspect the persisted config in between."""
    b = backend or _backend()
    cfg = config or ConfigManager(tmp_path / "config.json")
    c = Controller(b, cfg)
    c.monitor = MagicMock()
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


# -- Clean stop leaves nothing behind --

def test_clean_stop_leaves_no_prev_default_for_next_startup(tmp_path: Path) -> None:
    b = _backend()
    d1 = _dev("EP1")
    b.list_outputs.return_value = [d1]
    b.get_default.return_value = "CABLE Input"          # the hub is already default
    b.restore_target.return_value = "real_device"        # what should actually come back
    cfg = ConfigManager(tmp_path / "config.json")
    ctrl = _ctrl(tmp_path, backend=b, config=cfg)

    ctrl.start_sharing([d1])
    b.restore_target.assert_called_once_with([d1])
    assert ctrl.prev_default == "real_device"
    assert cfg.data["prev_default"] == "real_device"       # persisted before the switch

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


# -- Startup self-heal: no session, but the default is still our own hub --

@pytest.mark.parametrize("default, heals", [("CABLE Input", True), ("EP1", False)])
def test_startup_heals_a_stranded_hub_default_with_no_session(tmp_path: Path, default: str, heals: bool) -> None:
    b = _backend()
    b.list_outputs.return_value = [_dev("EP1")]
    b.get_default.return_value = default
    b.restore_target.return_value = "EP1"
    ctrl = _ctrl(tmp_path, backend=b)

    ctrl.clean_orphans()

    if heals:
        b.set_default.assert_called_once_with("EP1")
    else:
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
