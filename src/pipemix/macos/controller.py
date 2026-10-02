"""
PipeMix — Controller (macOS).

The state machine: owns the SharingSession, reacts to device hotplug, runs
crash recovery, drives the backend, and pushes updates to the UI as signals.
All business logic lives here; the UI only triggers and listens.

Forked from `pipemix.windows.controller`, minus what macOS has no use for:

- No leader mode. The hub is always an aggregate device we own, so there is
  never a real device to re-elect when one drops.
- No pins. An app routed by hand is captured by a process tap only for as
  long as PipeMix runs (see `apps.py`), so nothing persists to sweep up.
- Crash recovery also has an orphan to clean: an aggregate device outlives
  the process that made it.

And a few things only the Mac build does: the master level is remembered
between runs (starting at 100, since each row already carries its device's
own level), the volume keys drive it while sharing, and Bluetooth battery
levels are polled from System Information.
"""

from __future__ import annotations

import functools
import logging
import re
import threading
import time
from typing import TYPE_CHECKING

from pipemix.macos import battery

from pipemix.models import AudioDevice, SessionState, SharingSession, VirtualSink
from pipemix.linux.services.backend import BackendError, BackendHealth
from pipemix.linux.services.config.config_manager import ConfigManager
from pipemix.windows.signal import SignalEmitter

if TYPE_CHECKING:
    from pipemix.macos.backend import CoreAudioBackend

log = logging.getLogger(__name__)

