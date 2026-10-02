"""Bluetooth battery levels, the macOS stand-in for BlueZ's Battery1.

There is no public API for another device's battery, but System Information
has it: `system_profiler SPBluetoothDataType -json` reports
device_batteryLevelMain (speakers, headsets) or Left/Right/Case (earbuds)
for every connected device that publishes one. It takes a second or two, so
the Controller calls `read()` off the UI's path, about once a minute.

A Bluetooth output's Core Audio UID carries its address
("AC-BF-71-3E-6D-71:output"), which is how a level finds its device.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess

log = logging.getLogger(__name__)

_MAC = re.compile(r"([0-9A-Fa-f]{2}[-:]){5}[0-9A-Fa-f]{2}")


def address_of(device_uid: str) -> str | None:
    """The Bluetooth address inside a Core Audio UID, normalised, or None."""
    m = _MAC.search(device_uid)
    return re.sub(r"[^0-9a-f]", "", m.group(0).lower()) if m else None


def _percent(value) -> int | None:
    m = re.match(r"\s*(\d+)", str(value))
    return int(m.group(1)) if m else None


def level_of(info: dict) -> int | None:
    """One number for a device: its main battery, else the lower earbud
    (the one that will die first). The case does not count."""
    main = _percent(info.get("device_batteryLevelMain", ""))
    if main is not None:
        return main
    buds = [_percent(info.get(k, "")) for k in ("device_batteryLevelLeft", "device_batteryLevelRight")]
    buds = [b for b in buds if b is not None]
    if buds:
        return min(buds)
    for key, value in info.items():
        if key.startswith("device_batteryLevel") and "Case" not in key:
            pct = _percent(value)
            if pct is not None:
                return pct
    return None


def parse(report: dict) -> dict[str, int]:
    """{normalised address: percent} from system_profiler's JSON."""
    out: dict[str, int] = {}
    for section in report.get("SPBluetoothDataType", []):
        for entry in section.get("device_connected", []) or []:
            for _name, info in entry.items():
                address = info.get("device_address")
                level = level_of(info)
                if address and level is not None:
                    out[re.sub(r"[^0-9a-f]", "", address.lower())] = level
    return out


def read(timeout: float = 15.0) -> dict[str, int]:
    """Battery levels of connected Bluetooth devices. Never raises."""
    try:
        res = subprocess.run(
            ["/usr/sbin/system_profiler", "SPBluetoothDataType", "-json"],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
        return parse(json.loads(res.stdout or "{}"))
    except Exception as e:
        log.debug("Could not read Bluetooth battery levels: %s", e)
        return {}
