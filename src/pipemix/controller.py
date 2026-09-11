"""
PipeMix — Controller.

The state machine: owns the SharingSession, reacts to Bluetooth events, runs
crash recovery, drives the backend, and pushes updates to the UI as GObject
signals. All business logic lives here; the UI only triggers and listens.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

from gi.repository import GLib, GObject

from pipemix.models import AudioDevice, DeviceKind, SessionState, SharingSession, VirtualSink
from pipemix.services.backend import BackendError, BackendHealth
from pipemix.services.bluetooth.device_monitor import DeviceMonitor
from pipemix.services.config.config_manager import ConfigManager

if TYPE_CHECKING:
    from pipemix.services.backend.pactl_backend import PactlBackend

log = logging.getLogger(__name__)


class Controller(GObject.Object):

    __gsignals__ = {
        "state-changed":   (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "devices-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "health-changed":  (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, backend: PactlBackend, config: ConfigManager | None = None) -> None:
        super().__init__()
        self.backend = backend
        self.config = config or ConfigManager()

        self.monitor = DeviceMonitor()
        self.monitor.on_connect = self._on_connect
        self.monitor.on_disconnect = self._on_disconnect
        self.monitor.on_property = self._on_property

        self.session = SharingSession()
        self.devices: dict[str, AudioDevice] = {}
        self.prev_default: str | None = None

        # MACs we want back if they reconnect mid-session.
        self.targets: set[str] = set()

        # Streams the user routed by hand, so a rebuild does not drag them back.
        self.overrides: dict[int, str] = {}

        self.master_volume = 50

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

        self.refresh()

    def stop(self) -> None:
        if self.session.is_active:
            try:
                self.stop_sharing()
            except Exception as e:
                log.error("Failed to stop sharing during shutdown: %s", e)
        self.monitor.stop()

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
        try:
            bt = self.monitor.connected()
            sinks = self.backend.resolve_bt_sinks([d["mac"] for d in bt])
            found: dict[str, AudioDevice] = {}

            for dev in self.backend.list_outputs():
                # Bluetooth is keyed by MAC and rebuilt from BlueZ below.
                if dev.kind == DeviceKind.BLUETOOTH:
                    continue
                dev.name = self.config.device_name(dev.id, dev.name)
                dev.volume = self._volume_of(dev.id, dev.sink)
                found[dev.id] = dev

            for d in bt:
                mac = d["mac"]
                sink = sinks.get(mac)
                found[mac] = AudioDevice(
                    id=mac,
                    name=self.config.device_name(mac, d["name"]),
                    sink=sink,
                    kind=DeviceKind.BLUETOOTH,
                    connected=True,
                    volume=self._volume_of(mac, sink),
                )

            # Keep saved-but-offline devices visible so presets still make sense.
            saved = set(self.config.data["devices"])
            for preset in self.config.data["presets"].values():
                saved.update(preset.get("devices", []))

            for dev_id in saved - set(found):
                found[dev_id] = AudioDevice(
                    id=dev_id,
                    name=self.config.device_name(dev_id, f"Offline Device ({dev_id})"),
                    sink=None,
                    kind=DeviceKind.BLUETOOTH if ":" in dev_id else DeviceKind.UNKNOWN,
                )

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
        if dev.connected and dev.sink:
            try:
                self.backend.set_mute(dev.sink, False)
                self.backend.set_volume(dev.sink, volume)
            except Exception as e:
                log.error("Failed to set volume for %s: %s", dev_id, e)

    def set_master_volume(self, volume: int) -> None:
        self.master_volume = volume
        target = self.active_sink() if self.session.is_active else self.prev_default
        if target:
            try:
                self.backend.set_volume(target, volume)
            except Exception as e:
                log.error("Failed to set master volume on %s: %s", target, e)

    def active_sink(self) -> str | None:
        """Whatever the session is currently playing through."""
        if self.session.sink:
            return self.session.sink.name
        return self.session.devices[0].sink if self.session.devices else None

    # ---------- Presets ----------

    @property
    def presets(self) -> dict:
        return self.config.data["presets"]

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

    def start_sharing(self, devices: list[AudioDevice]) -> None:
        if not devices:
            log.warning("start_sharing() called with no devices.")
            return

        if self.session.is_active:
            try:
                self.stop_sharing()
            except Exception as e:
                log.warning("Failed to stop previous session: %s", e)

        log.info("Starting session with %d device(s)...", len(devices))
        self._set_state(SessionState.STARTING)

        try:
            self.prev_default = self.backend.get_default()
            sink = self._route(devices)
            self.session.devices = devices
            self.session.sink = sink
            self.targets = {d.id for d in devices if d.kind == DeviceKind.BLUETOOTH}
            self._set_state(SessionState.ACTIVE)
            log.info("Session active: %s", sink.name if sink else devices[0].sink)
        except Exception as e:
            log.error("Failed to start session: %s", e)
            self._set_state(SessionState.ERROR)
            self.stop_sharing()
            raise

    def _route(self, devices: list[AudioDevice]) -> VirtualSink | None:
        """Combine into a virtual sink, or route straight to a lone device."""
        for d in devices:
            if d.sink:
                try:
                    self.backend.set_mute(d.sink, False)
                    self.backend.set_volume(d.sink, d.volume)
                except Exception as e:
                    log.warning("Failed to configure %s: %s", d.name, e)

        if len(devices) == 1:
            target, sink = devices[0].sink, None
            if not target:
                raise BackendError(f"{devices[0].name} has no resolved sink name.")
        else:
            sink = self.backend.create_sink(devices)
            target = sink.name

        # Set the volume before switching output, or the first moment of audio
        # lands at whatever level the new sink happened to be at.
        try:
            self.backend.set_volume(target, self.master_volume)
        except Exception as e:
            log.warning("Failed to pre-set volume on %s: %s", target, e)

        self.backend.set_default(target)
        self.backend.move_streams(target, exclude=list(self.overrides))
        return sink

    def route_stream(self, stream_id: int, target: str) -> None:
        self.backend.move_stream(stream_id, target)
        self.overrides[stream_id] = target
        log.info("Manual route: stream %d → %s", stream_id, target)

    def stop_sharing(self) -> None:
        self._set_state(SessionState.STOPPING)
        try:
            if self.prev_default:
                try:
                    self.backend.set_default(self.prev_default)
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

    def reset_audio(self) -> None:
        """Force-clean every PipeMix sink and start over."""
        log.info("Reset Audio requested.")
        self._set_state(SessionState.REPAIRING)
        try:
            if self.session.sink:
                try:
                    self.backend.destroy_sink(self.session.sink)
                except Exception:
                    pass

            self.clean_orphans()
            self.emit("health-changed", self.backend.health())
            self.refresh()

            self.session = SharingSession()
            self.targets.clear()
            self.overrides.clear()
            self._set_state(SessionState.IDLE)
        except Exception as e:
            log.error("Reset Audio failed: %s", e)
            self._set_state(SessionState.ERROR)

    # ---------- Bluetooth events ----------

    def _on_connect(self, mac: str) -> None:
        log.info("Bluetooth connected: %s", mac)
        # PipeWire creates the sink a moment after BlueZ reports the connection.
        self._resolve_retry(mac, tries=6)

    def _resolve_retry(self, mac: str, tries: int) -> bool:
        sink = self.backend.resolve_bt_sink(mac)

        if not sink:
            if tries > 0:
                GLib.timeout_add(250, self._resolve_retry, mac, tries - 1)
            else:
                log.warning("Could not resolve a sink for %s after retries.", mac)
            return GLib.SOURCE_REMOVE

        log.info("Resolved sink for %s: %s", mac, sink)
        if mac not in self.devices:
            self.refresh()
        else:
            self.devices[mac].connected = True
            self.devices[mac].sink = sink
            self.emit("devices-changed", list(self.devices.values()))

        if self.session.state == SessionState.REPAIRING and mac in self.targets:
            self._rebuild()
        return GLib.SOURCE_REMOVE

    def _on_disconnect(self, mac: str) -> None:
        log.info("Bluetooth disconnected: %s", mac)

        dev = self.devices.get(mac)
        lost_sink = dev.sink if dev else None
        if dev:
            dev.connected = False
            dev.sink = None
        self.emit("devices-changed", list(self.devices.values()))

        for sid in [s for s, sink in self.overrides.items() if sink == lost_sink]:
            del self.overrides[sid]

        if not (self.session.is_active and mac in self.targets):
            return

        log.warning("An active sharing device (%s) disconnected.", mac)
        remaining = [
            d for d in (self.devices.get(t) for t in self.targets if t != mac)
            if d and d.connected and d.sink
        ]
        remaining += [d for d in self.session.devices if d.kind != DeviceKind.BLUETOOTH]

        self._set_state(SessionState.REPAIRING)
        if self.session.sink:
            try:
                self.backend.destroy_sink(self.session.sink)
            except Exception:
                pass
            self.session.sink = None

        if not remaining:
            log.warning("No sharing devices left connected.")
            return

        log.info("Continuing on: %s", [d.name for d in remaining])
        try:
            self.session.sink = self._route(remaining)
            self._set_state(SessionState.ACTIVE)
        except Exception as e:
            log.error("Failed to move session to remaining devices: %s", e)
            self._set_state(SessionState.ERROR)

    def _on_property(self, mac: str, key: str, value: object) -> None:
        dev = self.devices.get(mac)
        if dev and key == "Battery":
            dev.battery = int(value)
            self.emit("devices-changed", list(self.devices.values()))

    def _rebuild(self) -> None:
        """Restore the session once every target is back with a live sink."""
        ready = []
        for mac in self.targets:
            dev = self.devices.get(mac)
            if not (dev and dev.connected and dev.sink):
                return
            ready.append(dev)
        ready += [d for d in self.session.devices if d.kind != DeviceKind.BLUETOOTH]

        log.info("All targets reconnected — rebuilding session...")
        try:
            self.session.sink = self._route(ready)
            self._set_state(SessionState.ACTIVE)
        except Exception as e:
            log.error("Failed to rebuild session: %s", e)
            self._set_state(SessionState.ERROR)

    def _set_state(self, state: SessionState) -> None:
        if self.session.state != state:
            log.debug("State: %s → %s", self.session.state.value, state.value)
            self.session.state = state
            self.emit("state-changed", state)