# How often the app list is reconciled with what is playing, and how often
# Bluetooth battery levels are re-read.
APP_POLL_S = 1.0
BATTERY_POLL_S = 60.0


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

        # Apps the user routed by hand (pid -> device UIDs, a subset of the
        # session's outputs). Everything else follows the session.
        self.overrides: dict[int, list[str]] = {}
        self._last_streams: list[dict] | None = None

        # Bluetooth address -> battery percent, refreshed in the background.
        self.batteries: dict[str, int] = {}

        self._polls: list[threading.Thread] = []
        self._polls_stop = threading.Event()

        saved = self.config.data.get("master")
        self.master_volume = saved if isinstance(saved, int) else 100
        self._unmuted_master: int | None = None

        # The page calls in on pywebview's thread while the notify worker
        # fires on its own, and both change which outputs the session feeds.
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
        t = threading.Thread(target=run, name=name, daemon=True)
        t.start()
        self._polls.append(t)

    def stop(self) -> None:
        # Never join the polls: either may be waiting on the routing lock.
        self._polls_stop.set()
        # The hub, not the state: a session whose every output dropped sits in
        # REPAIRING, and its hub still has to go and the default come back.
        if self.session.sink:
            try:
                self.stop_sharing()
            except Exception as e:
                log.error("Failed to stop sharing during shutdown: %s", e)
        try:
            self.backend.clear_apps()  # unmute anything muted from the Apps tab
        except Exception as e:
            log.warning("Failed to release per-app captures: %s", e)
        self.config.data["master"] = self.master_volume
        self.config.save()
        self.monitor.stop()

    def clean_orphans(self) -> None:
        """Undo what a crashed run left behind: a hub nobody owns, and a
        default output still pointing at it."""
        try:
            stranded = self.config.data.get("prev_default")
            if stranded:
                live = {d.id for d in self.backend.list_outputs()}
                if stranded in live:
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
                current = self.backend.get_default()
                if current in {o.name for o in orphans}:
                    # Move off the hub before it disappears, or macOS picks for us.
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
        """Re-enumerate outputs. `backend.list_outputs()` is already the whole truth."""
        self.emit("health-changed", self.backend.health())
        try:
            found: dict[str, AudioDevice] = {}
            for dev in self.backend.list_outputs():
                dev.name = self.config.device_name(dev.id, dev.name)
                dev.volume = self._volume_of(dev.id, dev.sink)
                dev.battery = self._battery_of(dev)
                found[dev.id] = dev

            # Keep the ones the session is waiting on, so the page can show
            # them as dropped out rather than forgetting them.
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
        """Re-read Bluetooth battery levels; push the page only when one moved."""
        if not any(battery.address_of(i) for i in self.devices):
            return
        levels = battery.read()
        self.batteries = levels
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
        self._unmuted_master = None

        solo = self._solo()
        if solo:
            # The session is transparent for a lone output, so the level
            # belongs on the device — and its row has to move with the master.
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

    def volume_key(self, action: str, step: float) -> bool:
        """A volume key was pressed. While sharing, the hub is the default
        output and has no volume of its own, so the key moves master; returns
        False otherwise, and macOS handles the key itself."""
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
            level = round((round(base / step) * step + delta))
            self.set_master_volume(max(0, min(100, level)))
        self.emit("master-changed", self.master_volume)
        return True

    def _solo(self) -> AudioDevice | None:
        """The one output the session is feeding, when there is only one."""
        return self.session.devices[0] if len(self.session.devices) == 1 else None

    def _level_hub(self, sink: str, devices: list[AudioDevice]) -> None:
        """
        Set the session's own level, and keep master honest for a lone output.

        With one output the master fader and that device's fader are two
        handles on the same thing, so the hub steps aside and the device
        carries the level. If both carried one they would multiply, and the
        fader would feel dead until it was most of the way up.
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

    # ---------- Presets ----------

    @property
    def presets(self) -> dict:
        return self.config.data["presets"]

    @property
    def last_preset(self) -> str | None:
        return self.config.data["last_preset"]

    @last_preset.setter
    def last_preset(self, preset_id: str | None) -> None:
        self.config.data["last_preset"] = preset_id
        self.config.save()

    def save_preset(self, name: str, devices: list[str]) -> str:
        preset_id = re.sub(r"[^a-z0-9_]", "", name.lower().replace(" ", "_"))
        if not preset_id:
            preset_id = f"preset_{int(time.time())}"
        self.config.save_preset(preset_id, name, devices)
        self.config.save()
        log.info("Saved preset '%s' (%s): %s", name, preset_id, devices)
        return preset_id

    def delete_preset(self, preset_id: str) -> None:
        self.config.delete_preset(preset_id)
        self.config.save()

    # ---------- Sharing ----------

    @locked
    def start_sharing(self, devices: list[AudioDevice]) -> None:
        if not devices:
            log.warning("start_sharing() called with no devices.")
            return

        if self.session.sink:
            # The hub is already up, so only its legs move. Nothing is torn
            # down, and the outputs that are staying keep playing.
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
        """Stand up the hub and make it the default output, so every app follows."""
        self._prepare(devices)
        sink = self.backend.create_sink(devices)

        # Set the level before switching output, or the first moment of audio
        # lands at whatever level the devices happened to be at.
        self._level_hub(sink.name, devices)

        self.backend.set_default(sink.name)
        return sink

    def _retarget(self, devices: list[AudioDevice]) -> None:
        """Change which outputs the live hub feeds. The hub itself stays put."""
        self._prepare(devices)

        # Going from one output to two, the hub is still at 100 because the
        # lone device was carrying the level. Duck it before the new leg
        # attaches, or that output gets one blast at full volume.
        if self._hub_level(devices) < self._hub_level(self.session.devices):
            self._level_hub(self.session.sink.name, devices)

        self.backend.set_legs(self.session.sink, devices)
        self._level_hub(self.session.sink.name, devices)
        self._adopt(devices)
        self._prune_overrides()
        self._sync_apps()

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
        """Push the app list to the page when it changed. Never raises."""
        try:
            out = self.streams()
            if out != self._last_streams:
                self._last_streams = out
                self.emit("streams-changed", out)
        except Exception as e:
            log.warning("Failed to sync apps: %s", e)

    def _prune_overrides(self) -> None:
        """Keep each app's outputs inside the session; an app left with none
        follows the session again."""
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
        """Send one app to some of the session's outputs, or (None) let it
        follow the session again."""
        if not self.session.is_active:
            raise BackendError("Start sharing first — apps are routed to the outputs being shared.")
        live = {d.id for d in self.session.devices}
        devs = [i for i in ids or [] if i in live]
        before = self.overrides.get(stream_id)
        if devs and set(devs) != live:
            self.overrides[stream_id] = devs
        else:
            # Every session output is just what following the session means.
            self.overrides.pop(stream_id, None)
        try:
            # Returns at once: the taps are made on the backend's own thread,
            # since the first one waits on macOS's permission prompt.
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

            # Routed apps go back to the default output before the hub goes.
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

        # Drop that one leg. The hub stays the default output either way, so
        # whatever is playing into it keeps playing on the rest.
        self._set_state(SessionState.REPAIRING)
        try:
            self.backend.set_legs(self.session.sink, remaining)
        except Exception as e:
            log.error("Failed to drop %s from the hub: %s", device_id, e)
        self.session.devices = remaining
        self._prune_overrides()
        if remaining:
            self._level_hub(self.session.sink.name, remaining)

        if not remaining:
            log.warning("No sharing devices left connected.")
            return

        log.info("Continuing on: %s", [d.name for d in remaining])
        self._set_state(SessionState.ACTIVE)

    @locked
    def _rebuild(self) -> None:
        """Feed the hub to each target that has come back, as it comes back."""
        ready = [
            d for d in (self.devices.get(i) for i in self.targets)
            if d and d.connected and d.sink
        ]
        if not (self.session.sink and ready):
            return

        log.info("Reconnected — feeding %s again.", [d.name for d in ready])
        self._retarget(ready)
        # If every output had dropped, macOS will have moved the default off
        # the hub; take it back now that there is something to play on.
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
