from __future__ import annotations

import functools
import logging
import queue
import threading
from typing import TYPE_CHECKING

from gi.repository import GLib, GObject

from pipemix.models import AudioDevice, DeviceKind, SessionState, SharingSession, VirtualSink
from pipemix.models import BackendHealth
from pipemix.linux.bluetooth import DeviceMonitor
from pipemix.config import ConfigManager

if TYPE_CHECKING:
    from pipemix.linux.pactl_backend import PactlBackend

log = logging.getLogger(__name__)

# How long to wait for PipeWire to publish a bluez sink after BlueZ reports the
# connection. Slow headsets take seconds; retries are cheap and stop on success.
SINK_TRIES = 20
SINK_WAIT_MS = 250

# Let a burst of pactl events (session start, plug-in) settle before reacting once.
PW_SETTLE_MS = 100

# A device can report its real latency a moment after its leg loads (Bluetooth
# settling on a codec), so a joining leg gets one re-read this long after.
RECHECK_MS = 2000


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

    def __init__(self, backend: PactlBackend, config: ConfigManager) -> None:
        super().__init__()
        self.backend = backend
        self.config = config

        self.monitor = DeviceMonitor(
            lambda mac: self._bg(self._on_connect, mac),
            lambda mac: self._bg(self._on_disconnect, mac),
        )

        # BlueZ and pactl-subscribe jobs run in order on pipemix-events, off the GTK loop.
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

        # A stream routed to several outputs gets its own hub, fanned out like the session's.
        self.hubs: dict[int, VirtualSink] = {}

        self.master_volume = 50

        # Page calls (pywebview thread) and BlueZ/PipeWire events (worker) both change the legs.
        self._lock = threading.RLock()

        # Unsent levels per sink: (level, unmute). Drag ticks arrive on separate pywebview
        # threads and can reorder in pactl, so the worker sends only the latest.
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
            self.stop_sharing()
        # Moving a stream to the default sink clears its pin, so apps follow the default
        # again. Done before the app hubs go, or their streams drop out for a beat.
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
        for sink in self.backend.find_orphans():
            log.warning("Destroying orphaned sink: %s", sink.name)
            self.backend.destroy_sink(sink)

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

            # A dropped session output stays listed offline: shown reconnecting, level kept.
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
        if unmute:
            self.backend.set_mute(sink, False)
        self.backend.set_volume(sink, level)

    @locked  # so a routing change can't land between picking the sink and queueing
    def set_device_volume(self, dev_id: str, volume: int) -> None:
        dev = self.devices.get(dev_id)
        if not dev:
            return
        dev.volume = volume
        if self._solo() is dev:
            self.master_volume = volume
        if dev.connected and dev.sink:
            self._send(dev.sink, volume, True)

    @locked
    def set_master_volume(self, volume: int) -> None:
        self.master_volume = volume

        solo = self._solo()
        if solo:
            # The hub is transparent for a lone output, so the level belongs on
            # the device — and its row has to move with the master row.
            self.set_device_volume(solo.id, volume)
            self.emit("devices-changed", list(self.devices.values()))
            return

        target = self.active_sink() if self.session.is_active else self.prev_default
        if target:
            self._send(target, volume, True)
        self._level_apps(volume)

    def _solo(self) -> AudioDevice | None:
        """The one output the session is feeding, when there is only one."""
        return self.session.devices[0] if len(self.session.devices) == 1 else None

    def _level_hub(self, sink: str, devices: list[AudioDevice]) -> None:
        """
        Set the hub's level; with a lone output the device carries it instead.

        Master and that device's fader are then one control. If both carried a
        level they would multiply, and the fader would feel dead until near the top.
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

    @locked
    def _hotplug(self) -> None:
        """A wired output showed up or left, or a session Bluetooth sink was recreated."""
        try:
            wired = {d.id for d in self.backend.list_outputs() if d.kind != DeviceKind.BLUETOOTH}
        except Exception as e:  # don't let a pactl hiccup swallow this batch's streams push
            log.error("Failed to check for hotplug: %s", e)
            return
        known = {i for i, d in self.devices.items() if d.kind != DeviceKind.BLUETOOTH and d.connected}
        back = False
        if wired != known:
            self.refresh()
            # An unplugged output gets no BlueZ event, so it takes the same exit.
            for dev_id in known - wired:
                self._on_disconnect(dev_id)
            back = bool((wired - known) & self.targets)
        # A codec switch or WirePlumber restart recreates a Bluetooth sink with
        # no BlueZ event, and the old leg unloads itself along with the old sink.
        bt = []
        if self.session.sink or self.hubs:
            used = self.targets | {i for ids in self.overrides.values() for i in ids}
            bt = [d for d in self.devices.values() if d.kind == DeviceKind.BLUETOOTH and d.connected and d.id in used]
        for d in bt:
            d.sink = self.backend.resolve_bt_sink(d.id)
        if back or bt:
            self._rebuild()
            self._sync_hubs()

    # ---------- Sharing ----------

    @locked
    def start_sharing(self, devices: list[AudioDevice]) -> None:
        if not devices:
            log.warning("start_sharing() called with no devices.")
            return

        if self.session.sink:
            # The hub is already up, so only its legs move; staying outputs never drop out.
            self._retarget(devices)
            return

        log.info("Starting session with %d device(s)...", len(devices))
        self._set_state(SessionState.STARTING)

        try:
            self.prev_default = self.backend.get_default()
            self._route(devices)
            self._adopt(devices)
            log.info("Session active: %s", self.active_sink())
        except Exception:
            self._set_state(SessionState.ERROR)
            self.stop_sharing()
            raise

    def _route(self, devices: list[AudioDevice]) -> None:
        """Stand up the hub and point everything that is playing at it."""
        self._prepare(devices)
        # On the session the moment it exists, so a failure further down still
        # gets it torn down by stop_sharing instead of leaking it.
        sink = self.session.sink = self.backend.create_sink(devices)

        # Set the volume before switching output, or the first audio lands at the old level.
        self._level_hub(sink.name, devices)

        self.backend.set_default(sink.name)
        self.backend.move_streams(sink.name, exclude=list(self.overrides))
        GLib.timeout_add(RECHECK_MS, self._bg, self._rebuild)

    def _retarget(self, devices: list[AudioDevice], keep: set[str] = frozenset()) -> None:
        """Change which outputs the live hub feeds. The hub itself stays put."""
        sink = self.session.sink
        # Outputs already fed are already unmuted and at their own level.
        self._prepare([d for d in devices if d.sink not in sink.legs])

        # One output to two: the hub is still at 100 (the lone device carried the level),
        # so duck it before the new leg attaches or that output blasts at full volume.
        # Back to one: it rises only after the other leg drops.
        duck = self._hub_level(devices) < self._hub_level(self.session.devices)
        if duck:
            self._level_hub(sink.name, devices)
        # Only on a leg that actually loaded (a failing load would re-arm forever),
        # including one reloaded because its sink was recreated under the same name.
        if self.backend.set_legs(sink, devices):
            GLib.timeout_add(RECHECK_MS, self._bg, self._rebuild)
        if not duck:
            self._level_hub(sink.name, devices)
        self._adopt(devices, keep)

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

    def _adopt(self, devices: list[AudioDevice], keep: set[str] = frozenset()) -> None:
        """Record who the session is for, now that the routing matches."""
        self.session.devices = devices
        self.targets = {d.id for d in devices} | keep
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

        # Drop just that leg; the hub stays the default sink, so no stream has to move.
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
        # Still-offline targets stay targets, or they never get their leg back.
        self._retarget(ready, self.targets)

    def _set_state(self, state: SessionState) -> None:
        if self.session.state != state:
            log.debug("State: %s → %s", self.session.state.value, state.value)
            self.session.state = state
            self.emit("state-changed", state)
