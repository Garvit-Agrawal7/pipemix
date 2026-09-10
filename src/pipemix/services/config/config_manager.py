"""Device names and presets, stored in ~/.config/pipemix/config.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class ConfigManager:

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".config" / "pipemix" / "config.json"
        self.data: dict = {"devices": {}, "presets": {}, "last_preset": None}
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
