from __future__ import annotations

import functools
import logging
import threading
from typing import TYPE_CHECKING, Callable

from pipemix.macos import battery
from pipemix.models import AudioDevice, SessionState, SharingSession, VirtualSink
from pipemix.models import BackendError, BackendHealth
from pipemix.config import ConfigManager

if TYPE_CHECKING:
    from pipemix.macos.backend import CoreAudioBackend

log = logging.getLogger(__name__)

APP_POLL_S = 1.0
BATTERY_POLL_S = 60.0


class SignalEmitter:
    """Minimal connect/emit, one signal name at a time, any number of handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = {}

    def connect(self, name: str, handler: Callable) -> None:
        self._handlers.setdefault(name, []).append(handler)

    def emit(self, name: str, *args) -> None:
        for handler in list(self._handlers.get(name, ())):
            try:
                # Emitter first, as GObject does: the Bridge handlers are shared with Linux.
                handler(self, *args)
            except Exception:
                log.exception("Handler for signal %r raised", name)


def locked(fn):
    """Serialize routing: the page calls in on one thread, the notify worker on another."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


class Controller(SignalEmitter):

    def __init__(self, backend: CoreAudioBackend, config: ConfigManager | None = None,
                 monitor=None) -> None:
        super().__init__()
        self.backend = backend
        self.config = config or ConfigManager()

        if monitor is None:
            from pipemix.macos.notify import DeviceMonitor
            monitor = DeviceMonitor()
        self.monitor = monitor
        self.monitor.on_connect = self._on_connect
        self.monitor.on_disconnect = self._on_disconnect

        self.session = SharingSession()
        self.devices: dict[str, AudioDevice] = {}
        self.prev_default: str | None = None
        # Device UIDs we want back if they reconnect mid-session.
        self.targets: set[str] = set()
        # pid -> device UIDs, a subset of the session's outputs. Other apps follow the session.
        self.overrides: dict[int, list[str]] = {}
        self._last_streams: list[dict] | None = None
        self.batteries: dict[str, int] = {}  # Bluetooth address -> percent

        self._polls_stop = threading.Event()

        # Each row already carries its device's own level, so master starts at 100.
        saved = self.config.data.get("master")
        self.master_volume = saved if isinstance(saved, int) else 100
        self._unmuted_master: int | None = None

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
            log.error("Failed to start device monitor: %s", e)

        self.refresh()
        self._polls_stop.clear()
        self._poll("pipemix-apps", APP_POLL_S, self._sync_apps)
        self._poll("pipemix-battery", BATTERY_POLL_S, self._read_batteries, now=True)

    def _poll(self, name: str, every: float, fn, now: bool = False) -> None:
        def run() -> None:
            if now:
                fn()
            while not self._polls_stop.wait(every):
                fn()
        threading.Thread(target=run, name=name, daemon=True).start()

    def stop(self) -> None:
        # Never join the polls: either may be waiting on the routing lock.
        self._polls_stop.set()
        # The hub, not the state: a session whose outputs all dropped is REPAIRING.
        if self.session.sink:
            try:
                self.stop_sharing()
            except Exception as e:
                log.error("Failed to stop sharing during shutdown: %s", e)
        try:
            self.backend.clear_apps()
        except Exception as e:
            log.warning("Failed to release per-app captures: %s", e)
        self.config.data["master"] = self.master_volume
        self.config.save()
        self.monitor.stop()

    def clean_orphans(self) -> None:
        """Undo a crashed run: a hub nobody owns, and a default output still on it."""
        try:
            stranded = self.config.data.get("prev_default")
            if stranded:
                if stranded in {d.id for d in self.backend.list_outputs()}:
                    log.warning("Previous run was interrupted — restoring default output to %s.",
                                stranded)
                    self.backend.set_default(stranded)
                else:
                    log.warning("Previous run was interrupted, but its default output "
                                "(%s) is no longer connected.", stranded)
                self.config.data["prev_default"] = None
                self.config.save()

            if self.session.is_active:
                return

            orphans = self.backend.find_orphans()
            if orphans:
                if self.backend.get_default() in {o.name for o in orphans}:
                    # Move off the hub before it goes, or macOS picks for us.
                    target = self.backend.restore_target([])
                    if target:
                        self.backend.set_default(target)
                for o in orphans:
                    log.warning("Removing leftover PipeMix output %s", o.name)
                    self.backend.destroy_sink(o)
        except Exception as e:
            log.error("Error during crash recovery: %s", e)

    # ---------- Devices ----------

    def refresh(self) -> None:
        self.emit("health-changed", self.backend.health())
        try:
            found: dict[str, AudioDevice] = {}
            for dev in self.backend.list_outputs():
                dev.name = self.config.device_name(dev.id, dev.name)
                dev.volume = self._volume_of(dev.id, dev.sink)
                dev.battery = self._battery_of(dev)
                found[dev.id] = dev

            # Keep the ones the session waits on, so the page shows them as dropped out.
            for dev_id in self.targets - found.keys():
                old = self.devices.get(dev_id)
                if old:
                    old.connected = False
                    old.sink = None
                    found[dev_id] = old

            self.devices = found
            self.emit("devices-changed", list(found.values()))
            self.emit("streams-changed", self.streams())
        except Exception as e:
            log.error("Failed to refresh devices: %s", e)

    def _battery_of(self, dev: AudioDevice) -> int | None:
        address = battery.address_of(dev.id)
        return self.batteries.get(address) if address else None

    def _read_batteries(self) -> None:
        if not any(battery.address_of(i) for i in self.devices):
            return
        self.batteries = battery.read()
        changed = False
        for dev in self.devices.values():
            level = self._battery_of(dev)
            if dev.connected and level != dev.battery:
                dev.battery = level
                changed = True
        if changed:
            self.emit("devices-changed", list(self.devices.values()))

    def _volume_of(self, dev_id: str, sink: str | None) -> int:
        """Keep the volume we already know; otherwise ask the device, else 50%."""
        if dev_id in self.devices:
            return self.devices[dev_id].volume
        if not sink:
            return 50
        try:
            return self.backend.get_volume(sink)
        except Exception:
            return 50

    def set_device_volume(self, dev_id: str, volume: int) -> None:
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

    def set_master_volume(self, volume: int) -> None:
        self.master_volume = volume
        self._unmuted_master = None

        solo = self._solo()
        if solo:
            # A lone output carries the level itself, and its row moves with master.
            self.set_device_volume(solo.id, volume)
            self.emit("devices-changed", list(self.devices.values()))
            return

        target = self.active_sink() if self.session.is_active else self.prev_default
        if target:
            try:
                self.backend.set_volume(target, volume)
            except Exception as e:
                log.error("Failed to set master volume on %s: %s", target, e)

    def volume_key(self, action: str, step: float) -> bool:
        """While sharing the hub has no volume of its own, so the volume keys move
        master. False when not sharing: macOS handles the key itself."""
        if not self.session.is_active:
            return False
        if action == "mute":
            if self._unmuted_master is None:
                restore, level = self.master_volume, 0
            else:
                restore, level = None, self._unmuted_master
            self.set_master_volume(level)
            self._unmuted_master = restore
        else:
            base = self._unmuted_master if self._unmuted_master is not None else self.master_volume
            delta = step if action == "up" else -step
            # Snap to the grid, as macOS does, so up then down lands where it began.
            self.set_master_volume(max(0, min(100, round(round(base / step) * step + delta))))
        self.emit("master-changed", self.master_volume)
        return True

    def _solo(self) -> AudioDevice | None:
        return self.session.devices[0] if len(self.session.devices) == 1 else None

    def _level_hub(self, sink: str, devices: list[AudioDevice]) -> None:
        """Set the hub's level. With one output, master and that device's fader are
        the same handle, so the hub steps aside rather than multiplying them."""
        if len(devices) == 1:
            self.master_volume = devices[0].volume
        try:
            self.backend.set_volume(sink, self._hub_level(devices))
        except Exception as e:
            log.warning("Failed to set the level on %s: %s", sink, e)

    def _hub_level(self, devices: list[AudioDevice]) -> int:
        return 100 if len(devices) == 1 else self.master_volume

    def active_sink(self) -> str | None:
        return self.session.sink.name if self.session.sink else None

    # ---------- Sharing ----------

    @locked
    def start_sharing(self, devices: list[AudioDevice]) -> None:
        if not devices:
            log.warning("start_sharing() called with no devices.")
            return

        if self.session.sink:
            # Only the legs move; the outputs that stay keep playing.
            self._retarget(devices)
            return

        log.info("Starting session with %d device(s)...", len(devices))
        self._set_state(SessionState.STARTING)
        try:
            self.prev_default = self.backend.restore_target(devices)
            self.config.data["prev_default"] = self.prev_default
            self.config.save()
            self.session.sink = self._route(devices)
            self._adopt(devices)
            log.info("Session active: %s", self.active_sink())
        except Exception as e:
            log.error("Failed to start session: %s", e)
            self._set_state(SessionState.ERROR)
            self.stop_sharing()
            raise

    def _route(self, devices: list[AudioDevice]) -> VirtualSink:
        self._prepare(devices)
        sink = self.backend.create_sink(devices)
        # Level first, or the first moment of audio lands at whatever level the devices were.
        self._level_hub(sink.name, devices)
        self.backend.set_default(sink.name)
        return sink

    def _retarget(self, devices: list[AudioDevice]) -> None:
        self._prepare(devices)
        # From one output to two the hub is still at 100: duck it before the new leg
        # attaches, or that output gets a blast at full volume.
        if self._hub_level(devices) < self._hub_level(self.session.devices):
            self._level_hub(self.session.sink.name, devices)
        self.backend.set_legs(self.session.sink, devices)
        self._level_hub(self.session.sink.name, devices)
        self._adopt(devices)
        self._prune_overrides()
        self._sync_apps()

    def _prepare(self, devices: list[AudioDevice]) -> None:
        for d in devices:
            if d.sink:
                try:
                    self.backend.set_mute(d.sink, False)
                    self.backend.set_volume(d.sink, d.volume)
                except Exception as e:
                    log.warning("Failed to configure %s: %s", d.name, e)

    def _adopt(self, devices: list[AudioDevice]) -> None:
        self.session.devices = devices
        self.targets = {d.id for d in devices}
        self.emit("devices-changed", list(self.devices.values()))
        self._set_state(SessionState.ACTIVE)

    # ---------- Per-app routing ----------

    def streams(self) -> list[dict]:
        """What is playing, each with the devices it was routed to (None: following)."""
        live = self.backend.list_streams()
        ids = {s["id"] for s in live}
        for pid in [p for p in self.overrides if p not in ids]:
            del self.overrides[pid]
        out = []
        for s in live:
            handled = s["id"] in self.overrides or s.get("mute")
            problem = self.backend.route_problem(s["id"], s.get("active")) if handled else None
            out.append({
                **s,
                "devices": list(self.overrides[s["id"]]) if s["id"] in self.overrides else None,
                "stuck": bool(problem),
                "hint": problem,
            })
        return out

    @locked
    def _sync_apps(self) -> None:
        try:
            out = self.streams()
            if out != self._last_streams:
                self._last_streams = out
                self.emit("streams-changed", out)
        except Exception as e:
            log.warning("Failed to sync apps: %s", e)

    def _prune_overrides(self) -> None:
        """Keep each app's outputs inside the session; one left with none follows it."""
        live = {d.id for d in self.session.devices}
        changed = False
        for pid, ids in list(self.overrides.items()):
            kept = [i for i in ids if i in live]
            if kept != ids:
                changed = True
                if kept:
                    self.overrides[pid] = kept
                else:
                    del self.overrides[pid]
        if changed:
            self._apply_routes()

    def _apply_routes(self) -> None:
        try:
            self.backend.set_app_routes(dict(self.overrides))
        except BackendError as e:
            log.error("Per-app routing failed: %s", e)

    @locked
    def route_stream(self, stream_id: int, ids: list[str] | None) -> None:
        """Play one app on some of the session's outputs, or (None) follow the session."""
        if not self.session.is_active:
            raise BackendError("Start sharing first — apps are routed to the outputs being shared.")
        live = {d.id for d in self.session.devices}
        devs = [i for i in ids or [] if i in live]
        before = self.overrides.get(stream_id)
        if devs and set(devs) != live:
            self.overrides[stream_id] = devs
        else:
            self.overrides.pop(stream_id, None)
        try:
            # Returns at once: taps are made on the backend's worker.
            self.backend.set_app_routes(dict(self.overrides))
        except BackendError:
            if before:
                self.overrides[stream_id] = before
            else:
                self.overrides.pop(stream_id, None)
            raise
        log.info("Manual route: app %d → %s", stream_id, self.overrides.get(stream_id) or "session")
        self._sync_apps()

    @locked
    def stop_sharing(self) -> None:
        self._set_state(SessionState.STOPPING)
        try:
            if self.prev_default:
                try:
                    self.backend.set_default(self.prev_default)
                    self.config.data["prev_default"] = None
                    self.config.save()
                except Exception as e:
                    log.warning("Could not restore original default output: %s", e)

            self.overrides.clear()
            self._apply_routes()

            if self.session.sink:
                self.backend.destroy_sink(self.session.sink)

            self.session.sink = None
            self.session.devices = []
            self.targets.clear()
            self.config.data["master"] = self.master_volume
            self.config.save()
            self.emit("devices-changed", list(self.devices.values()))
            self._set_state(SessionState.IDLE)
        except Exception as e:
            log.error("Error while stopping session: %s", e)
            self._set_state(SessionState.ERROR)
            raise

    # ---------- Hotplug ----------

    def _on_connect(self, device_id: str) -> None:
        log.info("Device connected: %s", device_id)
        self.refresh()
        if device_id in self.targets:
            self._rebuild()

    @locked
    def _on_disconnect(self, device_id: str) -> None:
        log.info("Device disconnected: %s", device_id)

        dev = self.devices.get(device_id)
        if dev:
            dev.connected = False
            dev.sink = None
        self.emit("devices-changed", list(self.devices.values()))

        if not (self.session.is_active and device_id in self.targets):
            return

        log.warning("An active sharing device (%s) disconnected.", device_id)
        remaining = [
            d for d in (self.devices.get(t) for t in self.targets if t != device_id)
            if d and d.connected and d.sink
        ]

        # Drop that one leg. The hub stays the default, so the rest keep playing.
        self._set_state(SessionState.REPAIRING)
        try:
            self.backend.set_legs(self.session.sink, remaining)
        except Exception as e:
            log.error("Failed to drop %s from the hub: %s", device_id, e)
        self.session.devices = remaining
        self._prune_overrides()

        if not remaining:
            log.warning("No sharing devices left connected.")
            return

        self._level_hub(self.session.sink.name, remaining)
        log.info("Continuing on: %s", [d.name for d in remaining])
        self._set_state(SessionState.ACTIVE)

    @locked
    def _rebuild(self) -> None:
        """Feed the hub to each target that has come back."""
        ready = [
            d for d in (self.devices.get(i) for i in self.targets)
            if d and d.connected and d.sink
        ]
        if not (self.session.sink and ready):
            return

        log.info("Reconnected — feeding %s again.", [d.name for d in ready])
        self._retarget(ready)
        # macOS makes reconnecting AirPods the default output; take it back.
        if self.backend.get_default() != self.active_sink():
            try:
                self.backend.set_default(self.active_sink())
            except Exception as e:
                log.warning("Could not make the hub the default again: %s", e)

    def _set_state(self, state: SessionState) -> None:
        if self.session.state != state:
            log.debug("State: %s → %s", self.session.state.value, state.value)
            self.session.state = state
            self.emit("state-changed", state)
