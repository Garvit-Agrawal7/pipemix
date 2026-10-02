"""
PipeMix — the Core Audio backend.

Same surface as `PactlBackend` and `WasapiBackend`, so the Controller calls
it the same way. The hub is a stacked aggregate device (a Multi-Output
Device) that PipeMix creates and makes the default output; its subdevices
are the legs. Changing the legs rewrites the live aggregate's subdevice list
rather than rebuilding it, so the session's UID — the `sink` the Controller
holds — never changes.

Two things the aggregate does not give us, made up for here:

- **Master volume.** A stacked aggregate has no volume control of its own,
  so the hub's level is applied by scaling each leg: a leg plays at
  (its own level × master / 100). `get_volume`/`set_volume` on a device
  always speak its *own* level, which is what the page shows on its row.
- **Per-app routing.** macOS has no way to move another app's stream to a
  device, so an app routed by hand is captured with a process tap, muted at
  the source, and played to its outputs by a private aggregate of its own
  (see `apps.py`). Apps nobody routed keep playing into the hub untouched.
  Needs macOS 14.2+; on older systems the engine is "aggregate" and the page
  hides the Apps tab.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import uuid

from pipemix.models import AudioDevice, VirtualSink
from pipemix.linux.services.backend import BackendError, BackendHealth, BackendStatus

log = logging.getLogger(__name__)

HUB_UID  = "com.pipemix.hub"   # prefix; each session's hub adds a suffix
HUB_NAME = "PipeMix"


class CoreAudioBackend:

    def __init__(self) -> None:
        self._status: BackendStatus | None = None
        self._levels: dict[str, int] = {}   # each device's own level, 0-100
        self._master = 100                  # the hub's level, applied through the legs
        self._legs: list[str] = []          # UIDs the live hub feeds
        # Per-app routing: what is wanted (guarded by _want_lock, changed by
        # any caller) and what the tap worker has made of it.
        self._want_lock = threading.Lock()
        self._app_routes: dict[int, list[str]] = {}  # pid -> outputs, set by the Controller
        self._mutes: set[int] = set()       # pids muted from the Apps tab
        self._router = None                 # apps.AppRouter, owned by the worker
        self._problems: dict[int, str] = {}
        self._busy: dict[int, float] = {}   # pid -> when the worker started on it
        self._worker: threading.Thread | None = None
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._closing = False

    # ---------- Health ----------

    def health(self) -> BackendStatus:
        """Never raises."""
        if self._status is None:
            self._status = self._probe()
        return self._status

    def _probe(self) -> BackendStatus:
        if sys.platform != "darwin":
            return BackendStatus(BackendHealth.UNAVAILABLE, "Core Audio is only on macOS.",
                                 engine="aggregate")
        try:
            from pipemix.macos import coreaudio as ca
            ca.device_ids()
        except Exception as e:
            return BackendStatus(BackendHealth.UNAVAILABLE, f"Core Audio is not responding: {e}",
                                 engine="aggregate")
        from pipemix.macos import coreaudio as ca
        if ca.taps_supported():
            return BackendStatus(BackendHealth.OK, "Core Audio ready.", engine="taps")
        return BackendStatus(BackendHealth.OK, "Core Audio ready. Per-app routing needs macOS 14.2.",
                             engine="aggregate")

    # ---------- Devices ----------

    def list_outputs(self) -> list[AudioDevice]:
        from pipemix.macos.devices import list_outputs
        try:
            devices = list_outputs()
        except Exception as e:
            raise BackendError(f"Failed to enumerate outputs: {e}") from e
        log.info("Found %d output(s)", len(devices))
        return devices

    def restore_target(self, devices: list[AudioDevice] = ()) -> str | None:
        """The output to put back when the session ends: the current default,
        unless that is a PipeMix hub (a crashed run's), then the first
        connected device. Never raises."""
        from pipemix.macos.devices import is_hub
        current = self.get_default()
        if current and not is_hub(current):
            return current
        for d in devices:
            if d.connected:
                return d.id
        try:
            for d in self.list_outputs():
                if d.connected:
                    return d.id
        except BackendError as e:
            log.debug("Could not enumerate outputs for restore target: %s", e)
        return None

    # ---------- Per-app routing ----------
    #
    # Taps are made and torn down on one worker thread, never on the caller's.
    # The first tap PipeMix ever makes raises macOS's System Audio Recording
    # prompt, and Core Audio blocks tap calls until it is answered; holding
    # the Controller's lock that long would freeze the window and Quit.
    # Callers only change the desired state; the worker converges on it and
    # leaves per-app problems in `_problems` for the Apps tab to show.

    def _start_worker(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._reconcile_loop, name="pipemix-taps",
                                            daemon=True)
            self._worker.start()

    def list_streams(self) -> list[dict]:
        """Apps now playing: {"id" (pid), "name", "exe", "sink", "endpoint",
        "active", "mute"}. Apps we route or mute stay listed while they pause."""
        from pipemix.macos import coreaudio as ca
        if not ca.taps_supported():
            return []
        from pipemix.macos.apps import playing
        with self._want_lock:
            keep = set(self._app_routes) | self._mutes
        try:
            live = playing(keep)
        except Exception as e:
            raise BackendError(f"Failed to list apps: {e}") from e
        pids = {s["id"] for s in live}
        gone = keep - pids
        if gone:   # those apps have quit
            with self._want_lock:
                for pid in gone:
                    self._app_routes.pop(pid, None)
                    self._mutes.discard(pid)
            self._kick()
        with self._want_lock:
            for s in live:
                s["mute"] = s["id"] in self._mutes
        return live

    def set_app_routes(self, routes: dict[int, list[str]]) -> None:
        """Route exactly these apps (pid -> device UIDs); every other app plays
        where it would anyway. Returns at once; the taps follow."""
        wanted = {pid: list(ids) for pid, ids in routes.items() if ids}
        if wanted:
            self._check_permission()
        with self._want_lock:
            self._app_routes = wanted
        self._kick()

    def set_stream_mute(self, stream_id: int, mute: bool) -> None:
        if mute:
            self._check_permission()
        with self._want_lock:
            if mute:
                self._mutes.add(stream_id)
            else:
                self._mutes.discard(stream_id)
        self._kick()

    def move_stream(self, stream_id: int, target: str | None) -> None:
        with self._want_lock:
            routes = dict(self._app_routes)
        routes[stream_id] = [target] if target else []
        self.set_app_routes(routes)

    def _check_permission(self) -> None:
        from pipemix.macos import coreaudio as ca
        from pipemix.macos.apps import PERMISSION_DENIED, permission
        if not ca.taps_supported():
            raise BackendError("Per-app audio needs macOS 14.2 or later.")
        if permission() == PERMISSION_DENIED:
            raise BackendError(
                "PipeMix isn't allowed to capture app audio. Turn it on in System Settings → "
                "Privacy & Security → Screen & System Audio Recording (System Audio "
                "Recording Only), then try again.")

    def _kick(self) -> None:
        self._start_worker()
        self._wake.set()

    def _reconcile_loop(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            if self._closing:
                return
            self._reconcile()
            self._idle.set()

    def _reconcile(self) -> None:
        from pipemix.macos.tapcopy import load
        router = self._apps()
        with self._want_lock:
            routes, mutes = dict(self._app_routes), set(self._mutes)
        for pid in set(router.routes) | set(routes) | mutes:
            want, mute = routes.get(pid), pid in mutes
            self._busy[pid] = time.monotonic()
            try:
                if want and not mute:
                    load()
                router.apply(pid, want, mute)
                self._problems.pop(pid, None)
            except RuntimeError as e:          # the IOProc helper
                self._problems[pid] = str(e)
            except Exception as e:
                log.warning("Could not capture app %d: %s", pid, e)
                self._problems[pid] = f"could not capture this app ({e})"
                router.clear(pid)
            finally:
                self._busy.pop(pid, None)

    def _apps(self):
        if self._router is None:
            from pipemix.macos.apps import AppRouter
            self._router = AppRouter()
        return self._router

    def route_problem(self, pid: int, playing: bool) -> str | None:
        """Why a routed or muted app is not doing what it was told, or None."""
        from pipemix.macos.apps import PERMISSION_UNKNOWN, permission
        if pid in self._problems:
            return self._problems[pid]
        since = self._busy.get(pid)
        if since is not None and time.monotonic() - since > 1.0:
            if permission() == PERMISSION_UNKNOWN:
                return "waiting for you to allow System Audio Recording for PipeMix"
            return "starting…"
        route = self._router.routes.get(pid) if self._router else None
        if route and route.silent(playing):
            return ("no sound captured — allow PipeMix under System Settings → "
                    "Privacy & Security → Screen & System Audio Recording")
        return None

    def clear_apps(self, wait: float = 2.0) -> None:
        """Release every tap: unroute and unmute everything. Waits briefly for
        the worker; taps are private, so whatever is left dies with us anyway."""
        with self._want_lock:
            self._app_routes.clear()
            self._mutes.clear()
        if self._worker is None:
            return
        self._idle.clear()
        self._wake.set()
        self._idle.wait(wait)

    # ---------- The hub ----------

    def create_sink(self, devices: list[AudioDevice]) -> VirtualSink:
        """Create the hub over `devices`. The Controller makes it the default."""
        from pipemix.macos import coreaudio as ca

        if not devices:
            raise BackendError("No devices selected.")

        # Only one hub at a time: anything with our UID is a leftover.
        for orphan in self.find_orphans():
            self.destroy_sink(orphan)

        uids = [d.id for d in devices]
        main = self._clock(uids)
        # A fresh UID per session: a leftover hub that coreaudiod has not
        # finished destroying can never collide with the new one.
        hub_uid = f"{HUB_UID}.{uuid.uuid4().hex[:8]}"
        try:
            agg = ca.create_aggregate(hub_uid, HUB_NAME, self._ordered(uids, main), main)
        except ca.CoreAudioError as e:
            raise BackendError(f"Could not create the PipeMix output: {e}") from e

        self._master = 100
        self._legs = []
        sink = VirtualSink(agg, hub_uid)
        self._adopt_legs(sink, uids)
        log.info("Created hub %s (id %d) clocked by %s", hub_uid, agg, main)
        return sink

    def set_legs(self, sink: VirtualSink, devices: list[AudioDevice]) -> None:
        """Make the hub feed exactly these outputs. The ones staying are not touched."""
        from pipemix.macos import coreaudio as ca

        uids = [d.id for d in devices if d.sink]
        if uids == self._legs:
            return

        agg = ca.device_for_uid(sink.name)
        if agg is not None and uids:
            main = self._clock(uids)
            try:
                ca.set_subdevices(agg, self._ordered(uids, main), main)
            except ca.CoreAudioError as e:
                log.warning("Live update of the hub failed (%s) — rebuilding it.", e)
                agg = self._recreate(sink, uids, main)
        elif uids:
            # The hub went away underneath us (every subdevice vanished, or
            # coreaudiod restarted). Stand it back up under the same UID.
            log.warning("Hub %s is missing — rebuilding it.", sink.name)
            agg = self._recreate(sink, uids, self._clock(uids))
        elif agg is not None:
            try:
                ca.set_subdevices(agg, [], "")
            except ca.CoreAudioError as e:
                log.debug("Could not empty the hub: %s", e)

        if agg is not None:
            sink.module = agg
        self._adopt_legs(sink, uids)

    def _recreate(self, sink: VirtualSink, uids: list[str], main: str) -> int:
        from pipemix.macos import coreaudio as ca
        was_default = self.get_default() == sink.name
        old = ca.device_for_uid(sink.name)
        if old is not None:
            try:
                ca.destroy_aggregate(old)
            except ca.CoreAudioError as e:
                log.debug("Could not destroy the old hub: %s", e)
        try:
            agg = ca.create_aggregate(sink.name, HUB_NAME, self._ordered(uids, main), main)
        except ca.CoreAudioError as e:
            raise BackendError(f"Could not rebuild the PipeMix output: {e}") from e
        if was_default or old is None:
            self.set_default(sink.name)
        return agg

    def _adopt_legs(self, sink: VirtualSink, uids: list[str]) -> None:
        """Record the new legs and put every device at the level it should be at now."""
        dropped = [u for u in self._legs if u not in uids]
        self._legs = list(uids)
        sink.legs = {u: 0 for u in uids}
        for u in dropped:
            self._apply(u)  # back to its own level, unscaled
        for u in uids:
            self._apply(u)
        log.info("%s now feeds %s", sink.name, uids)

    def _clock(self, uids: list[str]) -> str:
        from pipemix.macos.devices import clock_rank
        return min(uids, key=clock_rank)

    @staticmethod
    def _ordered(uids: list[str], main: str) -> list[str]:
        return [main] + [u for u in uids if u != main]

    def destroy_sink(self, sink: VirtualSink) -> None:
        """Safe to call when the hub is already gone — never raises."""
        from pipemix.macos import coreaudio as ca
        log.info("Destroying hub %s", sink.name)

        legs, self._legs = self._legs, []
        for u in legs:
            self._apply(u)
        sink.legs.clear()

        try:
            agg = ca.device_for_uid(sink.name)
            if agg is not None:
                ca.destroy_aggregate(agg)
        except Exception as e:
            log.warning("Could not destroy hub %s: %s", sink.name, e)

    def find_orphans(self) -> list[VirtualSink]:
        """Hubs left behind by a run that crashed. Aggregates outlive their creator."""
        from pipemix.macos.devices import hub_devices
        try:
            return [VirtualSink(dev, device_uid) for dev, device_uid in hub_devices()]
        except Exception as e:
            log.warning("Could not look for leftover hubs: %s", e)
            return []

    # ---------- Volume ----------

    def get_volume(self, sink: str) -> int:
        """0-100: the hub's master level, or a device's own level. 100 if unreadable."""
        from pipemix.macos.devices import is_hub
        if is_hub(sink):
            return self._master
        if sink in self._levels:
            return self._levels[sink]
        from pipemix.macos import coreaudio as ca
        try:
            dev = ca.device_for_uid(sink)
            level = ca.get_volume(dev) if dev is not None else None
        except Exception as e:
            log.warning("Failed to get volume for %s: %s", sink, e)
            level = None
        if level is None:
            return 100
        vol = round(level * 100)
        if sink in self._legs and self._master:
            vol = min(100, round(vol * 100 / self._master))
        return vol

    def set_volume(self, sink: str, volume: int) -> None:
        from pipemix.macos.devices import is_hub
        vol = max(0, min(100, volume))
        if is_hub(sink):
            self._master = vol
            for u in self._legs:
                self._apply(u)
            return
        self._levels[sink] = vol
        self._apply(sink, strict=True)

    def _apply(self, device_uid: str, strict: bool = False) -> None:
        """Set the device to its own level, scaled by master while it is a leg."""
        from pipemix.macos import coreaudio as ca
        level = self._levels.get(device_uid)
        if level is None:
            return
        if device_uid in self._legs:
            level = level * self._master / 100
        try:
            dev = ca.device_for_uid(device_uid)
            if dev is None:
                return
            if not ca.set_volume(dev, level / 100):
                log.debug("%s has no volume control", device_uid)
        except Exception as e:
            if strict:
                raise BackendError(f"Failed to set volume of {device_uid}: {e}") from e
            log.warning("Failed to set volume of %s: %s", device_uid, e)

    def set_mute(self, sink: str, mute: bool) -> None:
        from pipemix.macos.devices import is_hub
        if is_hub(sink):
            return  # nothing to mute; the legs carry their own mute
        from pipemix.macos import coreaudio as ca
        try:
            dev = ca.device_for_uid(sink)
            if dev is not None:
                ca.set_mute(dev, mute)
        except Exception as e:
            raise BackendError(f"Failed to set mute state for {sink}: {e}") from e

    # ---------- Default output ----------

    def get_default(self) -> str | None:
        from pipemix.macos import coreaudio as ca
        try:
            return ca.uid(ca.default_output()) or None
        except Exception as e:
            log.warning("Failed to read the default output: %s", e)
            return None

    def set_default(self, sink: str) -> None:
        from pipemix.macos import coreaudio as ca
        try:
            dev = ca.device_for_uid(sink)
            if dev is None:
                raise BackendError(f"Output {sink!r} is not present.")
            ca.set_default_output(dev)
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(f"Failed to set default output to {sink!r}: {e}") from e
        log.info("Default output set to %r", sink)
