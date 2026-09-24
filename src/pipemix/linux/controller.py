"""
PipeMix — Controller.

The state machine: owns the SharingSession, reacts to Bluetooth events, runs
crash recovery, drives the backend, and pushes updates to the UI as GObject
signals. All business logic lives here; the UI only triggers and listens.
"""

from __future__ import annotations

import functools
import logging
import queue
import re
import threading
import time
from typing import TYPE_CHECKING

from gi.repository import GLib, GObject

from pipemix.linux.models import AudioDevice, DeviceKind, SessionState, SharingSession, VirtualSink
from pipemix.linux.services.backend import BackendHealth
from pipemix.linux.services.bluetooth.device_monitor import DeviceMonitor
from pipemix.linux.services.config.config_manager import ConfigManager

if TYPE_CHECKING:
    from pipemix.linux.services.backend.pactl_backend import PactlBackend

log = logging.getLogger(__name__)

# How long to wait for PipeWire to publish a bluez sink after BlueZ says the
# device is connected. A slow headset can take several seconds; each try is one
# cheap pactl call and the first success exits, so the only cost of a generous
# ceiling is paid when the sink never shows up at all.
SINK_TRIES = 20
SINK_WAIT_MS = 250

# How long to let a burst of pactl events settle before reacting once. A
# session starting or a device plugging in fires several events back to back.
PW_SETTLE_MS = 100


