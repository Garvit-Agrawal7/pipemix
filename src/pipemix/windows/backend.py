"""
PipeMix — the WASAPI backend.

Same surface as `PactlBackend`, so the Controller calls either one identically.
Underneath, there is no PipeWire hub sink — `wasapi/engine.py` is the fan-out,
capturing from one source and writing to N render legs. Where a PipeWire
concept has no Windows meaning the method stays, made trivially correct rather
than deleted, so the Controller never has to special-case the platform.
"""

from __future__ import annotations

import logging

from pipemix.models import AudioDevice, VirtualSink
from pipemix.linux.services.backend import BackendError, BackendHealth, BackendStatus
from pipemix.windows.wasapi.devices import PKEY_FriendlyName, default_output_id, list_outputs
from pipemix.windows.wasapi.engine import Engine
from pipemix.windows.wasapi import policy as _policy
from pipemix.windows.wasapi import sessions as _sessions
from pipemix.windows.wasapi import volume as _volume

log = logging.getLogger(__name__)

# VB-Audio's driver always names its pair this way. A render endpoint carrying
# "CABLE Input" is the hub's sink; a capture endpoint carrying "CABLE Output"
# is the hub's source.
CABLE_INPUT_HINT = "CABLE Input"
CABLE_OUTPUT_HINT = "CABLE Output"


def _capture_endpoints() -> list[tuple[str, str]]:
    """[(endpoint id, friendly name)] for every active capture endpoint.

    `wasapi/devices.py` only enumerates render endpoints (Phase 1 only needed
    outputs); the VB-CABLE probe needs the capture flow too, so this asks
    pycaw for it directly rather than growing devices.py a flow argument for
    one caller.
    """
    from pycaw.constants import DEVICE_STATE, EDataFlow
    from pycaw.utils import AudioUtilities

    endpoints = []
    for d in AudioUtilities.GetAllDevices(EDataFlow.eCapture.value, DEVICE_STATE.ACTIVE.value):
        if d is None:
            continue
        endpoints.append((d.id, d.properties.get(PKEY_FriendlyName) or d.id))
    return endpoints


