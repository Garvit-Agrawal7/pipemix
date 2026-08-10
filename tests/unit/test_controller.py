"""
PipeMix — Controller Unit Test

Tests the controller state machine, device discovery, sharing logic,
and reset_audio without a GUI.

Run this from the project root:
    python3 tests/unit/test_controller.py

What it does:
  1. Creates a ConfigManager & PactlBackend.
  2. Instantiates the Controller.
  3. Tests Controller initialization and device discovery.
  4. Tests start_sharing() on all discovered devices.
  5. Verifies the SharingSession is ACTIVE and the virtual sink exists.
  6. Tests stop_sharing() and verifies state is IDLE.
  7. Tests reset_audio() to ensure cleanup.

Safe to run with or without Bluetooth devices connected.
"""

from __future__ import annotations

import logging
import os
import sys
import time

# Add src/ to path so we can import pipemix
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from gi.repository import GLib

from pipemix.controller import Controller
from pipemix.models import SessionState
from pipemix.services.backend.pactl_backend import PactlBackend
from pipemix.services.config.config_manager import ConfigManager

logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)-8s %(name)-30s %(message)s",
)
log = logging.getLogger("test_controller")


def section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def ok(msg: str) -> None:
    print(f"  ✓  {msg}")


def fail(msg: str) -> None:
    print(f"  ✗  {msg}")
    sys.exit(1)


# Global tracking variables for signal callbacks
state_changed_events = []
device_list_events = []


def on_state_changed(controller: Controller, state: SessionState) -> None:
    print(f"  [SIGNAL] session-state-changed: {state.value}")
    state_changed_events.append(state)


def on_device_list_updated(controller: Controller, devices: list) -> None:
    print(f"  [SIGNAL] device-list-updated: {len(devices)} device(s) in list")
    device_list_events.append(devices)


def main() -> None:
    print("\nPipeMix — Controller Test")
    print("=" * 60)

    # 1. Instantiate backend and config (using a temporary config path to avoid touching user settings)
    tmp_config_path = ConfigManager().config_dir / "config_test_tmp.toml"
    if tmp_config_path.exists():
        tmp_config_path.unlink()
    
    config = ConfigManager(config_path=tmp_config_path)
    backend = PactlBackend()
    controller = Controller(backend, config)

    # Connect signals
    controller.connect("session-state-changed", on_state_changed)
    controller.connect("device-list-updated", on_device_list_updated)

    # Step 1: Controller Start
    section("Step 1 — Start Controller & Discover Devices")
    controller.start()

    time.sleep(0.5)  # Let GLib loop process any pending events
    if len(device_list_events) == 0:
        fail("device-list-updated signal was never emitted on start.")

    devices = controller.known_devices.values()
    connected_devices = [d for d in devices if d.is_connected]
    if not connected_devices:
        fail("No connected audio devices discovered. Check if built-in audio or headphones are connected.")

    print("\n  Discovered connected devices:")
    for d in connected_devices:
        print(f"    - {d.display_name} ({d.kind.value}) id={d.device_id}")
    ok(f"Discovered {len(connected_devices)} connected output(s).")

    # Step 2: Test Start Sharing
    section("Step 2 — Test Start Sharing")
    # Share audio on the discovered connected devices
    devices_to_share = list(connected_devices)
    print(f"  Starting sharing session with devices: {[d.display_name for d in devices_to_share]}")
    
    controller.start_sharing(devices_to_share)
    
    if controller.session.state != SessionState.ACTIVE:
        fail(f"Session state should be ACTIVE, but is {controller.session.state.value}")
    
    if len(devices_to_share) > 1:
        if controller.session.virtual_sink is None:
            fail("Sharing active on multiple devices but no virtual sink is registered in controller.")
        ok(f"Sharing session is active. Virtual sink: {controller.session.virtual_sink.sink_name}")

        # Verify sink is default
        import subprocess
        result = subprocess.run(["pactl", "info"], capture_output=True, text=True)
        if controller.session.virtual_sink.sink_name in result.stdout:
            ok("Virtual sink successfully set as system default.")
        else:
            fail("Virtual sink is NOT set as system default.")
    else:
        if controller.session.virtual_sink is not None:
            fail("Sharing active on single device but a virtual sink was created (should be direct routing).")
        ok("Sharing session is active (Direct Routing to single hardware device).")

    # Step 3: Test Stop Sharing
    section("Step 3 — Test Stop Sharing")
    controller.stop_sharing()
    
    if controller.session.state != SessionState.IDLE:
        fail(f"Session state should be IDLE, but is {controller.session.state.value}")
        
    if controller.session.virtual_sink is not None:
        fail("Sharing stopped but virtual sink is still registered in controller.")
        
    ok("Sharing session successfully stopped.")

    # Step 4: Test Reset Audio
    section("Step 4 — Test Reset Audio")
    controller.reset_audio()
    
    if controller.session.state != SessionState.IDLE:
        fail(f"Session state should be IDLE after reset, but is {controller.session.state.value}")
        
    ok("Reset Audio complete, controller is clean.")

    # Cleanup temp test config file
    if tmp_config_path.exists():
        tmp_config_path.unlink()

    print(f"\n{'=' * 60}")
    print("  ALL CONTROLLER TESTS PASSED")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
