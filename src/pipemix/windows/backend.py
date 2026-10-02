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

# VB-Audio's fixed names: render "CABLE Input" is the hub's sink, capture
# "CABLE Output" its source.
CABLE_INPUT_HINT = "CABLE Input"
CABLE_OUTPUT_HINT = "CABLE Output"


def _capture_endpoints() -> list[tuple[str, str]]:
    """[(endpoint id, friendly name)] for every active capture endpoint.

    devices.py only enumerates render endpoints; this one caller asks pycaw directly.
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
        self._hub: str | None = None
        self._routed: set[int] = set()
        self._apps: dict[int, Engine] = {}
        self._failed_apps: set[int] = set()

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
                BackendHealth.OK, "VB-CABLE detected — outputs are synced in software; Bluetooth delay is not compensated.", engine="hub",
            )
        return BackendStatus(
            BackendHealth.OK,
            "Running in mirror mode — outputs may drift apart. "
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

    def restore_target(self, devices: list[AudioDevice] = ()) -> str | None:
        """The default to restore when a session ends: the current default, unless
        it is CABLE Input (our hub) — then the first connected non-cable device in
        `devices`, else the first of `list_outputs()`, else None. Never raises.

        Only CABLE Input is rejected, not every virtual endpoint: a user who
        defaults to e.g. Voicemeeter must get it back.
        """
        cable_in, _ = self._find_cable()
        current = self.get_default()
        if current and current != cable_in:
            return current

        for d in devices:
            if d.connected and d.id != cable_in:
                return d.id

        try:
            outputs = self.list_outputs()
        except BackendError as e:
            log.debug("Could not enumerate outputs for restore target: %s", e)
            outputs = []
        for d in outputs:
            if d.connected:
                return d.id

        return None

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

    def move_stream(self, stream_id: int, target: str | None) -> None:
        """
        Route one app to `target`, or None to clear its pin and follow the default.

        A persisted preference, not a live move: the app may not pick it up until
        it next opens an audio stream.
        """
        if not self._app_router.available:
            raise BackendError("Per-app routing is not available on this system.")
        log.info("Moving stream %d → %s", stream_id, target or "(cleared)")
        try:
            self._route_stream(stream_id, target)
        except Exception as e:
            raise BackendError(f"Failed to move stream {stream_id} to {target}: {e}") from e

    def _route_stream(self, pid: int, target: str | None) -> None:
        """Persist `pid`'s route and track whether it points at our hub, so
        `destroy_sink` knows what to unpin. Routes to real devices are left alone."""
        self._app_router.route(pid, target)
        if target == self._hub:
            self._routed.add(pid)
        else:
            self._routed.discard(pid)

    def set_stream_mute(self, stream_id: int, mute: bool) -> None:
        try:
            _sessions.set_stream_mute(stream_id, mute)
        except Exception as e:
            raise BackendError(f"Failed to mute stream {stream_id}: {e}") from e

    def create_sink(self, devices: list[AudioDevice]) -> VirtualSink:
        """
        Start the fan-out engine for this session and return its handle.

        Hub mode starts no engine: apps play into the silent CABLE Input and
        `set_app_routes` captures each app out to its devices. `sink.legs` still
        lists every selected device, so callers needn't care about the mode.
        Leader mode loopback-captures one selected device; it is not a leg, as it
        already plays through the OS and would echo itself. Either way the previous
        default is kept for `destroy_sink`.

        The Controller then switches the default to `sink.name`, after the level.
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
            self._prev_default = self.restore_target(devices)
            hub_id = cable_in
            engine = None  # per-app engines are started later, by set_app_routes.
        else:
            self._leader = self._elect_leader(devices)
            self._prev_default = self.restore_target(devices)
            hub_id = self._leader
            engine = Engine(self._leader)
            engine.start()

        self._hub = hub_id

        # `name` is the endpoint everything plays into, because the
        # Controller passes it to set_default/set_volume.
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
        """Make the engine feed exactly these outputs, never the leader. In hub mode
        only `sink.legs` changes; the fan-out is per-app, via `set_app_routes`."""
        wanted = [d for d in devices if d.id != self._leader]
        engine: Engine | None = sink.module
        if engine is not None:
            engine.set_legs([d.id for d in wanted])
        sink.legs = {d.id: 0 for d in wanted}
        log.info("%s now feeds %s", sink.name, sorted(sink.legs))

    def set_app_routes(self, routes: dict[int, list[str]]) -> None:
        """Reconcile per-app engines with `routes` (pid -> device ids), once per poll:
        one `Engine(pid=...)` per pid, stopped when it drops out, `set_legs` when its
        devices change. Never raises; a pid that fails to start is skipped until it
        leaves `routes` and returns."""
        wanted = set(routes)

        for pid in list(self._apps):
            if pid not in wanted:
                engine = self._apps.pop(pid)
                try:
                    engine.stop()
                except Exception:
                    log.exception("Failed to stop app engine for pid %d", pid)
        self._failed_apps &= wanted  # forget a failure once its pid leaves routes

        for pid, ids in routes.items():
            if pid in self._failed_apps:
                continue
            engine = self._apps.get(pid)
            is_new = engine is None
            if is_new:
                try:
                    engine = Engine(pid=pid)
                    engine.start()
                except Exception as e:
                    log.warning("Could not start per-app capture for pid %d: %s", pid, e)
                    self._failed_apps.add(pid)
                    continue
                self._apps[pid] = engine
            if is_new or set(engine.legs) != set(ids):
                try:
                    engine.set_legs(ids)
                except Exception:
                    log.exception("Failed to set legs for app pid %d", pid)

    def destroy_sink(self, sink: VirtualSink) -> None:
        """Safe to call when the sink is already gone — never raises."""
        log.info("Destroying session on %s", sink.name)
        engine: Engine | None = sink.module
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                log.exception("Engine stop failed for %s", sink)
        sink.legs.clear()

        for pid, app_engine in self._apps.items():
            try:
                app_engine.stop()
            except Exception:
                log.exception("Failed to stop app engine for pid %d", pid)
        self._apps.clear()
        self._failed_apps.clear()

        for pid in self._routed:
            try:
                self._app_router.route(pid, None)
            except Exception as e:
                log.warning("Failed to unpin stream %d from the hub: %s", pid, e)
        self._routed.clear()
        self._hub = None

        if self._prev_default:
            try:
                self.set_default(self._prev_default)
            except BackendError:
                log.exception("Could not restore previous default %r", self._prev_default)
        self._prev_default = None
        self._leader = None

    def find_orphans(self) -> list[VirtualSink]:
        """No-op: Windows has no modules to leak. Crash recovery here is about a
        stranded default endpoint."""
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
            return default_output_id()
        except Exception as e:
            log.warning("Failed to read default endpoint: %s", e)
            return None

    def set_default(self, sink: str) -> None:
        try:
            _policy.set_default(sink)
        except Exception as e:
            raise BackendError(f"Failed to set default endpoint to {sink!r}: {e}") from e
        log.info("Default endpoint set to %r", sink)