class WasapiBackend:

    def __init__(self) -> None:
        self._app_router = _policy.AppRouter()
        self._prev_default: str | None = None
        self._leader: str | None = None
        self._status: BackendStatus | None = None

    @property
    def leader(self) -> str | None:
        """The elected leader's endpoint id in leader mode, else None."""
        return self._leader

    def health(self) -> BackendStatus:
        """Never raises. Cached until the next create_sink — a cable
        appearing mid-session does not migrate a running session."""
        if self._status is None:
            self._status = self._probe()
        return self._status

    def _probe(self) -> BackendStatus:
        cable_in, cable_out = self._find_cable()
        if cable_in and cable_out:
            return BackendStatus(
                BackendHealth.OK, "VB-CABLE detected — outputs stay in sync.", engine="hub",
            )
        return BackendStatus(
            BackendHealth.OK,
            "Running in mirror mode — outputs may drift up to 50 ms apart. "
            "Install VB-CABLE for synced output.",
            engine="leader",
        )

    def _find_cable(self) -> tuple[str | None, str | None]:
        """(CABLE Input render id, CABLE Output capture id) — either is None
        if that endpoint is not present. Never raises."""
        cable_in = None
        try:
            # include_virtual: the cable is a virtual endpoint, and is hidden
            # from the outputs the user picks from for that very reason.
            for d in list_outputs(include_virtual=True):
                if CABLE_INPUT_HINT in d.name:
                    cable_in = d.id
                    break
        except Exception as e:
            log.debug("Could not enumerate outputs during VB-CABLE probe: %s", e)

        cable_out = None
        try:
            for device_id, name in _capture_endpoints():
                if CABLE_OUTPUT_HINT in name:
                    cable_out = device_id
                    break
        except Exception as e:
            log.debug("Could not enumerate capture endpoints during VB-CABLE probe: %s", e)

        return cable_in, cable_out

    def list_outputs(self) -> list[AudioDevice]:
        try:
            devices = list_outputs()
        except Exception as e:
            raise BackendError(f"Failed to enumerate outputs: {e}") from e
        log.info("Found %d output(s)", len(devices))
        return devices

    def list_streams(self) -> list[dict]:
        """Application streams now playing: {"id", "name", "sink", "mute"}."""
        try:
            return _sessions.list_streams()
        except Exception as e:
            raise BackendError(f"Failed to list streams: {e}") from e

    def move_streams(self, target: str, exclude: list[int] | None = None) -> None:
        """
        Route every active app to `target`, except those in `exclude`.

        Unlike `pactl move-sink-input`, which moves a live stream, this sets a
        *persisted preference* per app — some applications only pick it up the
        next time they open an audio stream.
        """
        if not self._app_router.available:
            log.warning("Per-app routing unavailable — skipping routing.")
            return

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
            try:
                self._app_router.route(s["id"], target)
                moved += 1
            except Exception as e:
                log.warning("Failed to move stream %d: %s", s["id"], e)

        log.info("Moved %d/%d stream(s) to %s", moved, len(streams), target)

    def move_stream(self, stream_id: int, target: str) -> None:
        """
        Route one app to `target`.

        Unlike `pactl move-sink-input`, this sets a *persisted preference* —
        the app may not pick it up until it next opens an audio stream.
        """
        if not self._app_router.available:
            raise BackendError("Per-app routing is not available on this system.")
        log.info("Moving stream %d → %s", stream_id, target)
        try:
            self._app_router.route(stream_id, target)
        except Exception as e:
            raise BackendError(f"Failed to move stream {stream_id} to {target}: {e}") from e

    def set_stream_mute(self, stream_id: int, mute: bool) -> None:
        try:
            _sessions.set_stream_mute(stream_id, mute)
        except Exception as e:
            raise BackendError(f"Failed to mute stream {stream_id}: {e}") from e

    def create_sink(self, devices: list[AudioDevice]) -> VirtualSink:
        """
        Start the fan-out engine for this session and return its handle.

        Hub mode captures VB-CABLE's "CABLE Output"; every selected device is
        a leg. Leader mode elects one of the selected devices and
        loopback-captures it; it is not a leg — it already plays through the
        OS path, and looping it back to itself would feed it its own echo.
        Either way the previous default is remembered so `destroy_sink` can
        restore it.

        Switching the Windows default to `sink.name` is the Controller's job,
        not done here, exactly as on Linux. Doing it here too meant it ran
        *before* `Controller._level_hub` had set the level, so the first
        moment of audio could arrive at whatever volume that endpoint
        happened to be sitting at — which is the very thing the comment in
        `_route` warns about.
        """
        if not devices:
            raise BackendError("No devices selected.")

        self._status = self._probe()

        if self._status.engine == "hub":
            cable_in, cable_out = self._find_cable()
            if not (cable_in and cable_out):
                raise BackendError(
                    "VB-CABLE endpoints disappeared before the session could start."
                )
            self._leader = None
            self._prev_default = self.get_default()
            source_id = cable_out
            hub_id = cable_in
        else:
            self._leader = self._elect_leader(devices)
            self._prev_default = self.get_default()
            source_id = self._leader
            hub_id = self._leader

        engine = Engine(source_id)
        engine.start()

        # `name` is the endpoint everything plays *into* — CABLE Input in hub
        # mode, the leader in leader mode. On Linux that slot holds the null
        # sink's name, which pactl accepts as a target for set-default-sink,
        # set-sink-volume and move-sink-input alike. The Controller uses it the
        # same way here (`active_sink()`), so it has to name a real endpoint;
        # a `pipemix_<uuid>` label would be meaningless to WASAPI. Nothing on
        # Windows needs the generated name — it exists on Linux so crash
        # recovery can spot our orphans, and Windows leaks no sinks to spot.
        sink = VirtualSink(engine, hub_id)
        log.info("Created session on %s (%s engine)", sink.name, self._status.engine)
        self.set_legs(sink, devices)
        return sink

    def _elect_leader(self, devices: list[AudioDevice]) -> str:
        """The current Windows default if it is among the selected outputs,
        else the first connected one."""
        current = default_output_id()
        ids = [d.id for d in devices]
        if current in ids:
            return current
        for d in devices:
            if d.connected:
                return d.id
        return devices[0].id

    def set_legs(self, sink: VirtualSink, devices: list[AudioDevice]) -> None:
        """Make the engine feed exactly these outputs. In leader mode the
        leader is excluded, whether or not it is still in `devices`."""
        wanted = [d for d in devices if d.id != self._leader]
        engine: Engine = sink.module
        engine.set_legs([d.id for d in wanted])
        sink.legs = {d.id: 0 for d in wanted}
        log.info("%s now feeds %s", sink.name, sorted(sink.legs))

    def destroy_sink(self, sink: VirtualSink) -> None:
        """Safe to call when the sink is already gone — never raises."""
        log.info("Destroying session on %s", sink.name)
        engine: Engine = sink.module
        try:
            engine.stop()
        except Exception:
            log.exception("Engine stop failed for %s", sink)
        sink.legs.clear()

        if self._prev_default:
            try:
                self.set_default(self._prev_default)
            except BackendError:
                log.exception("Could not restore previous default %r", self._prev_default)
        self._prev_default = None
        self._leader = None

    def find_orphans(self) -> list[VirtualSink]:
        """Nothing leaks on Windows — there are no kernel modules to unload.
        The crash-recovery problem here is a stranded default endpoint
        (Phase 6), not an orphaned sink."""
        return []

    def get_volume(self, sink: str) -> int:
        """0-100, or 100 if it cannot be read."""
        try:
            return _volume.get_volume(sink)
        except Exception as e:
            log.warning("Failed to get volume for %s: %s", sink, e)
            return 100

    def set_volume(self, sink: str, volume: int) -> None:
        vol = max(0, min(100, volume))
        try:
            _volume.set_volume(sink, vol)
        except Exception as e:
            raise BackendError(f"Failed to set volume of {sink} to {vol}%: {e}") from e

    def set_mute(self, sink: str, mute: bool) -> None:
        try:
            _volume.set_mute(sink, mute)
        except Exception as e:
            raise BackendError(f"Failed to set mute state for {sink}: {e}") from e

    def get_default(self) -> str | None:
        try:
            return _policy.get_default()
        except Exception as e:
            log.warning("Failed to read default endpoint: %s", e)
            return None

    def set_default(self, sink: str) -> None:
        try:
            _policy.set_default(sink)
        except Exception as e:
            raise BackendError(f"Failed to set default endpoint to {sink!r}: {e}") from e
        log.info("Default endpoint set to %r", sink)