def locked(fn):
    """Serialize routing: the page calls in on one thread, background events on another."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


class Controller(GObject.Object):

    __gsignals__ = {
        "state-changed":   (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "devices-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "health-changed":  (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "streams-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, backend: PactlBackend, config: ConfigManager | None = None) -> None:
        super().__init__()
        self.backend = backend
        self.config = config or ConfigManager()

        self.monitor = DeviceMonitor()
        self.monitor.on_connect = lambda mac: self._bg(self._on_connect, mac)
        self.monitor.on_disconnect = lambda mac: self._bg(self._on_disconnect, mac)
        self.monitor.on_property = lambda mac, key, value: self._bg(self._on_property, mac, key, value)

        # Background jobs (BlueZ events, pactl-subscribe events) land here and
        # run in order on the pipemix-events thread, off the GTK main loop.
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        # Event kinds seen since the last _pw_changed — worker-thread only, no lock.
        self._pending: set[str] = set()

        self.session = SharingSession()
        self.devices: dict[str, AudioDevice] = {}
        self.prev_default: str | None = None

        # Devices we want back if they reconnect or are plugged back in mid-session.
        self.targets: set[str] = set()

        # Streams the user routed by hand → the device ids they picked, so a
        # rebuild does not drag them back.
        self.overrides: dict[int, list[str]] = {}

        # A stream plays into one sink, so one routed to several outputs gets a
        # hub of its own, fed out the same way as the session's.
        self.hubs: dict[int, VirtualSink] = {}

        self.master_volume = 50

        # The page calls in on pywebview's thread while BlueZ and PipeWire
        # events land on the pipemix-events worker, and both change which
        # outputs the hub feeds.
        self._lock = threading.RLock()

        # Levels not yet sent, per sink: (level, unmute). pywebview runs each JS
        # call on its own thread, so a drag's ticks can overtake each other in
        # pactl; the worker only ever sends the latest.
        # ponytail: ticks waiting on _lock behind a routing change can still wake
        # out of order; stamp them with a sequence number if that ever shows.
        self._levels: dict[str, tuple[int, bool]] = {}

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
            log.error("Failed to start Bluetooth monitor: %s", e)

        threading.Thread(target=self._work, name="pipemix-events", daemon=True).start()
        self.refresh()
        threading.Thread(
            target=self.backend.watch, args=(lambda kind: self._bg(self._mark, kind),),
            name="pipemix-watch", daemon=True,
        ).start()

    def stop(self) -> None:
        if self.session.is_active:
            try:
                self.stop_sharing()
            except Exception as e:
                log.error("Failed to stop sharing during shutdown: %s", e)
        # Moving a stream onto the default sink clears its pin, so apps go back
        # to following the default instead of staying where we put them. Done
        # before the app hubs go, or their streams drop out for a beat.
        default = self.backend.get_default()
        if default:
            self.backend.move_streams(default)
        self._drop_hubs()
        self.monitor.stop()
        self.backend.unwatch()  # no pactl subscribe child may outlive the app

    # ---------- Background worker ----------

    def _bg(self, fn, *args) -> None:
        """Queue a job for the pipemix-events thread. Returns None, so it also
        works as a one-shot GLib.timeout_add callback."""
        self._jobs.put((fn, args))

    def _work(self) -> None:
        while True:
            fn, args = self._jobs.get()
            try:
                fn(*args)
            except Exception:
                log.exception("Background job failed: %s", fn)

    def clean_orphans(self) -> None:
        """Destroy virtual sinks left behind by a previous crash."""
        try:
            for sink in self.backend.find_orphans():
                log.warning("Destroying orphaned sink: %s", sink.name)
                self.backend.destroy_sink(sink)
        except Exception as e:
            log.error("Error during crash recovery: %s", e)

    # ---------- Devices ----------

    def refresh(self) -> None:
        """Re-enumerate outputs, merging PipeWire state with BlueZ state."""
        self.emit("health-changed", self.backend.health())
        try:
            outs = self.backend.list_outputs()
            found: dict[str, AudioDevice] = {}

            for dev in outs:
                # Bluetooth is keyed by MAC and rebuilt from BlueZ below.
                if dev.kind == DeviceKind.BLUETOOTH:
                    continue
                dev.name = self.config.device_name(dev.id, dev.name)
                dev.volume = self._volume_of(dev.id, dev.volume)
                found[dev.id] = dev

            bt = {dev.id: dev for dev in outs if dev.kind == DeviceKind.BLUETOOTH}
            for d in self.monitor.connected():
                mac = d["mac"]
                hit = bt.get(mac)
                found[mac] = AudioDevice(
                    id=mac,
                    name=self.config.device_name(mac, d["name"]),
                    sink=hit.sink if hit else None,
                    kind=DeviceKind.BLUETOOTH,
                    connected=True,
                    volume=self._volume_of(mac, hit.volume if hit else 50),
                )

            # A session output that dropped stays listed, offline, so the page can
            # show it reconnecting and it keeps its level for when it comes back.
            for i in self.targets - found.keys():
                dev = self.devices.get(i)
                if dev:
                    dev.connected, dev.sink = False, None
                    found[i] = dev

            self.devices = found
            self.emit("devices-changed", list(found.values()))

        except Exception as e:
            log.error("Failed to refresh devices: %s", e)

    def _volume_of(self, dev_id: str, fallback: int) -> int:
        """Keep the volume we already know; otherwise the caller's fallback."""
        return self.devices[dev_id].volume if dev_id in self.devices else fallback

    def _send(self, sink: str, level: int, unmute: bool = False) -> None:
        """Queue a level for the worker, replacing one still waiting. Callers hold _lock."""
        first = sink not in self._levels
        unmute = unmute or self._levels.get(sink, (0, False))[1]
        self._levels[sink] = (level, unmute)
        if first:
            self._bg(self._flush, sink)

    @locked  # or a tick mid-pactl could land after a routing change's direct set
    def _flush(self, sink: str) -> None:
        level, unmute = self._levels.pop(sink, (None, False))
        if level is None:
            return  # a direct set already replaced it
        try:
            if unmute:
                self.backend.set_mute(sink, False)
            self.backend.set_volume(sink, level)
        except Exception as e:
            log.warning("Failed to set the level on %s: %s", sink, e)

    @locked  # so a routing change can't land between picking the sink and queueing
    def set_device_volume(self, dev_id: str, volume: int, unmute: bool = False) -> None:
        dev = self.devices.get(dev_id)
        if not dev:
            return
        dev.volume = volume
        if self._solo() is dev:
            self.master_volume = volume
        if dev.connected and dev.sink:
            self._send(dev.sink, volume, unmute)

    @locked
    def set_master_volume(self, volume: int, unmute: bool = False) -> None:
        self.master_volume = volume

        solo = self._solo()
        if solo:
            # The hub is transparent for a lone output, so the level belongs on
            # the device — and its row has to move with the master row.
            self.set_device_volume(solo.id, volume, unmute)
            self.emit("devices-changed", list(self.devices.values()))
            return

        target = self.active_sink() if self.session.is_active else self.prev_default
        if target:
            self._send(target, volume, unmute)
        self._level_apps(volume)

    def _solo(self) -> AudioDevice | None:
        """The one output the session is feeding, when there is only one."""
        return self.session.devices[0] if len(self.session.devices) == 1 else None

    def _level_hub(self, sink: str, devices: list[AudioDevice]) -> None:
        """
        Set the hub's own level, and keep master honest for a lone output.

        With one output the master fader and that device's fader are two handles
        on the same thing, so the hub steps aside and the device carries the
        level. If both carried one they would multiply, and the fader would feel
        dead until it was most of the way up.
        """
        solo = devices[0] if len(devices) == 1 else None
        if solo:
            self.master_volume = solo.volume
        level = self._hub_level(devices)
        self._levels.pop(sink, None)  # a queued tick must not undo this direct set
        try:
            self.backend.set_volume(sink, level)
        except Exception as e:
            log.warning("Failed to set the level on %s: %s", sink, e)
        self._level_apps(level)

    def _level_apps(self, level: int) -> None:
        """App hubs sit where the session hub does, or master stops meaning anything for them."""
        for hub in self.hubs.values():
            self._send(hub.name, level)

    def _hub_level(self, devices: list[AudioDevice]) -> int:
        """100 when a lone output carries the level itself, else the master."""
        return 100 if len(devices) == 1 else self.master_volume

    def active_sink(self) -> str | None:
        """Whatever the session is currently playing through."""
        return self.session.sink.name if self.session.sink else None

    # ---------- PipeWire events ----------

    def _mark(self, kind: str) -> None:
        """Runs on the worker. Coalesces a burst of events into one _pw_changed."""
        if not self._pending:
            GLib.timeout_add(PW_SETTLE_MS, self._bg, self._pw_changed)
        self._pending.add(kind)

    def _pw_changed(self) -> None:
        kinds, self._pending = self._pending, set()
        if "sinks" in kinds:
            self._hotplug()
        if "streams" in kinds:
            # @locked, so this waits behind an in-flight route_stream instead
            # of racing it — and it also sweeps out any app hub whose stream ended.
            self.emit("streams-changed", self.streams())

    def _hotplug(self) -> None:
        """A wired output showed up or left; Bluetooth churn is BlueZ's job, not ours."""
        try:
            wired = {d.id for d in self.backend.list_outputs() if d.kind != DeviceKind.BLUETOOTH}
        except Exception as e:
            log.error("Failed to check for hotplug: %s", e)
            return
        known = {i for i, d in self.devices.items() if d.kind != DeviceKind.BLUETOOTH and d.connected}
        if wired != known:
            self.refresh()
            # An unplugged output gets no BlueZ event, so it takes the same exit.
            for dev_id in known - wired:
                self._on_disconnect(dev_id)
            if (wired - known) & self.targets:
                self._rebuild()

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
            # down, and the outputs that are staying never drop out.
            self._retarget(devices)
            return

        log.info("Starting session with %d device(s)...", len(devices))
        self._set_state(SessionState.STARTING)

        try:
            self.prev_default = self.backend.get_default()
            self._route(devices)
            self._adopt(devices)
            log.info("Session active: %s", self.active_sink())
        except Exception as e:
            log.error("Failed to start session: %s", e)
            self._set_state(SessionState.ERROR)
            self.stop_sharing()
            raise

    def _route(self, devices: list[AudioDevice]) -> None:
        """Stand up the hub and point everything that is playing at it."""
        self._prepare(devices)
        # On the session the moment it exists, so a failure further down still
        # gets it torn down by stop_sharing instead of leaking it.
        sink = self.session.sink = self.backend.create_sink(devices)

        # Set the volume before switching output, or the first moment of audio
        # lands at whatever level the new sink happened to be at.
        self._level_hub(sink.name, devices)

        self.backend.set_default(sink.name)
        self.backend.move_streams(sink.name, exclude=list(self.overrides))

    def _retarget(self, devices: list[AudioDevice]) -> None:
        """Change which outputs the live hub feeds. The hub itself stays put."""
        sink = self.session.sink
        # Outputs already fed are already unmuted and at their own level.
        self._prepare([d for d in devices if d.sink not in sink.legs])

        # Going from one output to two, the hub is still at 100 because the lone
        # device was carrying the level. Duck it before the new leg attaches, or
        # that output gets one blast at full volume before the level catches up.
        # Going back to one, it rises only once the other leg has dropped.
        duck = self._hub_level(devices) < self._hub_level(self.session.devices)
        if duck:
            self._level_hub(sink.name, devices)
        self.backend.set_legs(sink, devices)
        if not duck:
            self._level_hub(sink.name, devices)
        self._adopt(devices)

    def _prepare(self, devices: list[AudioDevice]) -> None:
        """Unmute each output and put it back at its own level."""
        for d in devices:
            if d.sink:
                self._levels.pop(d.sink, None)  # a queued tick must not undo this direct set
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

    @locked
    def streams(self) -> list[dict]:
        """What is playing, each with the devices it was pinned to (None: following)."""
        live = self.backend.list_streams()
        ids = {s["id"] for s in live}
        for sid in [s for s in self.hubs if s not in ids]:
            self.backend.destroy_sink(self.hubs.pop(sid))
        for sid in [s for s in self.overrides if s not in ids]:
            del self.overrides[sid]
        return [{**s, "devices": self.overrides.get(s["id"])} for s in live]

    @locked
    def route_stream(self, stream_id: int, ids: list[str] | None) -> None:
        """Pin a stream to these devices, or hand it back to the session with None."""
        devs = [d for d in (self.devices.get(i) for i in ids or []) if d and d.sink]
        hub = self.hubs.get(stream_id)

        if len(devs) > 1:
            if hub:
                self.backend.set_legs(hub, devs)
            else:
                hub = self.backend.create_sink(devs)
                try:
                    # A new null sink starts at 100%; level it before any audio lands.
                    self.backend.set_volume(hub.name, self._hub_level(self.session.devices))
                    self.backend.move_stream(stream_id, hub.name)
                except Exception:
                    self.backend.destroy_sink(hub)
                    raise
                self.hubs[stream_id] = hub
        else:
            target = devs[0].sink if devs else self.active_sink() or self.backend.get_default()
            # Moved before its old hub goes, or it falls to the default for a beat.
            self.backend.move_stream(stream_id, target)
            if hub:
                self.backend.destroy_sink(self.hubs.pop(stream_id))

        if devs:
            self.overrides[stream_id] = [d.id for d in devs]
        else:
            self.overrides.pop(stream_id, None)
        log.info("Manual route: stream %d → %s", stream_id, [d.name for d in devs] or "session")

    @locked
    def _sync_hubs(self) -> None:
        """Point each app hub at whichever of its picked devices are connected."""
        for sid, hub in self.hubs.items():
            picked = (self.devices.get(i) for i in self.overrides.get(sid, []))
            self.backend.set_legs(hub, [d for d in picked if d and d.connected and d.sink])

    def _drop_hubs(self) -> None:
        for sid in list(self.hubs):
            self.backend.destroy_sink(self.hubs.pop(sid))

    @locked
    def stop_sharing(self) -> None:
        self._set_state(SessionState.STOPPING)
        try:
            if self.prev_default:
                try:
                    self.backend.set_default(self.prev_default)
                    # Moved before the hubs go, or streams still in them drop
                    # out for a beat. Ones pinned to a single device stay put.
                    pinned = [s for s in self.overrides if s not in self.hubs]
                    self.backend.move_streams(self.prev_default, exclude=pinned)
                except Exception as e:
                    log.warning("Could not restore original default sink: %s", e)

            if self.session.sink:
                self.backend.destroy_sink(self.session.sink)
            self._drop_hubs()

            self.session.sink = None
            self.session.devices = []
            self.targets.clear()
            self.overrides.clear()
            self.emit("devices-changed", list(self.devices.values()))
            self._set_state(SessionState.IDLE)
        except Exception as e:
            log.error("Error while stopping session: %s", e)
            self._set_state(SessionState.ERROR)
            raise

    # ---------- Bluetooth events ----------

    def _on_connect(self, mac: str) -> None:
        log.info("Bluetooth connected: %s", mac)
        # _resolve_retry gives up on a device marked offline, and a reconnect is
        # exactly the case where the last disconnect left that flag set.
        dev = self.devices.get(mac)
        if dev:
            dev.connected = True
        # PipeWire creates the sink a moment after BlueZ reports the connection.
        self._resolve_retry(mac, tries=SINK_TRIES)

    def _resolve_retry(self, mac: str, tries: int) -> None:
        # The window is long enough that a disconnect can land mid-chain, and a
        # late success would mark a device that is already gone as connected.
        dev = self.devices.get(mac)
        if dev and not dev.connected:
            return

        sink = self.backend.resolve_bt_sink(mac)

        if not sink:
            if tries > 0:
                # The wait happens on the main loop; the retry itself runs on
                # the worker, so it never blocks other events.
                GLib.timeout_add(SINK_WAIT_MS, self._bg, self._resolve_retry, mac, tries - 1)
            else:
                log.warning(
                    "No sink for %s after %.1fs — it will appear on the next refresh.",
                    mac, SINK_TRIES * SINK_WAIT_MS / 1000,
                )
            return

        log.info("Resolved sink for %s: %s", mac, sink)
        if mac not in self.devices:
            self.refresh()
        else:
            self.devices[mac].connected = True
            self.devices[mac].sink = sink
            self.emit("devices-changed", list(self.devices.values()))
        self._sync_hubs()

        # Not gated on REPAIRING: a device that drops while the others carry on
        # leaves the session active, and it still has to be let back in.
        if mac in self.targets:
            self._rebuild()

    @locked
    def _on_disconnect(self, dev_id: str) -> None:
        """An output went away: a Bluetooth device dropping, or a wired one pulled out."""
        log.info("Disconnected: %s", dev_id)

        dev = self.devices.get(dev_id)
        if dev:
            dev.connected = False
            dev.sink = None
        self.emit("devices-changed", list(self.devices.values()))

        # PipeWire moves a stream off a sink that vanished, so one pinned to
        # just this device is back to following the session.
        for sid in [s for s, ids in self.overrides.items() if ids == [dev_id]]:
            del self.overrides[sid]
        self._sync_hubs()

        if not (self.session.is_active and dev_id in self.targets):
            return

        log.warning("An active sharing device (%s) disconnected.", dev_id)
        remaining = [
            d for d in (self.devices.get(t) for t in self.targets if t != dev_id)
            if d and d.connected and d.sink
        ]

        # Drop that one leg. The hub stays the default sink either way, so the
        # streams playing into it keep playing and nothing has to be moved.
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

    def _on_property(self, mac: str, key: str, value: object) -> None:
        dev = self.devices.get(mac)
        if dev and key == "Battery":
            dev.battery = int(value)
            self.emit("devices-changed", list(self.devices.values()))

    @locked
    def _rebuild(self) -> None:
        """Feed the hub back to each target that has come back, as it comes back."""
        ready = [
            d for d in (self.devices.get(t) for t in self.targets)
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
