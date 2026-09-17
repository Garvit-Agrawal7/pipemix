"""
PipeMix — the pactl backend.

Talks to PipeWire through its PulseAudio compatibility layer. Every pactl call
in the app goes through here; the Controller and UI never shell out themselves.
"""

from __future__ import annotations

import logging
import re
import subprocess

from pipemix.models import AudioDevice, DeviceKind, VirtualSink, sink_to_mac
from pipemix.services.backend import BackendError, BackendHealth, BackendStatus

log = logging.getLogger(__name__)


def _run(args: list[str]) -> tuple[int, str, str]:
    """Run a command → (rc, stdout, stderr). Never raises; rc is -1 on failure."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=5)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        log.error("Command timed out: %s", " ".join(args))
        return -1, "", "Command timed out"
    except FileNotFoundError:
        log.error("Command not found: %s", args[0])
        return -1, "", f"Command not found: {args[0]}"


def _kind(sink: str) -> DeviceKind:
    name = sink.lower()
    if name.startswith("bluez_"):
        return DeviceKind.BLUETOOTH
    if "hdmi" in name or "iec958" in name or "dp-" in name:
        return DeviceKind.HDMI
    if "usb" in name:
        return DeviceKind.USB
    return DeviceKind.BUILTIN


def _parse_sinks(output: str) -> list[dict]:
    """`pactl list sinks` → one dict per sink (name, description, owner_module, props)."""
    sinks: list[dict] = []
    cur: dict = {}
    in_props = False

    for raw in output.splitlines():
        line = raw.strip()

        if raw.startswith("Sink #"):
            if cur:
                sinks.append(cur)
            cur, in_props = {"props": {}}, False

        elif line.startswith("Properties:"):
            in_props = True

        elif in_props and "=" in line:
            k, v = line.split("=", 1)
            cur["props"][k.strip()] = v.strip().strip('"')

        elif line.startswith("Name:"):
            cur["name"] = line.split(":", 1)[1].strip()

        elif line.startswith("Description:"):
            cur["desc"] = line.split(":", 1)[1].strip()

        elif line.startswith("Owner Module:"):
            try:
                cur["module"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass

    if cur:
        sinks.append(cur)
    return sinks


def _parse_inputs(output: str) -> list[dict]:
    """`pactl list sink-inputs` → one flat dict per stream, keyed as pactl names them."""
    streams: list[dict] = []
    cur: dict = {}

    for raw in output.splitlines():
        line = raw.strip()

        if raw.startswith("Sink Input #"):
            if cur:
                streams.append(cur)
            cur = {"id": int(raw.split("#")[1].strip())}

        elif not cur:
            continue

        elif line.startswith("Sink:"):
            cur["sink_index"] = line.split(":", 1)[1].strip()

        elif line.startswith("Mute:"):
            cur["mute"] = line.split(":", 1)[1].strip() == "yes"

        elif "=" in line:
            k, v = line.split("=", 1)
            cur[k.strip()] = v.strip().strip('"')

    if cur:
        streams.append(cur)
    return streams


class PactlBackend:

    def health(self) -> BackendStatus:
        """Never raises."""
        rc, out, err = _run(["pactl", "info"])

        if rc != 0:
            return BackendStatus(
                BackendHealth.UNAVAILABLE,
                "PipeWire is not running, or pactl is not installed.",
                err.strip() or f"pactl exited with code {rc}",
            )
        if "PipeWire" not in out:
            return BackendStatus(
                BackendHealth.DEGRADED,
                "pactl is connected, but PipeWire was not detected. "
                "Some features may not work.",
                out[:300],
            )
        return BackendStatus(BackendHealth.OK, "PipeWire is running.")

    def list_outputs(self) -> list[AudioDevice]:
        rc, out, err = _run(["pactl", "list", "sinks"])
        if rc != 0:
            raise BackendError(f"pactl list sinks failed: {err.strip()}")

        devices = []
        for s in _parse_sinks(out):
            sink = s.get("name", "")
            props = s.get("props", {})
            if not sink or sink.startswith("pipemix_"):
                continue
            if props.get("node.virtual") == "true" or props.get("device.bus") == "virtual":
                continue

            kind = _kind(sink)
            devices.append(AudioDevice(
                id=sink_to_mac(sink) or sink,
                name=s.get("desc") or self._fallback_name(sink, kind),
                sink=sink,
                kind=kind,
                connected=True,
            ))

        log.info("Found %d output(s)", len(devices))
        return devices

    def _fallback_name(self, sink: str, kind: DeviceKind) -> str:
        if kind == DeviceKind.BLUETOOTH:
            mac = sink_to_mac(sink)
            return f"Bluetooth Device ({mac})" if mac else "Bluetooth Device"
        if kind == DeviceKind.HDMI:
            return "HDMI Output"
        if kind == DeviceKind.USB:
            return "USB Audio Device"
        return "Built-in Audio"

    def _sink_names(self) -> dict[str, str]:
        """{sink index: sink name}. Empty on failure."""
        rc, out, _ = _run(["pactl", "list", "short", "sinks"])
        if rc != 0:
            return {}
        rows = (line.split("\t") for line in out.splitlines())
        return {r[0].strip(): r[1].strip() for r in rows if len(r) >= 2}

    def resolve_bt_sinks(self, macs: list[str]) -> dict[str, str | None]:
        """{mac: sink name or None} for every given MAC, in one pactl call."""
        found = {}
        for sink in self._sink_names().values():
            mac = sink_to_mac(sink)
            if mac:
                found[mac] = sink

        resolved = {mac: found.get(mac.upper()) for mac in macs}
        log.debug("Resolved BT sinks: %s", resolved)
        return resolved

    def resolve_bt_sink(self, mac: str) -> str | None:
        return self.resolve_bt_sinks([mac])[mac]

    def list_streams(self) -> list[dict]:
        """
        Application streams now playing: {"id", "name", "sink", "mute"}.
        Our own combine-sink plumbing is filtered out.
        """
        rc, out, err = _run(["pactl", "list", "sink-inputs"])
        if rc != 0:
            raise BackendError(f"pactl list sink-inputs failed: {err.strip()}")

        names = self._sink_names()
        streams = []
        for s in _parse_inputs(out):
            if s.get("media.class", "Stream/Output/Audio") != "Stream/Output/Audio":
                continue

            app = s.get("application.name", "Application")
            media = s.get("media.name", "")
            if app == "pipemix" or "pipemix_" in media:
                continue

            index = s.get("sink_index", "")
            streams.append({
                "id":   s["id"],
                "name": f"{app} ({media})" if media and media not in ("Playback", app) else app,
                "sink": names.get(index, index),
                "mute": s.get("mute", False),
            })
        return streams

    def move_streams(self, target: str, exclude: list[int] | None = None) -> None:
        try:
            streams = self.list_streams()
        except BackendError as e:
            log.warning("Could not list streams — skipping routing: %s", e)
            return

        if not streams:
            log.info("No active streams to route.")
            return

        skip = set(exclude or [])
        moved = 0
        for s in streams:
            if s["id"] in skip:
                continue
            rc, _, err = _run(["pactl", "move-sink-input", str(s["id"]), target])
            if rc == 0:
                moved += 1
            else:
                log.warning("Failed to move stream %d: %s", s["id"], err.strip())

        log.info("Moved %d/%d stream(s) to %s", moved, len(streams), target)

    def move_stream(self, stream_id: int, target: str) -> None:
        log.info("Moving stream %d → %s", stream_id, target)
        rc, _, err = _run(["pactl", "move-sink-input", str(stream_id), target])
        if rc != 0:
            raise BackendError(f"Failed to move stream {stream_id} to {target}: {err.strip()}")

    def set_stream_mute(self, stream_id: int, mute: bool) -> None:
        rc, _, err = _run(["pactl", "set-sink-input-mute", str(stream_id), "1" if mute else "0"])
        if rc != 0:
            raise BackendError(f"Failed to mute stream {stream_id}: {err.strip()}")

    def create_sink(self, devices: list[AudioDevice]) -> VirtualSink:
        """Combine the given outputs into one virtual sink."""
        slaves = [d.sink for d in devices if d.sink]
        if not slaves:
            raise BackendError(
                "None of the selected devices have a resolvable PipeWire sink name. "
                "Are they connected?"
            )

        name = VirtualSink.make_name()
        log.info("Creating virtual sink %s  slaves=[%s]", name, ", ".join(slaves))

        rc, out, err = _run([
            "pactl", "load-module", "module-combine-sink",
            f"sink_name={name}",
            f"slaves={','.join(slaves)}",
            f"sinks={','.join(slaves)}",
            "sink_properties=device.description=PipeMix\\ Combined",
        ])
        if rc != 0:
            raise BackendError(f"Failed to create combined sink: {err.strip() or 'unknown error'}")

        try:
            module = int(out.strip())
        except ValueError:
            raise BackendError(f"pactl load-module returned unexpected output: {out.strip()!r}")

        sink = VirtualSink(module, name)
        log.info("Created %s", sink)
        return sink

    def destroy_sink(self, sink: VirtualSink) -> None:
        """Safe to call when the sink is already gone — never raises."""
        log.info("Destroying %s", sink)
        rc, _, err = _run(["pactl", "unload-module", str(sink.module)])
        if rc != 0:
            log.debug("unload-module %d failed (already gone?): %s", sink.module, err.strip())

    def find_orphans(self) -> list[VirtualSink]:
        """Our sinks left behind by a crash. Never raises."""
        rc, out, _ = _run(["pactl", "list", "sinks"])
        if rc != 0:
            log.warning("Could not list sinks during orphan scan.")
            return []

        orphans = []
        try:
            for s in _parse_sinks(out):
                name, module = s.get("name", ""), s.get("module")
                if name.startswith("pipemix_") and module is not None:
                    orphans.append(VirtualSink(module, name))
                    log.warning("Found orphaned sink: %s", name)
        except Exception as e:
            log.error("Error during orphan scan: %s", e)
        return orphans

    def get_volume(self, sink: str) -> int:
        """0-100, or 100 if it cannot be read."""
        rc, out, err = _run(["pactl", "get-sink-volume", sink])
        if rc != 0:
            log.warning("Failed to get volume for %s: %s", sink, err.strip())
            return 100
        m = re.search(r"(\d+)%", out)
        return int(m.group(1)) if m else 100

    def set_volume(self, sink: str, volume: int) -> None:
        vol = max(0, min(100, volume))
        rc, _, err = _run(["pactl", "set-sink-volume", sink, f"{vol}%"])
        if rc != 0:
            raise BackendError(f"Failed to set volume of {sink} to {vol}%: {err.strip()}")

    def set_mute(self, sink: str, mute: bool) -> None:
        rc, _, err = _run(["pactl", "set-sink-mute", sink, "1" if mute else "0"])
        if rc != 0:
            raise BackendError(f"Failed to set mute state for {sink}: {err.strip()}")

    def get_default(self) -> str | None:
        rc, out, _ = _run(["pactl", "info"])
        if rc != 0:
            return None
        for line in out.splitlines():
            if line.startswith("Default Sink:"):
                return line.split(":", 1)[1].strip()
        return None

    def set_default(self, sink: str) -> None:
        rc, _, err = _run(["pactl", "set-default-sink", sink])
        if rc != 0:
            raise BackendError(f"Failed to set default sink to {sink!r}: {err.strip()}")
        log.info("Default sink set to %r", sink)
