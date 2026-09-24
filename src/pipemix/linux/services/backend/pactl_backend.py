"""
PipeMix — the pactl backend.

Talks to PipeWire through its PulseAudio compatibility layer. Every pactl call
in the app goes through here; the Controller and UI never shell out themselves.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from typing import Callable

from pipemix.models import AudioDevice, DeviceKind, VirtualSink, sink_to_mac
from pipemix.linux.services.backend import BackendError, BackendHealth, BackendStatus

log = logging.getLogger(__name__)

# Delay the slowest leg gets; every other leg adds on top of it to line up with
# it (see set_legs). High enough to survive a Bluetooth hiccup — tune by ear.
LOOPBACK_LATENCY_MS = 60


def _load(args: list[str]) -> int | None:
    """load-module → its module id, or None with the reason logged."""
    rc, out, err = _run(["pactl", "load-module", *args])
    if rc != 0:
        log.error("load-module %s failed: %s", args[0], err.strip() or f"exit {rc}")
        return None
    try:
        return int(out.strip())
    except ValueError:
        log.error("load-module %s returned %r", args[0], out.strip())
        return None


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


def _event_kind(line: str) -> str | None:
    """
    Classify one `pactl subscribe` line, or None to ignore it.

    Every pactl call this app makes shows up here too, as a client event —
    ignored, or watch() would retrigger itself forever. Sink volume changes
    are also ignored; only a sink appearing or disappearing means hotplug.
    """
    if " on sink-input #" in line:
        return "streams"
    if " on sink #" in line and "'change'" not in line:
        return "sinks"
    return None


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

    def __init__(self) -> None:
        # Set here, not in watch(): a stop() that lands before the watch
        # thread starts must still keep it from running.
        self._stopped = False
        self._proc: subprocess.Popen | None = None

    def watch(self, on_change: Callable[[str], None]) -> None:
        """Blocks, running `pactl subscribe` and reporting each classified line."""
        while not self._stopped:
            try:
                self._proc = subprocess.Popen(
                    ["pactl", "subscribe"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                )
            except FileNotFoundError:
                return
            for line in self._proc.stdout:
                kind = _event_kind(line)
                if kind:
                    on_change(kind)
            if self._stopped:
                return
            log.warning("pactl subscribe exited — restarting.")
            time.sleep(1)
            on_change("sinks")
            on_change("streams")  # catch up on anything missed while it was down

    def unwatch(self) -> None:
        self._stopped = True
        if self._proc:
            self._proc.terminate()

    def health(self) -> BackendStatus:
        """Never raises."""
        rc, out, err = _run(["pactl", "info"])

        if rc != 0:
            log.debug("pactl info failed: %s", err.strip() or f"exit {rc}")
            return BackendStatus(
                BackendHealth.UNAVAILABLE,
                "PipeWire is not running, or pactl is not installed.",
            )
        if "PipeWire" not in out:
            return BackendStatus(
                BackendHealth.DEGRADED,
                "pactl is connected, but PipeWire was not detected. "
                "Some features may not work.",
            )
        return BackendStatus(BackendHealth.OK, "PipeWire is running.")

    def list_outputs(self) -> list[AudioDevice]:
        rc, out, err = _run(["pactl", "-f", "json", "list", "sinks"])
        if rc != 0:
            raise BackendError(f"pactl list sinks failed: {err.strip()}")

        devices = []
        for s in json.loads(out):
            sink = s.get("name", "")
            props = s.get("properties", {})
            if not sink or sink.startswith("pipemix_"):
                continue
            if props.get("node.virtual") == "true" or props.get("device.bus") == "virtual":
                continue

            kind = _kind(sink)
            chan = next(iter(s.get("volume", {}).values()), None)
            devices.append(AudioDevice(
                id=sink_to_mac(sink) or sink,
                name=s.get("description") or self._fallback_name(sink, kind),
                sink=sink,
                kind=kind,
                connected=True,
                volume=int(chan["value_percent"].rstrip("%")) if chan else 50,
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

    def _latencies(self) -> dict[str, int]:
        """{sink name: ns the device adds, its latency offset included}. Empty on failure."""
        rc, out, _ = _run(["pw-dump"])
        if rc != 0:
            return {}
        try:
            lat = {}
            for obj in json.loads(out):
                if not obj.get("type", "").endswith("Node"):
                    continue
                info = obj.get("info") or {}
                name = (info.get("props") or {}).get("node.name")
                for p in (info.get("params") or {}).get("Latency") or []:
                    if name and p.get("direction") == "Input":
                        # ponytail: quantum term dropped; every sink reports 1 quantum, so it cancels
                        lat[name] = p["minNs"] + p["minRate"] * 1_000_000_000 // 48000
            return lat
        except Exception:
            log.debug("pw-dump output unparsable")
            return {}

    def _sink_names(self) -> dict[str, str]:
        """{sink index: sink name}. Empty on failure."""
        rc, out, _ = _run(["pactl", "list", "short", "sinks"])
        if rc != 0:
            return {}
        rows = (line.split("\t") for line in out.splitlines())
        return {r[0].strip(): r[1].strip() for r in rows if len(r) >= 2}

    def resolve_bt_sink(self, mac: str) -> str | None:
        mac = mac.upper()
        for sink in self._sink_names().values():
            if sink_to_mac(sink) == mac:
                log.debug("Resolved %s -> %s", mac, sink)
                return sink
        return None

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
        """
        A hub sink for the session, with one loopback out to each chosen output.

        Not module-combine-sink: its slave list is fixed at load time, so every
        toggle meant destroying and rebuilding it, and under that churn it
        silently stops attaching one of the slaves — a live sink that is deaf on
        one device. Loopbacks are independent, so a toggle adds or drops exactly
        one of them and leaves the rest playing.
        """
        if not any(d.sink for d in devices):
            raise BackendError(
                "None of the selected devices have a resolvable PipeWire sink name. "
                "Are they connected?"
            )

        name = VirtualSink.make_name()
        module = _load([
            "module-null-sink",
            f"sink_name={name}",
            "sink_properties=device.description=PipeMix\\ Combined",
        ])
        if module is None:
            raise BackendError("Failed to create the PipeMix sink.")

        sink = VirtualSink(module, name)
        log.info("Created %s", sink)
        self.set_legs(sink, devices)
        return sink

    def set_legs(self, sink: VirtualSink, devices: list[AudioDevice]) -> None:
        """Make the hub feed exactly these outputs, touching only what changed."""
        wanted = {d.sink for d in devices if d.sink}
        lat = self._latencies()
        # High-water mark: a slow device dropping out doesn't pull the rest forward,
        # so a Bluetooth flap never glitches the outputs that stayed.
        sink.slowest = max([sink.slowest, *(lat.get(t, 0) for t in wanted)])

        for target in wanted:
            if not lat and target in sink.delays:
                continue  # pw-dump failed transiently; don't disturb an already-aligned leg
            ms = LOOPBACK_LATENCY_MS + (sink.slowest - lat.get(target, 0)) // 1_000_000
            if sink.delays.get(target) == ms:
                continue
            module = _load([
                "module-loopback",
                f"source={sink.name}.monitor",
                f"sink={target}",
                f"latency_msec={ms}",
                f"sink_input_properties=media.name={sink.name}",
            ])
            if module is None:
                continue  # the old leg, if any, keeps playing
            if target in sink.legs:
                self._unload(sink.legs[target])  # make before break
            sink.legs[target], sink.delays[target] = module, ms
            log.info("%s now feeds %s at %dms", sink.name, target, ms)

        for target in set(sink.legs) - wanted:
            self._unload(sink.legs.pop(target))
            sink.delays.pop(target, None)
            log.info("%s no longer feeds %s", sink.name, target)

    def _unload(self, module: int) -> None:
        rc, _, err = _run(["pactl", "unload-module", str(module)])
        if rc != 0:
            log.debug("unload-module %d failed (already gone?): %s", module, err.strip())

    def destroy_sink(self, sink: VirtualSink) -> None:
        """Safe to call when the sink is already gone — never raises."""
        log.info("Destroying %s", sink)
        for module in list(sink.legs.values()):
            self._unload(module)
        sink.legs.clear()
        self._unload(sink.module)

    def find_orphans(self) -> list[VirtualSink]:
        """Modules a previous run left behind. Never raises."""
        rc, out, _ = _run(["pactl", "list", "short", "modules"])
        if rc != 0:
            log.warning("Could not list modules during orphan scan.")
            return []

        orphans = []
        for line in out.splitlines():
            # The hub carries its own name; each loopback carries it too, in the
            # media.name we stamp on them. One scan over the whole line catches
            # both, and skips the continuation lines of multi-line module args.
            found = re.search(r"pipemix_[0-9a-f]+", line)
            if not found:
                continue
            try:
                module = int(line.split("\t", 1)[0])
            except ValueError:
                continue
            orphans.append(VirtualSink(module, found.group()))
            log.warning("Found orphaned module %d for %s", module, found.group())
        return orphans

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
