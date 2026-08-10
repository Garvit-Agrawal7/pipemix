"""
PipeMix — BluezResolver

Resolves a Bluetooth MAC address (stable BlueZ identity) to the current
PipeWire sink name (runtime, changes on reconnect).

Why this is a separate module:
  The MAC is what we store in config files and what BlueZ gives us.
  PipeWire generates sink names from MACs but the format can include
  a profile suffix (e.g. ".1"). This module handles that translation.

Example:
    resolver = BluezResolver()
    sink = resolver.resolve("61:C5:02:3A:59:49")
    # → "bluez_output.61_C5_02_3A_59_49.1"  (or None if not connected)
"""

from __future__ import annotations

import logging
import re
import subprocess

log = logging.getLogger(__name__)

# Matches any PipeWire BT sink name and captures the MAC portion.
# e.g. "bluez_output.61_C5_02_3A_59_49.1" → group(1) = "61_C5_02_3A_59_49"
_BT_SINK_RE = re.compile(
    r"^(bluez_output\.)([0-9A-Fa-f]{2}(?:_[0-9A-Fa-f]{2}){5})(\.\d+)$"
)


def _run_pactl_short_sinks() -> list[str]:
    """
    Run `pactl list short sinks` and return lines.
    Returns empty list on any failure.
    """
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sinks"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip().splitlines()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("pactl list short sinks failed: %s", e)
    return []


def _mac_to_underscored(mac: str) -> str:
    """'61:C5:02:3A:59:49' → '61_C5_02_3A_59_49'"""
    return mac.replace(":", "_").upper()


class BluezResolver:
    """
    Resolves Bluetooth MAC addresses ↔ PipeWire sink names.

    This is called:
      - After a connect event fires (to find the new sink name)
      - Before creating a virtual output (to validate devices are sinkable)
      - During preset application (to resolve stored MACs to live sinks)
    """

    def resolve(self, mac: str) -> str | None:
        """
        Resolve a single MAC to a PipeWire sink name.
        Returns None if no matching sink is currently available.
        """
        prefix = f"bluez_output.{_mac_to_underscored(mac)}."

        for line in _run_pactl_short_sinks():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            sink_name = parts[1].strip()
            if sink_name.startswith(prefix):
                log.debug("Resolved %s → %s", mac, sink_name)
                return sink_name

        log.debug("No sink found for %s (not connected as audio, or PipeWire not ready)", mac)
        return None

    def resolve_all(self, macs: list[str]) -> dict[str, str | None]:
        """
        Resolve multiple MACs in a single pactl call.
        More efficient than calling resolve() in a loop.

        Returns {mac: sink_name_or_None} for every input MAC.
        """
        # Build a lookup table: underscored_mac → sink_name from pactl output
        available: dict[str, str] = {}
        for line in _run_pactl_short_sinks():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            sink_name = parts[1].strip()
            m = _BT_SINK_RE.match(sink_name)
            if m:
                # m.group(2) is the MAC with underscores
                mac_underscored = m.group(2).upper()
                available[mac_underscored] = sink_name

        result: dict[str, str | None] = {}
        for mac in macs:
            key = _mac_to_underscored(mac)
            resolved = available.get(key)
            result[mac] = resolved
            if resolved:
                log.debug("Resolved %s → %s", mac, resolved)
            else:
                log.debug("No sink for %s", mac)

        return result

    def get_all_bt_sinks(self) -> dict[str, str]:
        """
        Return all Bluetooth sinks currently visible in PipeWire.
        Returns {mac: sink_name} for every BT sink found.
        Useful for initial device discovery when BlueZ events aren't available yet.
        """
        result: dict[str, str] = {}
        for line in _run_pactl_short_sinks():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            sink_name = parts[1].strip()
            m = _BT_SINK_RE.match(sink_name)
            if m:
                mac = m.group(2).replace("_", ":").upper()
                result[mac] = sink_name
                log.debug("Found BT sink: %s → %s", mac, sink_name)
        return result
