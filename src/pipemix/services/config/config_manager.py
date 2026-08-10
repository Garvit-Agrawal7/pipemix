"""
PipeMix — ConfigManager

Handles loading and saving the TOML configuration file.
Saves to ~/.config/pipemix/config.toml.
Ensures the directory exists and handles file errors gracefully.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import tomllib  # Python 3.11+

log = logging.getLogger(__name__)

# Default configuration template
DEFAULT_CONFIG = """[general]
start_minimized = false
autostart = false
last_preset = ""

[devices]
# Stores device MAC/ID to display name mappings
# Example:
# "61:C5:02:3A:59:49" = "Boat Airdopes"

[presets]
# Example:
# [presets.movie]
# name = "Movie Mode"
# devices = ["61:C5:02:3A:59:49", "E3:ED:0F:56:16:7D"]
"""

class ConfigManager:
    """
    Manages TOML configuration file reading and writing.
    All paths are resolved dynamically (no hardcoded paths).
    """

    def __init__(self, config_path: Path | None = None) -> None:
        if config_path is None:
            self.config_dir = Path.home() / ".config" / "pipemix"
            self.config_path = self.config_dir / "config.toml"
        else:
            self.config_path = config_path
            self.config_dir = self.config_path.parent

        self._config_data: dict = {}
        self.load()

    def load(self) -> None:
        """Load config from disk. If not present, create a default one."""
        try:
            if not self.config_path.exists():
                self.config_dir.mkdir(parents=True, exist_ok=True)
                self.config_path.write_text(DEFAULT_CONFIG, encoding="utf-8")
                log.info("Created default config file at %s", self.config_path)

            with open(self.config_path, "rb") as f:
                self._config_data = tomllib.load(f)
            log.debug("Loaded config from %s", self.config_path)
        except Exception as e:
            log.error("Failed to load config: %s. Using empty default.", e)
            self._config_data = {
                "general": {"start_minimized": False, "autostart": False, "last_preset": ""},
                "devices": {},
                "presets": {}
            }

    def save(self) -> None:
        """Save the current configuration to disk as TOML."""
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            
            # Simple TOML serializer for our basic structure
            # python stdlib doesn't have a toml writer (tomllib is read-only)
            # Since we want zero dependencies, we write a simple formatter
            lines = []
            
            # [general]
            lines.append("[general]")
            gen = self._config_data.get("general", {})
            lines.append(f"start_minimized = {'true' if gen.get('start_minimized') else 'false'}")
            lines.append(f"autostart = {'true' if gen.get('autostart') else 'false'}")
            lines.append(f"last_preset = \"{gen.get('last_preset', '')}\"")
            lines.append("")

            # [devices]
            lines.append("[devices]")
            devices = self._config_data.get("devices", {})
            for mac, name in devices.items():
                # Escape double quotes in names
                safe_name = name.replace('"', '\\"')
                lines.append(f'"{mac}" = "{safe_name}"')
            lines.append("")

            # [presets]
            presets = self._config_data.get("presets", {})
            for key, preset in presets.items():
                lines.append(f"[presets.{key}]")
                lines.append(f"name = \"{preset.get('name', '')}\"")
                # Format list of strings
                devs = ", ".join(f'"{d}"' for d in preset.get("devices", []))
                lines.append(f"devices = [{devs}]")
                lines.append("")

            content = "\n".join(lines)
            self.config_path.write_text(content, encoding="utf-8")
            log.debug("Saved config to %s", self.config_path)
        except Exception as e:
            log.error("Failed to save config: %s", e)

    def get_general(self, key: str, default: object = None) -> object:
        return self._config_data.get("general", {}).get(key, default)

    def set_general(self, key: str, value: object) -> None:
        if "general" not in self._config_data:
            self._config_data["general"] = {}
        self._config_data["general"][key] = value

    def get_device_name(self, mac: str, default: str) -> str:
        return self._config_data.get("devices", {}).get(mac, default)

    def set_device_name(self, mac: str, name: str) -> None:
        if "devices" not in self._config_data:
            self._config_data["devices"] = {}
        self._config_data["devices"][mac] = name

    def get_preset(self, preset_id: str) -> dict | None:
        return self._config_data.get("presets", {}).get(preset_id)

    def save_preset(self, preset_id: str, name: str, devices: list[str]) -> None:
        if "presets" not in self._config_data:
            self._config_data["presets"] = {}
        self._config_data["presets"][preset_id] = {
            "name": name,
            "devices": devices
        }

    def delete_preset(self, preset_id: str) -> None:
        if "presets" in self._config_data and preset_id in self._config_data["presets"]:
            del self._config_data["presets"][preset_id]
