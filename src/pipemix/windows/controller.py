from __future__ import annotations

import functools
import logging
import threading
import time
from typing import TYPE_CHECKING

from pipemix.models import AudioDevice, SessionState, SharingSession, VirtualSink
from pipemix.models import BackendError, BackendHealth
from pipemix.config import ConfigManager
from pipemix.windows.signal import SignalEmitter
from pipemix.windows.wasapi.notify import DeviceMonitor

if TYPE_CHECKING:
    from pipemix.windows.backend import WasapiBackend

log = logging.getLogger(__name__)

# How often a hub-mode session reconciles per-app captures with what is playing.
APP_POLL_S = 1.0
# How long a paused app keeps its capture, so resuming loses no audio. Capped,
# or idle legs stream silence forever and Bluetooth links never sleep.
APP_IDLE_S = 30.0
# Total time quitting may wait: in-flight app captures finishing, then engines closing.
QUIT_S = 3.0


def locked(fn):
    """Serialize routing: the page calls in on one thread, the notify worker on another."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


class Controller(SignalEmitter):

    def __init__(self, backend: WasapiBackend, config: ConfigManager | None = None) -> None:
        super().__init__()
        self.backend = backend
        self.config = config or ConfigManager()

        self.monitor = DeviceMonitor()
        self.monitor.on_connect = self._on_connect
        self.monitor.on_disconnect = self._on_disconnect

        self.session = SharingSession()
        self.devices: dict[str, AudioDevice] = {}
        self.prev_default: str | None = None

        # Endpoint ids we want back if they reconnect mid-session.
        self.targets: set[str] = set()

        # Streams the user routed by hand (pid -> device ids), so a rebuild
        # does not drag them back.
        self.overrides: dict[int, list[str]] = {}

        # Hub-mode app poll: the thread, the events that stop and wake it, and
        # the last app list pushed to the page. Only this thread calls
        # backend.set_app_routes, so Engine.start/stop never runs under `_lock`.
        self._app_poll: threading.Thread | None = None
        # Every poll not yet seen to exit, so quitting joins one a restart left behind.
        self._app_polls: list[threading.Thread] = []
        self._app_poll_stop: threading.Event | None = None
        self._app_wake: threading.Event | None = None
        self._last_streams: list[dict] | None = None
        # pid -> monotonic time it was last seen playing into the hub.
        self._app_active: dict[int, float] = {}

        # Last known exe per stream pid, so a pin can be recorded/cleared in
        # config.data["pinned_apps"] by exe even after the pid exits.
        self._exe: dict[int, str] = {}

        self.master_volume = 50

        # Page calls (pywebview thread) and the notify worker both change the legs.
        self._lock = threading.RLock()

    # ---------- Startup / shutdown ----------

    def start(self) -> None:
        status = self.backend.health()
        self.emit("health-changed", status)
        if status.health != BackendHealth.OK:
            log.warning("Backend unhealthy on startup: %s", status.message)

        self.clean_orphans()

        try:
            self.monitor.start()
        except Exception as e:
            log.error("Failed to start endpoint monitor: %s", e)

        self.refresh()

    def stop(self) -> None:
        """Quit: unwind the session, then wait up to QUIT_S in total for the app polls
        (an in-flight Engine.start hands its late engine to the backend) and for every
        stopped engine to close. Never called under `_lock`, which the polls need."""
        deadline = time.monotonic() + QUIT_S
        if self.session.is_active or self.session.sink:  # a REPAIRING session too
            try:
                self.stop_sharing()
            except Exception as e:
                log.error("Failed to stop sharing during shutdown: %s", e)
        else:
            # Pins made while idle are still ours to undo on the way out.
            self.overrides.clear()
            self._last_streams = None
            try:
                self._sweep_pins(self.backend.list_streams())
            except Exception as e:
                log.warning("Failed to sweep leftover per-app pins: %s", e)
        for poll in self._app_polls:
            poll.join(max(0.0, deadline - time.monotonic()))
        stuck = [p for p in self._app_polls if p.is_alive()]
        if stuck:
            # ponytail: a capture start that never returns is abandoned, killed at exit with the
            # daemon poll; a process-exit hook or a longer QUIT_S if that bites.
            log.warning("Quit: abandoning %d app poll(s) still mid-sync (a capture start?) after %.1f s",
                        len(stuck), QUIT_S)
        self.backend.close(max(0.0, deadline - time.monotonic()))
        self.monitor.stop()

    def clean_orphans(self) -> None:
        """Restore a default output stranded by a crash.

        Uses `prev_default` if it is still on disk (set by `start_sharing`, cleared
        by `stop_sharing`). Otherwise, with no session running and the default still
        our hub (CABLE Input), puts the default back.
        """
        try:
            # Overrides are empty this early, so any app still pinned by a
            # previous run gets cleared here rather than waiting for streams().
            try:
                self._sweep_pins(self.backend.list_streams())
            except Exception as e:
                log.warning("Failed to sweep leftover per-app pins: %s", e)

            stranded = self.config.data.get("prev_default")
            restored_stranded = False
            if stranded:
                live_ids = {d.id for d in self.backend.list_outputs()}
                if stranded not in live_ids:
                    log.warning(
                        "Previous run was interrupted, but its default output "
                        "(%s) is no longer connected — leaving it as is.", stranded)
                else:
                    log.warning(
                        "Previous run was interrupted — restoring default output to %s.", stranded)
                    self.backend.set_default(stranded)
                    restored_stranded = True

                self.config.data["prev_default"] = None
                self.config.save()

            # The stranded-default path above already restored something (or
            # deliberately left a vanished endpoint alone) — never double-set.
            if restored_stranded or self.session.is_active:
                return

            current = self.backend.get_default()
            target = self.backend.restore_target([])
            if target and target != current:
                log.warning(
                    "Windows default is PipeMix's hub (CABLE Input) with no "
                    "session running — restoring output to %s", target)
                self.backend.set_default(target)
        except Exception as e:
            log.error("Error during crash recovery: %s", e)

    # ---------- Devices ----------

    def refresh(self) -> None:
        """Re-enumerate outputs. `backend.list_outputs()` is already the whole truth."""
        self.emit("health-changed", self.backend.health())
        try:
            found: dict[str, AudioDevice] = {}
            for dev in self.backend.list_outputs():
                dev.name = self.config.device_name(dev.id, dev.name)
                dev.volume = self._volume_of(dev.id, dev.sink)
                found[dev.id] = dev

            self.devices = found
            self.emit("devices-changed", list(found.values()))
            # No session notifications on Windows yet: outside a hub session the
            # app list only updates here (a hub session also polls, `_sync_apps`).
            self.emit("streams-changed", self.streams())

        except Exception as e:
            log.error("Failed to refresh devices: %s", e)

    def _volume_of(self, dev_id: str, sink: str | None) -> int:
        """Keep the volume we already know; otherwise ask the sink, else 50%."""
        if dev_id in self.devices:
            return self.devices[dev_id].volume
        if not sink:
            return 50
        try:
            return self.backend.get_volume(sink)
        except Exception:
            return 50

    def set_device_volume(self, dev_id: str, volume: int, unmute: bool = False) -> None:
        dev = self.devices.get(dev_id)
        if not dev:
            return
        dev.volume = volume
        if self._solo() is dev:
            self.master_volume = volume
        if dev.connected and dev.sink:
            try:
                self.backend.set_mute(dev.sink, False)
                self.backend.set_volume(dev.sink, volume)
            except Exception as e:
                log.error("Failed to set volume for %s: %s", dev_id, e)

    def set_master_volume(self, volume: int, unmute: bool = False) -> None:
        self.master_volume = volume

        solo = self._solo()
        if solo:
            # The session is transparent for a lone output, so the level belongs on
            # the device — and its row has to move with the master row.
            self.set_device_volume(solo.id, volume, unmute)
            self.emit("devices-changed", list(self.devices.values()))
            return

        target = self.active_sink() if self.session.is_active else self.prev_default
        if target:
            try:
                if unmute:
                    self.backend.set_mute(target, False)
                self.backend.set_volume(target, volume)
            except Exception as e:
                log.error("Failed to set master volume on %s: %s", target, e)

    def _solo(self) -> AudioDevice | None:
        """The one output the session is feeding, when there is only one."""
        return self.session.devices[0] if len(self.session.devices) == 1 else None

    def _level_hub(self, sink: str, devices: list[AudioDevice]) -> None:
        """
        Set the session's level; with a lone output the device carries it instead.

        Master and that device's fader are then one control. If both carried a
        level they would multiply, and the fader would feel dead until near the top.
        """
        solo = devices[0] if len(devices) == 1 else None
        if solo:
            self.master_volume = solo.volume
        try:
            self.backend.set_volume(sink, self._hub_level(devices))
        except Exception as e:
            log.warning("Failed to set the level on %s: %s", sink, e)

    def _hub_level(self, devices: list[AudioDevice]) -> int:
        """100 when a lone output carries the level itself, else the master."""
        return 100 if len(devices) == 1 else self.master_volume

    def active_sink(self) -> str | None:
        """Whatever the session is currently playing through."""
        return self.session.sink.name if self.session.sink else None

    # ---------- Sharing ----------

    @locked
    def start_sharing(self, devices: list[AudioDevice]) -> None:
        if not devices:
            log.warning("start_sharing() called with no devices.")
            return

        if self.session.sink:
            # The session is already up, so only its legs move; staying outputs never drop out.
            self._retarget(devices)
            return

        log.info("Starting session with %d device(s)...", len(devices))
        self._set_state(SessionState.STARTING)
        try:
            self.backend.reprobe()  # only here: re-election keeps the session's mode
            self.emit("health-changed", self.backend.health())  # mode may have flipped
            self.prev_default = self.backend.restore_target(devices)
            self.config.data["prev_default"] = self.prev_default
            self.config.save()
            self.session.sink = self._route(devices)
            self._adopt(devices)
            log.info("Session active: %s", self.active_sink())
            if self.backend.health().engine == "hub":
                self._start_app_poll()  # syncs once straight away
        except Exception as e:
            log.error("Failed to start session: %s", e)
            self._set_state(SessionState.ERROR)
            self.stop_sharing()
            raise

    def _route(self, devices: list[AudioDevice]) -> VirtualSink:
        """Stand up the session and point everything that is playing at it."""
        self._prepare(devices)
        sink = self.backend.create_sink(devices)
        try:
            # Set the volume before switching output, or the first audio lands at the old level.
            self._level_hub(sink.name, devices)
            self.backend.set_default(sink.name)
        except Exception:
            self.backend.destroy_sink(sink)  # nobody else holds it yet, so its engine would leak
            raise
        # Apps follow the default into the hub; pinning them here would
        # persist past this session.
        return sink

    def _retarget(self, devices: list[AudioDevice]) -> None:
        """Change which outputs the live session feeds. The session itself stays put."""
        self._prepare(devices)

        # One output to two: the session is still at 100 (the lone device carried the
        # level), so duck it before the new leg attaches or that output blasts at full volume.
        if self._hub_level(devices) < self._hub_level(self.session.devices):
            self._level_hub(self.session.sink.name, devices)

        self.backend.set_legs(self.session.sink, devices)
        self._level_hub(self.session.sink.name, devices)
        self._adopt(devices)
        self._wake_apps()

    def _prepare(self, devices: list[AudioDevice]) -> None:
        """Unmute each output and put it back at its own level."""
        for d in devices:
            if d.sink:
                try:
                    self.backend.set_mute(d.sink, False)
                    self.backend.set_volume(d.sink, d.volume)
                except Exception as e:
                    log.warning("Failed to configure %s: %s", d.name, e)

    def _adopt(self, devices: list[AudioDevice]) -> None:
        """Record who the session is for, now that the routing matches."""
        self.session.devices = devices
        self.targets = {d.id for d in devices}
        # The page reads who is in the session off each device.
        self.emit("devices-changed", list(self.devices.values()))
        self._set_state(SessionState.ACTIVE)

    def streams(self) -> list[dict]:
        """What is playing, each with the devices it was routed to (None: following)."""
        live = self.backend.list_streams()
        ids = {s["id"] for s in live}
        for s in live:
            if s.get("exe"):
                self._exe[s["id"]] = s["exe"]
        for sid in [s for s in self.overrides if s not in ids]:
            del self.overrides[sid]
        self._sweep_pins(live)
        hub = self.session.is_active and self.backend.health().engine == "hub"
        out = []
        for s in live:
            if hub:
                # In hub mode an override is a capture-side choice: the app
                # should still play into the hub, whatever it is routed to.
                expected = self.active_sink()
            elif s["id"] in self.overrides:
                target = self.devices.get(self.overrides[s["id"]][0])
                expected = target.sink if target else None
            elif self.session.is_active:
                expected = self.active_sink()
            else:
                expected = None
            # Some apps pick an output only when opening audio, so an accepted route
            # may not apply until the app reopens its stream.
            stuck = bool(s.get("active") and expected and s.get("endpoint") != expected)
            out.append({
                **s,
                "devices": list(self.overrides[s["id"]]) if s["id"] in self.overrides else None,
                "stuck": stuck,
            })
        return out

    def _sync_apps(self, stop: threading.Event | None = None) -> None:
        """Hand the backend each app playing into the hub with its outputs (override,
        else every session device); push the app list if it changed. Hub mode only.
        Never raises. A paused app stays routed for APP_IDLE_S, so resuming is instant.
        The routes are worked out under `_lock`, but applied outside it: starting an
        app's capture can block for seconds. Called from the poll thread only, with its
        `stop`: a poll stopped meanwhile must not sync (or push) the next session."""
        # ponytail: 1 s poll; IAudioSessionNotification if the delay before a new app is heard matters.
        try:
            with self._lock:
                if stop is not None and stop.is_set():
                    return
                if not (self.session.is_active and self.session.sink
                        and self.backend.health().engine == "hub"):
                    self._app_active.clear()
                    return
                out = self.streams()
                hub = self.active_sink()
                session_ids = [d.id for d in self.session.devices]
                now = time.monotonic()
                live = {s["id"]: s for s in out}
                for s in out:
                    if s.get("active") and s.get("endpoint") == hub:
                        self._app_active[s["id"]] = now
                # An idle app's endpoint is whichever stale session won the dedupe,
                # so only an *active* one elsewhere means it left the hub.
                self._app_active = {
                    pid: t for pid, t in self._app_active.items()
                    if pid in live and now - t < APP_IDLE_S
                    and not (live[pid].get("active") and live[pid].get("endpoint") != hub)
                }
                routes = {
                    pid: self.overrides.get(pid) or session_ids
                    for pid in self._app_active
                }
                # Tags the routes with this session, so a restart meanwhile drops them.
                gen = self.backend.apps_gen
                changed = out != self._last_streams
                if changed:
                    self._last_streams = out
            self.backend.set_app_routes(routes, gen=gen)
            if changed:
                # set_app_routes can block; a restart meanwhile has pushed its own list.
                with self._lock:
                    if stop is not None and stop.is_set():
                        return
                    self.emit("streams-changed", out)
        except Exception as e:
            log.warning("Failed to sync per-app routes: %s", e)

    def _wake_apps(self) -> None:
        """Have the poll thread re-sync now. Safe under `_lock`; a no-op with no poll."""
        if self._app_wake:
            self._app_wake.set()

    def _start_app_poll(self) -> None:
        self._app_poll_stop = stop = threading.Event()
        self._app_wake = wake = threading.Event()
        self._app_poll = threading.Thread(
            target=self._poll_apps, args=(stop, wake), name="pipemix-app-poll", daemon=True)
        self._app_poll.start()
        self._app_polls = [p for p in self._app_polls if p.is_alive()] + [self._app_poll]

    def _poll_apps(self, stop: threading.Event, wake: threading.Event) -> None:
        while not stop.is_set():
            self._sync_apps(stop)
            wake.wait(APP_POLL_S)
            wake.clear()

    def _sweep_pins(self, live: list[dict]) -> None:
        """Clear pins PipeMix left behind (an exe in `pinned_apps` with no override):
        an app that reopened under a new pid, or a previous run's pins. Never raises."""
        pinned = self.config.data["pinned_apps"]
        if not pinned:
            return
        kept = {self._exe.get(pid) for pid in self.overrides}
        changed = False
        for s in live:
            exe = s.get("exe")
            if not exe or exe not in pinned or exe in kept:
                continue
            try:
                self.backend.move_stream(s["id"], None)
            except Exception as e:
                log.warning("Failed to clear pin for %s: %s", exe, e)
                continue
            pinned.remove(exe)
            changed = True
        if changed:
            self.config.save()

    @locked
    def route_stream(self, stream_id: int, ids: list[str] | None) -> None:
        """Route a stream to some outputs, or clear the route with None.

        Hub mode only records the override: the app keeps playing into the hub and
        is captured out to its devices (None: all). Otherwise it pins the app to one
        device; None follows the machine default again."""
        devs = [d for d in (self.devices.get(i) for i in ids or []) if d and d.sink]
        exe = self._exe.get(stream_id)
        pinned = self.config.data["pinned_apps"]

        if self.session.is_active and self.backend.health().engine == "hub":
            if devs:
                self.overrides[stream_id] = [d.id for d in devs]
            else:
                self.overrides.pop(stream_id, None)
            # A pin left from idle or leader mode would pull the app off the hub.
            if exe and exe in pinned:
                self.backend.move_stream(stream_id, None)
                pinned.remove(exe)
                self.config.save()
            log.info("Manual route: stream %d → %s", stream_id, [d.id for d in devs] or "session")
            self._wake_apps()
            return

        # ponytail: leader mode has no silent sink to capture apps from, so it
        # keeps one pin per app; revisit if per-app fan-out matters there.
        if len(devs) > 1:
            raise BackendError("On Windows an app can be routed to one output at a time.")

        target = devs[0].sink if devs else None
        self.backend.move_stream(stream_id, target)
        if devs:
            self.overrides[stream_id] = [devs[0].id]
            if exe and exe not in pinned:
                pinned.append(exe)
                self.config.save()
        else:
            self.overrides.pop(stream_id, None)
            if exe and exe in pinned:
                pinned.remove(exe)
                self.config.save()
        log.info("Manual route: stream %d → %s", stream_id, target)

    @locked
    def stop_sharing(self) -> None:
        self._set_state(SessionState.STOPPING)
        # Never join here: the poll thread may be waiting on this very lock.
        if self._app_poll_stop:
            self._app_poll_stop.set()
            self._app_wake.set()
        try:
            if self.prev_default:
                try:
                    self.backend.set_default(self.prev_default)
                    self.config.data["prev_default"] = None
                    self.config.save()
                except Exception as e:
                    log.warning("Could not restore original default sink: %s", e)

            if self.session.sink:
                self.backend.destroy_sink(self.session.sink)

            self.session.sink = None
            self.session.devices = []
            self.targets.clear()
            self.overrides.clear()
            self._app_active.clear()
            self._last_streams = None
            try:
                self._sweep_pins(self.backend.list_streams())
            except Exception as e:
                log.warning("Failed to sweep leftover per-app pins: %s", e)
            self.emit("devices-changed", list(self.devices.values()))
            self._set_state(SessionState.IDLE)
        except Exception as e:
            log.error("Error while stopping session: %s", e)
            self._set_state(SessionState.ERROR)
            raise

    # ---------- Endpoint hotplug ----------

    def _on_connect(self, device_id: str) -> None:
        log.info("Endpoint connected: %s", device_id)
        self.refresh()
        if device_id in self.targets:
            self._rebuild()

    @locked
    def _on_disconnect(self, device_id: str) -> None:
        log.info("Endpoint disconnected: %s", device_id)

        dev = self.devices.get(device_id)
        if dev:
            dev.connected = False
            dev.sink = None
        self.emit("devices-changed", list(self.devices.values()))

        for sid, ids in list(self.overrides.items()):
            if device_id in ids:
                ids = [i for i in ids if i != device_id]
                if ids:
                    self.overrides[sid] = ids
                else:
                    del self.overrides[sid]

        if not (self.session.is_active and device_id in self.targets):
            # It may still have been an app's override target.
            self._wake_apps()
            return

        log.warning("An active sharing device (%s) disconnected.", device_id)

        if self.backend.health().engine == "leader" and self.backend.leader == device_id:
            self._reelect_leader()
            return

        remaining = [
            d for d in (self.devices.get(t) for t in self.targets if t != device_id)
            if d and d.connected and d.sink
        ]

        # Drop just that leg; the session stays the default sink, so no stream has to move.
        self._set_state(SessionState.REPAIRING)
        self.backend.set_legs(self.session.sink, remaining)
        self.session.devices = remaining
        if remaining:
            self._level_hub(self.session.sink.name, remaining)

        if not remaining:
            log.warning("No sharing devices left connected.")
            return

        log.info("Continuing on: %s", [d.name for d in remaining])
        self._set_state(SessionState.ACTIVE)
        # After ACTIVE: `_sync_apps` does nothing while the session repairs.
        self._wake_apps()

    def _reelect_leader(self) -> None:
        """
        The leader can vanish mid-session. Rebuild on a survivor (the capture source
        can't be swapped in place; ~200 ms gap), or stop sharing if none is left.
        """
        survivors = [
            d for d in (self.devices.get(t) for t in self.targets if t != self.backend.leader)
            if d and d.connected and d.sink
        ]

        if not survivors:
            log.warning("Leader disconnected and no other sharing device is left.")
            self.stop_sharing()
            self._set_state(SessionState.ERROR)
            return

        log.warning("Leader disconnected — electing from %s.", [d.name for d in survivors])
        self._set_state(SessionState.REPAIRING)
        try:
            self.backend.destroy_sink(self.session.sink)
            self.session.sink = self._route(survivors)
            self._adopt(survivors)
            log.info("New leader elected: %s", self.active_sink())
        except Exception as e:
            log.error("Leader re-election failed: %s", e)
            # The old sink is already destroyed: end the session so the next start is fresh.
            try:
                self.stop_sharing()
            except Exception:
                log.exception("Could not clean up after the failed re-election")
            self._set_state(SessionState.ERROR)
            raise

    @locked
    def _rebuild(self) -> None:
        """Feed the session back to each target that has come back, as it comes back."""
        ready = [
            d for d in (self.devices.get(i) for i in self.targets)
            if d and d.connected and d.sink
        ]
        if not (self.session.sink and ready):
            return

        log.info("Reconnected — feeding %s again.", [d.name for d in ready])
        self._retarget(ready)

    def _set_state(self, state: SessionState) -> None:
        if self.session.state != state:
            log.debug("State: %s → %s", self.session.state.value, state.value)
            self.session.state = state
            self.emit("state-changed", state)
