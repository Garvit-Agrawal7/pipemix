from __future__ import annotations

import logging
import threading
import time

from pipemix.models import AudioDevice, VirtualSink
from pipemix.models import BackendError, BackendHealth, BackendStatus
from pipemix.windows.wasapi.devices import default_output_id, list_outputs
from pipemix.windows.wasapi.engine import Engine
from pipemix.windows.wasapi import policy as _policy
from pipemix.windows.wasapi import sessions as _sessions

log = logging.getLogger(__name__)

# VB-Audio's fixed names: render "CABLE Input" is the hub's sink, capture
# "CABLE Output" its source.
CABLE_INPUT_HINT = "CABLE Input"
CABLE_OUTPUT_HINT = "CABLE Output"


def _endpoint_volume(device_id: str):
    import comtypes
    from pycaw.api.endpointvolume import IAudioEndpointVolume
    from pycaw.utils import AudioUtilities

    dev = AudioUtilities.GetDeviceEnumerator().GetDevice(device_id)
    iface = dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
    return iface.QueryInterface(IAudioEndpointVolume)


class WasapiBackend:

    def __init__(self) -> None:
        self._app_router = _policy.AppRouter()
        self.leader: str | None = None
        self._status: BackendStatus | None = None
        self._apps: dict[int, Engine] = {}
        self._failed_apps: set[int] = set()
        # Bumped by destroy_sink: routes computed for an older session are dropped.
        self.apps_gen = 0
        # Guards short mutations of the per-app state above; never held across Engine.start/stop.
        self._apps_lock = threading.Lock()
        # Engines told to stop; `close` waits for their pumps to exit.
        self._stopped: list[Engine] = []
        self._hub: str | None = None  # the session endpoint (cable_in, or the leader)
        self._routed: set[int] = set()  # pids pinned to _hub, unpinned in destroy_sink

    def health(self) -> BackendStatus:
        """Never raises. Cached until the next `reprobe` — a cable
        appearing mid-session does not migrate a running session."""
        if self._status is None:
            self._status = self._probe()
        return self._status

    def reprobe(self) -> None:
        """Pick hub vs leader mode afresh; the Controller calls it only before
        a new session, so a session keeps its mode through leader re-election."""
        self._status = self._probe()

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
            for d in list_outputs(include_virtual=True, flow="eCapture"):
                if CABLE_OUTPUT_HINT in d.name:
                    cable_out = d.id
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
            self._app_router.route(stream_id, target)
        except Exception as e:
            raise BackendError(f"Failed to move stream {stream_id} to {target}: {e}") from e
        if target and target == self._hub:
            self._routed.add(stream_id)
        else:
            self._routed.discard(stream_id)

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
        already plays through the OS and would echo itself. 
        The Controller then switches the default to `sink.name`, after the level.
        """
        if not devices:
            raise BackendError("No devices selected.")

        if self.health().engine == "hub":
            cable_in, cable_out = self._find_cable()
            if not (cable_in and cable_out):
                raise BackendError(
                    "VB-CABLE endpoints disappeared before the session could start."
                )
            self.leader = None
            hub_id = cable_in
            engine = None  # per-app engines are started later, by set_app_routes.
        else:
            self.leader = hub_id = self._elect_leader(devices)
            engine = Engine(self.leader)
            # ponytail: runs under the Controller lock (start_sharing, leader re-election), so it
            # is held for one loopback open (tens of ms, ~0.5 s for Bluetooth, up to
            # Engine.start's 10 s on a wedged driver). Moving it out needs
            # a STARTING session that a stop or re-election can cancel, like apps_gen does for apps.
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
        wanted = [d for d in devices if d.id != self.leader]
        engine: Engine | None = sink.module
        if engine is not None:
            engine.set_legs([d.id for d in wanted])
        sink.legs = {d.id: 0 for d in wanted}
        log.info("%s now feeds %s", sink.name, sorted(sink.legs))

    def set_app_routes(self, routes: dict[int, list[str]], gen: int) -> None:
        """Reconcile per-app engines with `routes` (pid -> device ids), once per poll;
        a pid that fails to start is skipped until it leaves `routes` and returns.

        `gen` is the `apps_gen` the routes were computed under: routes from an older
        session are ignored. _apps_lock is never held across Engine.start, so
        destroy_sink never waits on a start; a stale call stops what it started."""
        with self._apps_lock:
            if gen != self.apps_gen:
                return
            dead = [self._apps.pop(pid) for pid in list(self._apps) if pid not in routes]
            self._failed_apps &= set(routes)  # forget a failure once its pid leaves routes
            for pid, engine in self._apps.items():
                engine.set_legs(routes[pid])
            new = [p for p in routes if p not in self._apps and p not in self._failed_apps]

        self._stop_engines(dead)  # before any start: a slow start must not keep these playing
        stale = None
        for pid in new:
            try:
                engine = Engine(pid=pid)
                engine.start()
            except Exception as e:
                log.warning("Could not start per-app capture for pid %d: %s", pid, e)
                with self._apps_lock:
                    if gen != self.apps_gen:
                        break
                    self._failed_apps.add(pid)
                continue
            with self._apps_lock:
                if gen != self.apps_gen:  # the session ended while this one started
                    stale = engine
                    break
                self._apps[pid] = engine
                engine.set_legs(routes[pid])

        if stale:
            self._stop_engines([stale])

    def _stop_engines(self, engines: list[Engine]) -> None:
        """Signal every engine to stop (each goes quiet within one pump tick, together);
        `close` waits for the pumps. Never waits itself."""
        for engine in engines:
            engine.stop(wait=False)
        with self._apps_lock:
            self._stopped = [e for e in self._stopped if not e.join(0)] + engines

    def close(self, timeout: float = 3.0) -> None:
        """On exit: wait up to `timeout` in total for stopped engines to close their
        streams, including ones stopped while this waits."""
        deadline = time.monotonic() + timeout
        while True:
            with self._apps_lock:
                self._stopped = [e for e in self._stopped if not e.join(0)]
                engine = self._stopped[0] if self._stopped else None
            if engine is None:
                return
            if not engine.join(max(0.0, deadline - time.monotonic())):
                # ponytail: a driver call that never returns is abandoned here, and the daemon
                # pump dies at interpreter exit mid-close; a process-exit hook or longer budget if that bites.
                log.warning("Quit: abandoning %s, still closing after %.1f s",
                            engine.source_id or f"pid {engine.pid}", timeout)
                return

    def destroy_sink(self, sink: VirtualSink) -> None:
        """Safe to call when the sink is already gone — never raises."""
        log.info("Destroying session on %s", sink.name)
        engine: Engine | None = sink.module
        sink.legs.clear()

        # Swap the apps out without waiting on an in-flight set_app_routes: it sees
        # the gen change and stops whatever it was starting itself.
        with self._apps_lock:
            self.apps_gen += 1
            self._hub = None
            apps, self._apps = self._apps, {}
            self._failed_apps.clear()
        self._stop_engines(([engine] if engine is not None else []) + list(apps.values()))

        # The controller's pinned_apps sweep only reaches apps whose exe it knows.
        for pid in self._routed:
            try:
                self._app_router.route(pid, None)
            except Exception as e:
                log.warning("Failed to unpin stream %d from the hub: %s", pid, e)
        self._routed.clear()
        self.leader = None

    def get_volume(self, sink: str) -> int:
        """0-100, or 100 if it cannot be read."""
        try:
            return round(_endpoint_volume(sink).GetMasterVolumeLevelScalar() * 100)
        except Exception as e:
            log.warning("Failed to get volume for %s: %s", sink, e)
            return 100

    def set_volume(self, sink: str, volume: int) -> None:
        from pycaw.constants import IID_Empty

        vol = max(0, min(100, volume))
        try:
            _endpoint_volume(sink).SetMasterVolumeLevelScalar(vol / 100, IID_Empty)
        except Exception as e:
            raise BackendError(f"Failed to set volume of {sink} to {vol}%: {e}") from e

    def set_mute(self, sink: str, mute: bool) -> None:
        from pycaw.constants import IID_Empty

        try:
            _endpoint_volume(sink).SetMute(mute, IID_Empty)
        except Exception as e:
            raise BackendError(f"Failed to set mute state for {sink}: {e}") from e

    def get_default(self) -> str | None:
        return default_output_id()

    def set_default(self, sink: str) -> None:
        try:
            _policy.set_default(sink)
        except Exception as e:
            raise BackendError(f"Failed to set default endpoint to {sink!r}: {e}") from e
        log.info("Default endpoint set to %r", sink)
