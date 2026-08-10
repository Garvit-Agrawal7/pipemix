"""
PipeMix — PactlBackend

Audio backend that uses `pactl` subprocess calls to talk to PipeWire
via its PulseAudio compatibility layer.

Why subprocess instead of a Python library:
  - No external dependencies beyond `pactl` (already present on any PipeWire system).
  - Easily replaceable: swap this file for NativePipeWireBackend later without
    changing the Controller or UI.

All public methods follow the AudioBackend contract defined in __init__.py.
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import TYPE_CHECKING

from pipemix.models import (
    AudioDevice,
    AudioStream,
    DeviceCapabilities,
    DeviceKind,
    VirtualSink,
)
from pipemix.services.backend import (
    AudioBackend,
    BackendError,
    BackendHealth,
    BackendStatus,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Matches sink names like: bluez_output.61_C5_02_3A_59_49.1
_BT_SINK_RE = re.compile(
    r"^bluez_output\.([0-9A-Fa-f]{2}_){5}[0-9A-Fa-f]{2}\."
)

# Captures the MAC portion: bluez_output.61_C5_02_3A_59_49.1 → "61_C5_02_3A_59_49"
_BT_MAC_RE = re.compile(
    r"bluez_output\.([0-9A-Fa-f]{2}(?:_[0-9A-Fa-f]{2}){5})\."
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run(args: list[str]) -> tuple[int, str, str]:
    """
    Run a command and return (returncode, stdout, stderr).
    Never raises — on timeout or missing command, returns rc=-1.
    """
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        log.error("Command timed out: %s", " ".join(args))
        return -1, "", "Command timed out"
    except FileNotFoundError:
        log.error("Command not found: %s", args[0])
        return -1, "", f"Command not found: {args[0]}"


def _sink_name_to_mac(sink_name: str) -> str | None:
    """
    Extract Bluetooth MAC address from a PipeWire sink name.
    'bluez_output.61_C5_02_3A_59_49.1' → '61:C5:02:3A:59:49'
    Returns None if not a Bluetooth sink name.
    """
    m = _BT_MAC_RE.search(sink_name)
    if m:
        return m.group(1).replace("_", ":").upper()
    return None


def _classify_kind(sink_name: str) -> DeviceKind:
    """Determine DeviceKind from a PipeWire sink name."""
    name = sink_name.lower()
    if name.startswith("bluez_"):
        return DeviceKind.BLUETOOTH
    if "hdmi" in name or "iec958" in name or "dp-" in name:
        return DeviceKind.HDMI
    if "usb" in name:
        return DeviceKind.USB
    return DeviceKind.BUILTIN


def _capabilities_for(kind: DeviceKind) -> DeviceCapabilities:
    mapping = {
        DeviceKind.BLUETOOTH: DeviceCapabilities.for_bluetooth,
        DeviceKind.HDMI:      DeviceCapabilities.for_hdmi,
        DeviceKind.USB:       DeviceCapabilities.for_usb,
        DeviceKind.BUILTIN:   DeviceCapabilities.for_builtin,
    }
    factory = mapping.get(kind, DeviceCapabilities.for_builtin)
    return factory()


def _parse_sinks_detailed(output: str) -> list[dict]:
    """
    Parse the output of `pactl list sinks` into a list of dicts.
    Each dict has keys: name, description, state, owner_module, properties.
    """
    sinks: list[dict] = []
    current: dict = {}
    in_properties = False

    for raw_line in output.splitlines():
        line = raw_line.strip()

        if raw_line.startswith("Sink #"):
            if current:
                sinks.append(current)
            current = {"properties": {}}
            in_properties = False

        elif line.startswith("Properties:"):
            in_properties = True

        elif in_properties and line and "=" in line:
            parts = line.split("=", 1)
            k = parts[0].strip()
            v = parts[1].strip().strip('"')
            current["properties"][k] = v

        elif line.startswith("Name:"):
            current["name"] = line.split(":", 1)[1].strip()

        elif line.startswith("Description:"):
            current["description"] = line.split(":", 1)[1].strip()

        elif line.startswith("State:"):
            current["state"] = line.split(":", 1)[1].strip()

        elif line.startswith("Owner Module:"):
            try:
                current["owner_module"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass

    if current:
        sinks.append(current)

    return sinks


def _parse_sink_inputs(output: str) -> list[AudioStream]:
    """Parse `pactl list short sink-inputs` into AudioStream list."""
    streams: list[AudioStream] = []
    for line in output.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            stream_id = int(parts[0].strip())
            sink_name = parts[1].strip()
            client_id = parts[2].strip()
            
            # Skip virtual internal helper streams (which have no owning client)
            if client_id == "-":
                continue
                
            name = f"Stream {stream_id}"
            streams.append(AudioStream(
                stream_id=stream_id,
                name=name,
                sink_name=sink_name,
            ))
        except (ValueError, IndexError):
            log.debug("Skipping unparseable sink-input line: %r", line)
    return streams


# ---------------------------------------------------------------------------
# PactlBackend
# ---------------------------------------------------------------------------

class PactlBackend(AudioBackend):
    """
    AudioBackend implementation using `pactl` subprocess calls.

    Talks to PipeWire via the PulseAudio compatibility layer.
    Easily swappable for a native PipeWire backend without any UI changes.
    """

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health_check(self) -> BackendStatus:
        """
        Check whether pactl can reach PipeWire.
        Never raises.
        """
        rc, stdout, stderr = _run(["pactl", "info"])

        if rc != 0:
            return BackendStatus(
                health=BackendHealth.UNAVAILABLE,
                message="PipeWire is not running, or pactl is not installed.",
                details=stderr.strip() or f"pactl exited with code {rc}",
            )

        if "PipeWire" not in stdout:
            return BackendStatus(
                health=BackendHealth.DEGRADED,
                message=(
                    "pactl is connected, but PipeWire was not detected. "
                    "Some features may not work."
                ),
                details=stdout[:300],
            )

        log.debug("PactlBackend health check: OK")
        return BackendStatus(
            health=BackendHealth.OK,
            message="PipeWire is running.",
        )

    # ------------------------------------------------------------------
    # Device enumeration
    # ------------------------------------------------------------------

    def list_outputs(self) -> list[AudioDevice]:
        """
        Return all available audio output devices, excluding PipeMix virtual sinks.
        Uses `pactl list sinks` for human-readable descriptions.
        """
        rc, stdout, stderr = _run(["pactl", "list", "sinks"])
        if rc != 0:
            raise BackendError(f"pactl list sinks failed: {stderr.strip()}")

        raw_sinks = _parse_sinks_detailed(stdout)
        devices: list[AudioDevice] = []

        for s in raw_sinks:
            sink_name = s.get("name", "")
            if not sink_name:
                continue

            # Never expose our own virtual sinks as selectable outputs
            if sink_name.startswith("pipemix_"):
                continue

            # Skip any custom virtual combine/loopback sinks
            props = s.get("properties", {})
            if props.get("node.virtual") == "true" or props.get("device.bus") == "virtual":
                continue

            # Exclude dual_bt specifically
            if sink_name == "dual_bt":
                continue

            kind = _classify_kind(sink_name)
            caps = _capabilities_for(kind)

            # Stable device ID
            if kind == DeviceKind.BLUETOOTH:
                device_id = _sink_name_to_mac(sink_name) or sink_name
            else:
                # For non-BT, the sink name itself is stable (hardware path)
                device_id = sink_name

            # Human-readable name: prefer PipeWire's Description field
            description = s.get("description", "")
            display_name = description if description else self._fallback_name(sink_name, kind)

            device = AudioDevice(
                device_id=device_id,
                display_name=display_name,
                sink_name=sink_name,
                kind=kind,
                capabilities=caps,
                is_connected=True,
            )
            devices.append(device)
            log.debug("Discovered output: %r", device)

        log.info("Found %d output(s)", len(devices))
        return devices

    def _fallback_name(self, sink_name: str, kind: DeviceKind) -> str:
        """Generate a display name when PipeWire has no description."""
        if kind == DeviceKind.BLUETOOTH:
            mac = _sink_name_to_mac(sink_name)
            return f"Bluetooth Device ({mac})" if mac else "Bluetooth Device"
        if kind == DeviceKind.HDMI:
            return "HDMI Output"
        if kind == DeviceKind.USB:
            return "USB Audio Device"
        return "Built-in Audio"

    # ------------------------------------------------------------------
    # Virtual sink lifecycle
    # ------------------------------------------------------------------

    def create_virtual_output(self, outputs: list[AudioDevice]) -> VirtualSink:
        """
        Load module-combine-sink with the given outputs as slaves.
        Returns a VirtualSink containing the module_id needed for cleanup.
        """
        if not outputs:
            raise BackendError("Cannot create a virtual output with no devices selected.")

        sink_names = [d.sink_name for d in outputs if d.sink_name]
        if not sink_names:
            raise BackendError(
                "None of the selected devices have a resolvable PipeWire sink name. "
                "Are they connected?"
            )

        sink_name = VirtualSink.make_name()
        slaves_arg = ",".join(sink_names)

        cmd = [
            "pactl", "load-module", "module-combine-sink",
            f"sink_name={sink_name}",
            f"slaves={slaves_arg}",
            "sink_properties=device.description=PipeMix\\ Combined",
        ]

        log.info(
            "Creating virtual sink %s  slaves=[%s]",
            sink_name,
            ", ".join(sink_names),
        )
        rc, stdout, stderr = _run(cmd)

        if rc != 0:
            raise BackendError(
                f"Failed to create combined sink: {stderr.strip() or 'unknown error'}"
            )

        raw = stdout.strip()
        try:
            module_id = int(raw)
        except ValueError:
            raise BackendError(
                f"pactl load-module returned unexpected output: {raw!r}"
            )
        virtual_sink = VirtualSink(module_id=module_id, sink_name=sink_name)
        log.info("Created %r", virtual_sink)
        return virtual_sink

    def move_streams(self, target_sink_name: str, exclude_stream_ids: list[int] | None = None) -> None:
        """Move all active sink-inputs to the target sink, optionally excluding specific stream IDs."""
        rc, stdout, _ = _run(["pactl", "list", "short", "sink-inputs"])
        if rc != 0:
            log.warning("Could not list sink-inputs — skipping stream routing.")
            return

        streams = _parse_sink_inputs(stdout)
        if not streams:
            log.info("No active streams to route.")
            return

        excludes = set(exclude_stream_ids or [])
        moved = 0
        for stream in streams:
            if stream.stream_id in excludes:
                log.debug("Skipping excluded stream %d", stream.stream_id)
                continue

            rc2, _, stderr2 = _run([
                "pactl", "move-sink-input",
                str(stream.stream_id),
                target_sink_name,
            ])
            if rc2 == 0:
                moved += 1
                log.debug("Moved stream %d → %s", stream.stream_id, target_sink_name)
            else:
                log.warning(
                    "Failed to move stream %d: %s",
                    stream.stream_id,
                    stderr2.strip(),
                )

        log.info("Moved %d/%d stream(s) to %s", moved, len(streams), target_sink_name)

    def move_stream(self, stream_id: int, target_sink_name: str) -> None:
        """Move a specific sink-input to a target sink."""
        log.info("Moving stream %d → %s", stream_id, target_sink_name)
        rc, _, stderr = _run([
            "pactl", "move-sink-input",
            str(stream_id),
            target_sink_name,
        ])
        if rc != 0:
            raise BackendError(
                f"Failed to move stream {stream_id} to {target_sink_name}: {stderr.strip()}"
            )

    def destroy_virtual_output(self, sink: VirtualSink) -> None:
        """
        Unload the combined sink module.
        Safe to call even if the sink is already gone — does not raise.
        """
        log.info("Destroying %r", sink)
        rc, _, stderr = _run(["pactl", "unload-module", str(sink.module_id)])
        if rc != 0:
            # Module already gone (crash recovery, manual removal, etc.) — not an error.
            log.debug(
                "unload-module %d returned non-zero (may already be gone): %s",
                sink.module_id,
                stderr.strip(),
            )

    def get_sink_volume(self, sink_name: str) -> int:
        """Get current volume (0-100) of a sink."""
        rc, stdout, stderr = _run(["pactl", "get-sink-volume", sink_name])
        if rc != 0:
            log.warning("Failed to get volume for %s: %s", sink_name, stderr.strip())
            return 100
        
        # Look for percentage matches (e.g. 100%, 85%)
        import re
        matches = re.findall(r"(\d+)%", stdout)
        if matches:
            try:
                return int(matches[0])
            except ValueError:
                pass
        return 100

    def set_sink_volume(self, sink_name: str, volume_percent: int) -> None:
        """Set volume (0-100) of a sink."""
        vol = max(0, min(100, volume_percent))
        rc, _, stderr = _run(["pactl", "set-sink-volume", sink_name, f"{vol}%"])
        if rc != 0:
            raise BackendError(
                f"Failed to set volume of {sink_name} to {vol}%: {stderr.strip()}"
            )
        log.debug("Set volume of %s to %d%%", sink_name, vol)

    def set_sink_mute(self, sink_name: str, mute: bool) -> None:
        """Set the mute state of a sink."""
        state = "1" if mute else "0"
        rc, _, stderr = _run(["pactl", "set-sink-mute", sink_name, state])
        if rc != 0:
            raise BackendError(
                f"Failed to set mute state for {sink_name} to {mute}: {stderr.strip()}"
            )
        log.debug("Set mute state of %s to %s", sink_name, mute)

    # ------------------------------------------------------------------
    # Crash recovery
    # ------------------------------------------------------------------

    def find_orphaned_virtual_sinks(self) -> list[VirtualSink]:
        """
        Find any pipemix_* sinks in PipeWire.
        Called during startup crash recovery.
        Never raises.
        """
        rc, stdout, _ = _run(["pactl", "list", "sinks"])
        if rc != 0:
            log.warning("Could not list sinks during orphan scan.")
            return []

        orphans: list[VirtualSink] = []
        try:
            raw_sinks = _parse_sinks_detailed(stdout)
            for s in raw_sinks:
                name = s.get("name", "")
                if not name.startswith("pipemix_"):
                    continue
                module_id = s.get("owner_module")
                if module_id is not None:
                    orphan = VirtualSink(module_id=module_id, sink_name=name)
                    log.warning("Found orphaned sink: %r", orphan)
                    orphans.append(orphan)
        except Exception as e:
            log.error("Error during orphan scan: %s", e)

        return orphans

    # ------------------------------------------------------------------
    # Default sink
    # ------------------------------------------------------------------

    def set_default_sink(self, sink_name: str) -> None:
        """Set the system default audio output."""
        rc, _, stderr = _run(["pactl", "set-default-sink", sink_name])
        if rc != 0:
            raise BackendError(
                f"Failed to set default sink to {sink_name!r}: {stderr.strip()}"
            )
        log.info("Default sink set to %r", sink_name)
