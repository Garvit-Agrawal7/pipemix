"""Device names and presets.

Linux stores config at ~/.config/pipemix/config.json; Windows at
%APPDATA%\\PipeMix\\config.json. `default_log_dir` resolves the matching log
directory for `main.py` on each platform.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def _default_config_path() -> Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(appdata) / "PipeMix" / "config.json"
    return Path.home() / ".config" / "pipemix" / "config.json"


def default_log_dir() -> Path:
    """Where `main.py` should put its rotating log file."""
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        return Path(local_appdata) / "PipeMix" / "logs"
    return Path.home() / ".local" / "share" / "pipemix"


class ConfigManager:

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_config_path()
        self.data: dict = {"devices": {}, "presets": {}, "last_preset": None, "prev_default": None}
        self.load()

    def load(self) -> None:
        """A broken or missing config must never stop the app."""
        legacy = self.path.with_suffix(".toml")
        raw: dict = {}
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            elif legacy.exists():
                import tomllib
                raw = tomllib.loads(legacy.read_text(encoding="utf-8"))
                log.info("Imported legacy config from %s", legacy)
        except Exception as e:
            log.error("Failed to load config: %s. Using empty default.", e)

        self.data = {
            "devices": raw.get("devices", {}),
            "presets": raw.get("presets", {}),
            "last_preset": raw.get("last_preset"),
            "prev_default": raw.get("prev_default"),
        }

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except Exception as e:
            log.error("Failed to save config: %s", e)

    def device_name(self, device_id: str, default: str) -> str:
        return self.data["devices"].get(device_id, default)

    def save_preset(self, preset_id: str, name: str, devices: list[str]) -> None:
        self.data["presets"][preset_id] = {"name": name, "devices": devices}

    def delete_preset(self, preset_id: str) -> None:
        self.data["presets"].pop(preset_id, None)
