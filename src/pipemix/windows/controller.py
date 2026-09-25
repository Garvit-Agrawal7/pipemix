"""
PipeMix — Controller (Windows).

The state machine: owns the SharingSession, reacts to endpoint hotplug, runs
crash recovery, drives the backend, and pushes updates to the UI as signals.
All business logic lives here; the UI only triggers and listens.

Forked from `pipemix.linux.controller`. What changed and why:

- `GObject.Object` → `SignalEmitter` (no GLib on Windows).
- `DeviceMonitor` comes from `wasapi.notify` and reports endpoint ids, not
  MACs; there is no `on_property` (no Bluetooth battery plumbing here).
- The whole MAC layer is gone. `sink_to_mac`, `path_to_mac`,
  `resolve_bt_sinks` and the `_resolve_retry` chain existed because a
  PipeWire sink name changes per session; a Windows endpoint id is stable
  across reconnects, so `AudioDevice.id` *is* `AudioDevice.sink` and
  `_on_connect` acts immediately. `refresh()` no longer merges anything:
  `backend.list_outputs()` is already the whole truth.
- New: leader re-election. In leader mode the source endpoint is a real
  device that can vanish; `_on_disconnect` elects a survivor and rebuilds the
  session on it. Hub mode has no leader, so this path never triggers there.
"""

from __future__ import annotations

import functools
import logging
import re
import threading
import time
from typing import TYPE_CHECKING

from pipemix.models import AudioDevice, SessionState, SharingSession, VirtualSink
from pipemix.linux.services.backend import BackendHealth
from pipemix.linux.services.config.config_manager import ConfigManager
from pipemix.windows.signal import SignalEmitter
from pipemix.windows.wasapi.notify import DeviceMonitor

if TYPE_CHECKING:
    from pipemix.windows.backend import WasapiBackend

log = logging.getLogger(__name__)


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

        # Streams the user routed by hand, so a rebuild does not drag them back.
        self.overrides: dict[int, str] = {}

        self.master_volume = 50

        # The page calls in on pywebview's thread while the notification
        # worker fires on its own thread, and both change which outputs the
        # session feeds.
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
        if self.session.is_active:
            try:
                self.stop_sharing()
            except Exception as e:
                log.error("Failed to stop sharing during shutdown: %s", e)
        self.monitor.stop()

    def clean_orphans(self) -> None:
        """Restore a default output stranded by a crash.

        `backend.find_orphans()` always returns `[]` on Windows — nothing
        leaks, there are no kernel modules to unload. The real hazard is
        different: starting a session repoints the Windows default output,
        and if the process dies before `stop_sharing` restores it, the user
        is left hearing nothing with no clue why. `start_sharing` persists
        `prev_default` to config before it changes the default, and
        `stop_sharing` clears it after a clean restore — so a `prev_default`
        still on disk here means the last run never got that far.
        """
        try:
            for sink in self.backend.find_orphans():
                log.warning("Destroying orphaned sink: %s", sink.name)
                self.backend.destroy_sink(sink)

            stranded = self.config.data.get("prev_default")
            if not stranded:
                return

            live_ids = {d.id for d in self.backend.list_outputs()}
            if stranded not in live_ids:
                log.warning(
                    "Previous run was interrupted, but its default output "
                    "(%s) is no longer connected — leaving it as is.", stranded)
            else:
                log.warning(
                    "Previous run was interrupted — restoring default output to %s.", stranded)
                self.backend.set_default(stranded)

            self.config.data["prev_default"] = None
            self.config.save()
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

        solo = self._solo()
        if solo:
            # The session is transparent for a lone output, so the level
            # belongs on the device — and its row has to move with the master
            # row.
            self.set_device_volume(solo.id, volume)
            self.emit("devices-changed", list(self.devices.values()))
            return

        target = self.active_sink() if self.session.is_active else self.prev_default
        if target:
            try:
                self.backend.set_volume(target, volume)
            except Exception as e:
                log.error("Failed to set master volume on %s: %s", target, e)

    def _solo(self) -> AudioDevice | None:
        """The one output the session is feeding, when there is only one."""
        return self.session.devices[0] if len(self.session.devices) == 1 else None

    def _level_hub(self, sink: str, devices: list[AudioDevice]) -> None:
        """
        Set the session's own level, and keep master honest for a lone output.

        With one output the master fader and that device's fader are two
        handles on the same thing, so the session steps aside and the device
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
            # The session is already up, so only its legs move. Nothing is
            # torn down, and the outputs that are staying never drop out.
            self._retarget(devices)
            return

        log.info("Starting session with %d device(s)...", len(devices))
        self._set_state(SessionState.STARTING)
        try:
            self.prev_default = self.backend.get_default()
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
        """Stand up the session and point everything that is playing at it."""
        self._prepare(devices)
        sink = self.backend.create_sink(devices)

        # Set the volume before switching output, or the first moment of audio
        # lands at whatever level the new sink happened to be at.
        self._level_hub(sink.name, devices)

        self.backend.set_default(sink.name)
        self.backend.move_streams(sink.name, exclude=list(self.overrides))
        return sink

    def _retarget(self, devices: list[AudioDevice]) -> None:
        """Change which outputs the live session feeds. The session itself stays put."""
        self._prepare(devices)

        # Going from one output to two, the session is still at 100 because
        # the lone device was carrying the level. Duck it before the new leg
        # attaches, or that output gets one blast at full volume before the
        # level catches up.
        if self._hub_level(devices) < self._hub_level(self.session.devices):
            self._level_hub(self.session.sink.name, devices)

        self.backend.set_legs(self.session.sink, devices)
        self._level_hub(self.session.sink.name, devices)
        self._adopt(devices)

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
        self._set_state(SessionState.ACTIVE)

    def route_stream(self, stream_id: int, target: str) -> None:
        self.backend.move_stream(stream_id, target)
        self.overrides[stream_id] = target
        log.info("Manual route: stream %d → %s", stream_id, target)

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
                    log.warning("Could not restore original default sink: %s", e)

            if self.session.sink:
                self.backend.destroy_sink(self.session.sink)

            self.session.sink = None
            self.session.devices = []
            self.targets.clear()
            self.overrides.clear()
            self._set_state(SessionState.IDLE)
        except Exception as e:
            log.error("Error while stopping session: %s", e)
            self._set_state(SessionState.ERROR)
            raise

    # ---------- Endpoint hotplug ----------

    def _on_connect(self, device_id: str) -> None:
        log.info("Endpoint connected: %s", device_id)
        # A Windows endpoint id is stable and `list_outputs()` already knows
        # about it the moment it goes active, so there is no retry chain here
        # — that lives in `wasapi.notify` instead, a much smaller one.
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

        for sid in [s for s, sink in self.overrides.items() if sink == device_id]:
            del self.overrides[sid]

        if not (self.session.is_active and device_id in self.targets):
            return

        log.warning("An active sharing device (%s) disconnected.", device_id)

        if self.backend.health().engine == "leader" and self.backend.leader == device_id:
            self._reelect_leader()
            return

        remaining = [
            d for d in (self.devices.get(t) for t in self.targets if t != device_id)
            if d and d.connected and d.sink
        ]

        # Drop that one leg. The session stays the default sink either way, so
        # the streams playing into it keep playing and nothing has to be
        # moved.
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

    def _reelect_leader(self) -> None:
        """
        The leader is a real device and can vanish mid-session. Pick a
        survivor and stand a new session up on it — the fan-out has one
        capture source, and it cannot be swapped in place. Playback gaps for
        roughly 200 ms; accepted per the plan. If nothing is left, stop
        sharing entirely.
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
