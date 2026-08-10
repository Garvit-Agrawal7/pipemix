"""
PipeMix — Controller

The central state machine and coordinator. Controls the SharingSession,
handles Bluetooth connection events from DeviceMonitor, runs crash recovery,
orchestrates PactlBackend, and updates the UI via GObject signals.

Design Rules:
  - All business logic lives here. The UI only triggers actions and listens to signals.
  - Keeps track of physical and virtual sinks.
  - Automatically handles Bluetooth reconnects by recreating the virtual sink.
  - Uses GLib signals to push updates to the UI, ensuring thread-safe UI updates.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from gi.repository import GLib, GObject

from pipemix.models import (
    AudioDevice,
    DeviceCapabilities,
    DeviceKind,
    SessionState,
    SharingSession,
    VirtualSink,
)
from pipemix.services.backend import BackendHealth, BackendStatus
from pipemix.services.bluetooth.bluez_resolver import BluezResolver
from pipemix.services.bluetooth.device_monitor import DeviceMonitor
from pipemix.services.config.config_manager import ConfigManager

if TYPE_CHECKING:
    from pipemix.services.backend import AudioBackend

log = logging.getLogger(__name__)

class Controller(GObject.Object):
    """
    Main controller for PipeMix. Inherits from GObject.Object
    to provide event-driven signal propagation to the GTK UI.
    """

    __gsignals__ = {
        # Emitted when session state transitions (e.g. IDLE -> ACTIVE)
        "session-state-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # Emitted when the device list changes (e.g. BT device connects/disconnects)
        "device-list-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # Emitted when backend health status changes
        "backend-health-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, backend: AudioBackend, config_manager: ConfigManager | None = None) -> None:
        super().__init__()
        self.backend = backend
        self.config_manager = config_manager or ConfigManager()

        self.resolver = BluezResolver()
        self.monitor = DeviceMonitor()

        # Connect DeviceMonitor callbacks
        self.monitor.on_connected = self._on_device_connected
        self.monitor.on_disconnected = self._on_device_disconnected
        self.monitor.on_property_changed = self._on_device_property_changed

        # Central runtime states
        self.session = SharingSession()
        self.known_devices: dict[str, AudioDevice] = {}  # mac/device_id -> AudioDevice
        self.original_default_sink: str | None = None

        # Track the desired target MAC addresses if we are reconnecting
        self.reconnect_targets: set[str] = set()

        # Track manual split stream routes to exclude them from combined rebuild resets
        self.stream_routing_overrides: dict[int, str] = {}

        # Track the desired master volume to persist across session rebuilds
        self.master_volume: int = 50

    # ------------------------------------------------------------------
    # Initialisation & Startup
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start services and perform initial setup & crash recovery."""
        log.info("Initializing PipeMix Controller...")

        # 1. Health check
        status = self.backend.health_check()
        self.emit("backend-health-changed", status)
        if status.health != BackendHealth.OK:
            log.warning("Backend health check failed on startup: %s", status.message)

        # 2. Run crash recovery (cleanup orphans)
        self.startup_recovery()

        # 3. Start BlueZ DeviceMonitor
        try:
            self.monitor.start()
        except Exception as e:
            log.error("Failed to start Bluetooth DeviceMonitor: %s", e)

        # 4. Discover initial devices
        self.refresh_devices()

    def stop(self) -> None:
        """Stop all background monitors and clean up active session."""
        log.info("Stopping PipeMix Controller...")
        if self.session.is_active:
            try:
                self.stop_sharing()
            except Exception as e:
                log.error("Failed to stop sharing during shutdown: %s", e)
        self.monitor.stop()

    def startup_recovery(self) -> None:
        """Detect and clean up orphaned virtual sinks from a previous crash."""
        log.info("Running crash recovery check...")
        try:
            orphans = self.backend.find_orphaned_virtual_sinks()
            if orphans:
                log.info("Found %d orphaned sink(s) to clean up.", len(orphans))
                for sink in orphans:
                    log.warning("Destroying orphaned virtual sink: %s", sink.sink_name)
                    self.backend.destroy_virtual_output(sink)
            else:
                log.debug("No orphaned sinks found.")
        except Exception as e:
            log.error("Error during crash recovery: %s", e)

    # ------------------------------------------------------------------
    # Device Management
    # ------------------------------------------------------------------

    def refresh_devices(self) -> None:
        """
        Re-enumerate all output devices (Bluetooth, built-in, USB, HDMI).
        Merges BlueZ state and PipeWire state, then updates self.known_devices.
        """
        log.debug("Refreshing device list...")
        try:
            # 1. Get physical outputs from PipeWire
            pw_outputs = self.backend.list_outputs()
            pw_by_id = {d.device_id: d for d in pw_outputs}

            # 2. Get connected Bluetooth devices from BlueZ
            bt_devices = self.monitor.get_connected_devices()
            bt_macs = [d["mac"] for d in bt_devices]

            # 3. Resolve BT MACs to current PipeWire sinks
            resolved_sinks = self.resolver.resolve_all(bt_macs)

            new_devices: dict[str, AudioDevice] = {}

            # Process PipeWire devices
            for dev_id, pw_dev in pw_by_id.items():
                if pw_dev.kind == DeviceKind.BLUETOOTH:
                    # Bluetooth devices are managed by BlueZ resolver and MAC ids.
                    # We will construct them from the BlueZ info block next.
                    continue
                
                # Check if we have a saved friendly name in config
                friendly_name = self.config_manager.get_device_name(dev_id, pw_dev.display_name)
                pw_dev.display_name = friendly_name

                # Fetch current volume (preserve if already known)
                if dev_id in self.known_devices:
                    pw_dev.volume = self.known_devices[dev_id].volume
                elif pw_dev.sink_name:
                    try:
                        pw_dev.volume = self.backend.get_sink_volume(pw_dev.sink_name)
                    except Exception:
                        pw_dev.volume = 50
                else:
                    pw_dev.volume = 50
                
                new_devices[dev_id] = pw_dev

            # Process Bluetooth devices
            for bt in bt_devices:
                mac = bt["mac"]
                sink_name = resolved_sinks.get(mac)
                
                # Get friendly name from config or default to BlueZ name
                friendly_name = self.config_manager.get_device_name(mac, bt["name"])
                
                # Fetch current volume (preserve if already known)
                if mac in self.known_devices:
                    vol = self.known_devices[mac].volume
                else:
                    vol = 50
                    if sink_name:
                        try:
                            vol = self.backend.get_sink_volume(sink_name)
                        except Exception:
                            vol = 50
                
                device = AudioDevice(
                    device_id=mac,
                    display_name=friendly_name,
                    sink_name=sink_name,
                    kind=DeviceKind.BLUETOOTH,
                    capabilities=DeviceCapabilities.for_bluetooth(),
                    is_connected=True,
                    volume=vol
                )
                new_devices[mac] = device

            # Preserve disconnected/offline devices that exist in the config
            # so the UI can show them (e.g. for presets)
            presets = self.config_manager._config_data.get("presets", {})
            preset_macs = set()
            for preset in presets.values():
                preset_macs.update(preset.get("devices", []))

            saved_devices = self.config_manager._config_data.get("devices", {})
            for saved_id in set(saved_devices.keys()) | preset_macs:
                if saved_id not in new_devices:
                    # Device is offline but saved in config
                    friendly_name = self.config_manager.get_device_name(saved_id, f"Offline Device ({saved_id})")
                    kind = DeviceKind.BLUETOOTH if ":" in saved_id else DeviceKind.UNKNOWN
                    
                    device = AudioDevice(
                        device_id=saved_id,
                        display_name=friendly_name,
                        sink_name=None,
                        kind=kind,
                        capabilities=DeviceCapabilities.for_bluetooth() if kind == DeviceKind.BLUETOOTH else DeviceCapabilities.for_builtin(),
                        is_connected=False
                    )
                    new_devices[saved_id] = device

            self.known_devices = new_devices
            log.debug("Device list updated: %d devices known.", len(self.known_devices))
            self.emit("device-list-updated", list(self.known_devices.values()))

        except Exception as e:
            log.error("Failed to refresh devices: %s", e)

    def set_device_volume(self, device_id: str, volume: int) -> None:
        """Set the volume of a device and update its runtime state."""
        dev = self.known_devices.get(device_id)
        if dev:
            dev.volume = volume
            if dev.is_connected and dev.sink_name:
                try:
                    self.backend.set_sink_mute(dev.sink_name, False)
                    self.backend.set_sink_volume(dev.sink_name, volume)
                except Exception as e:
                    log.error("Failed to set volume/unmute for device %s: %s", device_id, e)

    def set_master_volume(self, volume: int) -> None:
        """Set the master volume and cache it for future session rebuilds."""
        self.master_volume = volume
        target_sink = None
        if self.session.is_active:
            if self.session.virtual_sink:
                target_sink = self.session.virtual_sink.sink_name
            elif self.session.selected_devices:
                target_sink = self.session.selected_devices[0].sink_name
        else:
            target_sink = self.original_default_sink
            
        if target_sink:
            try:
                self.backend.set_sink_volume(target_sink, volume)
            except Exception as e:
                log.error("Failed to set master volume on %s: %s", target_sink, e)

    def save_preset(self, name: str, devices: list[str]) -> str:
        """Sanitize name, save preset to config, write to disk, and return preset_id."""
        import re
        preset_id = re.sub(r"[^a-z0-9_]", "", name.lower().replace(" ", "_"))
        if not preset_id:
            preset_id = "preset_" + str(int(time.time()))
        
        self.config_manager.save_preset(preset_id, name, devices)
        self.config_manager.save()
        log.info("Saved preset '%s' (%s) with devices: %s", name, preset_id, devices)
        return preset_id

    def delete_preset(self, preset_id: str) -> None:
        """Remove preset from config and write to disk."""
        self.config_manager.delete_preset(preset_id)
        self.config_manager.save()
        log.info("Deleted preset '%s'", preset_id)

    def get_all_presets(self) -> dict:
        """Get dict of all configured presets."""
        return self.config_manager._config_data.get("presets", {})

    # ------------------------------------------------------------------
    # Sharing Session Lifecycle
    # ------------------------------------------------------------------

    def start_sharing(self, devices: list[AudioDevice]) -> None:
        """Create combined sink (or route directly if 1 device), route streams, and set default sink."""
        if not devices:
            log.warning("start_sharing() called with no devices.")
            return

        # If already sharing, stop the old one first
        if self.session.is_active:
            log.info("Already sharing. Stopping old session to rebuild...")
            try:
                self.stop_sharing()
            except Exception as e:
                log.warning("Failed to stop previous session: %s", e)

        log.info("Starting sharing session with %d device(s)...", len(devices))
        self._set_state(SessionState.STARTING)

        try:
            # Save current default sink so we can restore it later
            self._save_current_default_sink()

            # Apply routing (virtual combined sink if >1 device, direct if 1 device)
            virtual_sink = self._apply_sharing_routing(devices)

            # Update session state
            self.session.selected_devices = devices
            self.session.virtual_sink = virtual_sink
            self.reconnect_targets = {d.device_id for d in devices if d.kind == DeviceKind.BLUETOOTH}
            
            self._set_state(SessionState.ACTIVE)

            if virtual_sink:
                log.info("Sharing session active: %s", virtual_sink.sink_name)
            else:
                log.info("Sharing session active (Direct Routing): %s", devices[0].sink_name)

        except Exception as e:
            log.error("Failed to start sharing session: %s", e)
            self._set_state(SessionState.ERROR)
            self.stop_sharing()  # Clean up any partial state
            raise

    def _apply_sharing_routing(self, devices: list[AudioDevice]) -> VirtualSink | None:
        """Helper to create virtual sink (if >1 device) or route directly (if 1 device)."""
        exclude_ids = list(self.stream_routing_overrides.keys())
        
        # Ensure all target physical devices are unmuted and set to their individual volumes
        for d in devices:
            if d.sink_name:
                try:
                    self.backend.set_sink_mute(d.sink_name, False)
                    self.backend.set_sink_volume(d.sink_name, d.volume)
                except Exception as e:
                    log.warning("Failed to configure target device %s: %s", d.display_name, e)
        
        if len(devices) == 1:
            # Direct routing optimization for single output
            target_sink = devices[0].sink_name
            if not target_sink:
                from pipemix.services.backend import BackendError
                raise BackendError(f"Selected device {devices[0].display_name} has no resolved sink name.")
            
            # Configure direct target volume first to avoid volume spikes
            try:
                self.backend.set_sink_volume(target_sink, self.master_volume)
            except Exception as e:
                log.warning("Failed to pre-set direct target volume: %s", e)
                
            self.backend.set_default_sink(target_sink)
            self.backend.move_streams(target_sink, exclude_stream_ids=exclude_ids)
            return None
        else:
            # Combine multiple outputs
            virtual_sink = self.backend.create_virtual_output(devices)
            
            # Configure virtual combined volume first to avoid volume spikes
            try:
                self.backend.set_sink_volume(virtual_sink.sink_name, self.master_volume)
            except Exception as e:
                log.warning("Failed to pre-set virtual combined volume: %s", e)
                
            self.backend.set_default_sink(virtual_sink.sink_name)
            self.backend.move_streams(virtual_sink.sink_name, exclude_stream_ids=exclude_ids)
            return virtual_sink

    def set_stream_routing(self, stream_id: int, target_sink: str) -> None:
        """Route a specific stream to a target sink and track the override."""
        self.backend.move_stream(stream_id, target_sink)
        self.stream_routing_overrides[stream_id] = target_sink
        log.info("Registered manual split route: stream %d -> %s", stream_id, target_sink)

    def stop_sharing(self) -> None:
        """Destroy combined sink and restore default output."""
        log.info("Stopping sharing session...")
        self._set_state(SessionState.STOPPING)

        try:
            # Restore default sink first
            if self.original_default_sink:
                try:
                    self.backend.set_default_sink(self.original_default_sink)
                except Exception as e:
                    log.warning("Could not restore original default sink: %s", e)

            # Destroy virtual sink
            if self.session.virtual_sink:
                self.backend.destroy_virtual_output(self.session.virtual_sink)

            self.session.virtual_sink = None
            self.session.selected_devices = []
            self.reconnect_targets.clear()
            self.stream_routing_overrides.clear()

            self._set_state(SessionState.IDLE)
            log.info("Sharing session stopped successfully.")

        except Exception as e:
            log.error("Error occurred while stopping session: %s", e)
            self._set_state(SessionState.ERROR)
            raise

    def reset_audio(self) -> None:
        """Force clean all PipeMix virtual sinks and reset state."""
        log.info("Reset Audio requested.")
        self._set_state(SessionState.REPAIRING)

        try:
            # 1. Stop sharing and teardown active sessions
            if self.session.virtual_sink:
                try:
                    self.backend.destroy_virtual_output(self.session.virtual_sink)
                except Exception:
                    pass

            # 2. Re-run crash recovery (destroys all orphans)
            self.startup_recovery()

            # 3. Refresh backend health check
            status = self.backend.health_check()
            self.emit("backend-health-changed", status)

            # 4. Refresh devices
            self.refresh_devices()

            # 5. Restore session state
            self.session = SharingSession()
            self.reconnect_targets.clear()
            self.stream_routing_overrides.clear()
            self._set_state(SessionState.IDLE)
            log.info("Reset Audio complete.")

        except Exception as e:
            log.error("Reset Audio failed: %s", e)
            self._set_state(SessionState.ERROR)

    # ------------------------------------------------------------------
    # Bluetooth Reconnect & Monitor Callbacks
    # ------------------------------------------------------------------

    def _on_device_connected(self, mac: str) -> None:
        """Called when a Bluetooth device connects."""
        log.info("Bluetooth device connected: %s", mac)
        
        # We need to resolve the new PipeWire sink.
        # Sometimes there is a small delay between BlueZ connection and PipeWire sink creation.
        # We use a retry loop within GLib to check for the sink.
        self._retry_resolve_sink(mac, attempts_left=6)

    def _on_device_disconnected(self, mac: str) -> None:
        """Called when a Bluetooth device disconnects."""
        log.info("Bluetooth device disconnected: %s", mac)
        
        # Get sink name before clearing
        disconnected_sink = None
        if mac in self.known_devices:
            disconnected_sink = self.known_devices[mac].sink_name

        # 1. Update internal state
        if mac in self.known_devices:
            self.known_devices[mac].is_connected = False
            self.known_devices[mac].sink_name = None
        self.emit("device-list-updated", list(self.known_devices.values()))

        # Clean up overrides for the disconnected device
        if disconnected_sink:
            to_remove = [sid for sid, sink in self.stream_routing_overrides.items() if sink == disconnected_sink]
            for sid in to_remove:
                del self.stream_routing_overrides[sid]
                log.debug("Cleared split routing override for stream %d because target device disconnected", sid)

        # 2. Check if we need to trigger reconnect / restore logic
        if self.session.is_active and mac in self.reconnect_targets:
            log.warning("An active sharing device (%s) disconnected.", mac)
            
            # Find target devices that are still connected
            remaining_connected = []
            for target_mac in self.reconnect_targets:
                dev = self.known_devices.get(target_mac)
                if dev and dev.is_connected and dev.sink_name and target_mac != mac:
                    remaining_connected.append(dev)
            
            # Include non-bluetooth devices that were in the session
            for dev in self.session.selected_devices:
                if dev.kind != DeviceKind.BLUETOOTH:
                    remaining_connected.append(dev)

            if remaining_connected:
                log.info("Continuing session on remaining connected device(s): %s", 
                         [d.display_name for d in remaining_connected])
                self._set_state(SessionState.REPAIRING)
                
                try:
                    # Clean up old virtual sink first
                    if self.session.virtual_sink:
                        self.backend.destroy_virtual_output(self.session.virtual_sink)
                        self.session.virtual_sink = None
                    
                    # Apply routing (virtual combined sink if >1 device, direct if 1 device)
                    virtual_sink = self._apply_sharing_routing(remaining_connected)
                    self.session.virtual_sink = virtual_sink
                    self._set_state(SessionState.ACTIVE)
                except Exception as e:
                    log.error("Failed to transition session to remaining devices: %s", e)
                    self._set_state(SessionState.ERROR)
            else:
                log.warning("No target sharing devices remaining connected. Stopping session.")
                self._set_state(SessionState.REPAIRING)
                if self.session.virtual_sink:
                    try:
                        self.backend.destroy_virtual_output(self.session.virtual_sink)
                    except Exception:
                        pass
                    self.session.virtual_sink = None

    def _on_device_property_changed(self, mac: str, key: str, value: object) -> None:
        """Handle battery or other Bluetooth properties."""
        if mac in self.known_devices:
            dev = self.known_devices[mac]
            if key == "Battery":
                dev.battery_level = int(value)
                log.debug("Device %s battery updated: %d%%", mac, dev.battery_level)
                self.emit("device-list-updated", list(self.known_devices.values()))

    def _retry_resolve_sink(self, mac: str, attempts_left: int) -> bool:
        """GLib timeout callback to retry resolving the PipeWire sink."""
        sink = self.resolver.resolve(mac)
        
        if sink:
            log.info("Resolved sink name for %s: %s", mac, sink)
            
            # Update device state
            if mac not in self.known_devices:
                # Discovered device for the first time
                self.refresh_devices()
            else:
                self.known_devices[mac].is_connected = True
                self.known_devices[mac].sink_name = sink
                self.emit("device-list-updated", list(self.known_devices.values()))

            # If we are waiting for this device to reconnect to rebuild the session
            if self.session.state == SessionState.REPAIRING and mac in self.reconnect_targets:
                self._check_and_rebuild_session()
            
            return GLib.SOURCE_REMOVE  # Stop retrying

        if attempts_left <= 0:
            log.warning("Failed to resolve PipeWire sink for connected device %s after retries.", mac)
            return GLib.SOURCE_REMOVE

        # Try again in 250ms
        GLib.timeout_add(250, self._retry_resolve_sink, mac, attempts_left - 1)
        return GLib.SOURCE_REMOVE

    def _check_and_rebuild_session(self) -> None:
        """Rebuild the active sharing session if all targets are back online."""
        # Ensure all Bluetooth targets have resolved sinks
        ready_devices = []
        for mac in self.reconnect_targets:
            dev = self.known_devices.get(mac)
            if not (dev and dev.is_connected and dev.sink_name):
                log.debug("Cannot rebuild session yet: device %s is not fully connected.", mac)
                return
            ready_devices.append(dev)

        # Also add non-bluetooth targets that were in the original session
        for dev in self.session.selected_devices:
            if dev.kind != DeviceKind.BLUETOOTH:
                ready_devices.append(dev)

        log.info("All target devices are reconnected! Rebuilding sharing session...")
        try:
            # Apply routing (virtual combined sink if >1 device, direct if 1 device)
            virtual_sink = self._apply_sharing_routing(ready_devices)
            self.session.virtual_sink = virtual_sink
            self._set_state(SessionState.ACTIVE)
            log.info("Reconnected sharing session successfully.")
        except Exception as e:
            log.error("Failed to rebuild session on auto-reconnect: %s", e)
            self._set_state(SessionState.ERROR)

    # ------------------------------------------------------------------
    # Helper Utilities (Private)
    # ------------------------------------------------------------------

    def _set_state(self, state: SessionState) -> None:
        if self.session.state != state:
            log.debug("Session state change: %s -> %s", self.session.state.value, state.value)
            self.session.state = state
            self.emit("session-state-changed", state)

    def _save_current_default_sink(self) -> None:
        """Query pactl info to find and save the current default sink."""
        try:
            import subprocess
            result = subprocess.run(
                ["pactl", "info"],
                capture_output=True, text=True, timeout=3
            )
            for line in result.stdout.splitlines():
                if "Default Sink:" in line:
                    self.original_default_sink = line.split(":", 1)[1].strip()
                    log.debug("Saved original default sink: %s", self.original_default_sink)
                    return
        except Exception as e:
            log.warning("Could not query default sink: %s", e)
            self.original_default_sink = None
