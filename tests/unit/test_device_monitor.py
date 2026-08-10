"""
PipeMix — DeviceMonitor + BluezResolver Interactive Test

This test is interactive. It watches for real Bluetooth events.

Run from project root:
    python3 tests/unit/test_device_monitor.py

Step-by-step:
  1. Script starts → shows all currently connected BT audio devices + their sinks.
  2. Disconnect one earbud (turn it off or use Bluetooth settings to disconnect).
     → You should see: ✗ [DISCONNECTED] XX:XX:XX:XX:XX:XX
  3. Reconnect it.
     → You should see: ✓ [CONNECTED]    XX:XX:XX:XX:XX:XX  sink=bluez_output...
  4. Repeat with the other earbud.
  5. Press Ctrl+C to exit cleanly.

Expected with both earbuds connected at start:
  Step 1 — Currently connected BT audio devices:
    Boat Airdopes        mac=61:C5:02:3A:59:49  sink=bluez_output.61_C5_02_3A_59_49.1
    OnePlus Buds         mac=E3:ED:0F:56:16:7D  sink=bluez_output.E3_ED_0F_56_16_7D.1

IMPORTANT: Both earbuds must be connected BEFORE running this test for Step 1 to show them.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

# Add src/ to path — no install required
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

# Configure logging before any imports that use it
logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)-8s %(name)-40s %(message)s",
)
log = logging.getLogger("test_device_monitor")

from gi.repository import GLib

from pipemix.services.bluetooth.bluez_resolver import BluezResolver
from pipemix.services.bluetooth.device_monitor import DeviceMonitor


# ---------------------------------------------------------------------------
# Event callbacks
# ---------------------------------------------------------------------------

_resolver = BluezResolver()


def on_connected(mac: str) -> None:
    # Give PipeWire ~500ms to register the new sink before resolving
    # (BlueZ event fires slightly before PipeWire creates the sink)
    time.sleep(0.5)
    sink = _resolver.resolve(mac)
    ts = time.strftime("%H:%M:%S")
    if sink:
        print(f"  [{ts}] ✓ [CONNECTED]    {mac}  →  {sink}")
    else:
        print(f"  [{ts}] ✓ [CONNECTED]    {mac}  →  (sink not yet visible — may take a moment)")


def on_disconnected(mac: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"  [{ts}] ✗ [DISCONNECTED] {mac}")


def on_property_changed(mac: str, key: str, value: object) -> None:
    # Only print notable property changes (skip verbose ones)
    notable = {"Battery", "RSSI", "TxPower"}
    if key in notable:
        ts = time.strftime("%H:%M:%S")
        print(f"  [{ts}] ~ [PROP]         {mac}  {key} = {value!r}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def main() -> None:
    print("\nPipeMix — DeviceMonitor Test")
    print("=" * 60)

    monitor = DeviceMonitor()
    monitor.on_connected        = on_connected
    monitor.on_disconnected     = on_disconnected
    monitor.on_property_changed = on_property_changed

    # Start subscribing to D-Bus signals
    try:
        monitor.start()
    except Exception as e:
        print(f"\n  ✗ Failed to start DeviceMonitor: {e}")
        print("  Is BlueZ running?  Try: systemctl status bluetooth")
        sys.exit(1)

    # --- Step 1: Show currently connected devices ---
    section("Step 1 — Currently connected BT audio devices")

    devices = monitor.get_connected_devices()
    if devices:
        resolver = BluezResolver()
        macs = [d["mac"] for d in devices]
        resolved = resolver.resolve_all(macs)
        for d in devices:
            sink = resolved.get(d["mac"])
            print(
                f"  {d['name']:<40} mac={d['mac']}"
                f"  sink={sink or '(no PipeWire sink — not connected as audio?)'}"
            )
    else:
        print("  (none)")
        print("  → Connect both earbuds before running this test for best results.")

    # --- Step 2: Watch for events ---
    section("Step 2 — Watching for events (Ctrl+C to stop)")
    print("  Disconnect and reconnect your earbuds now.\n")

    loop = GLib.MainLoop()

    def _on_sigint(signum, frame):
        print("\n\nStopping...")
        monitor.stop()
        loop.quit()

    signal.signal(signal.SIGINT, _on_sigint)

    try:
        loop.run()
    except KeyboardInterrupt:
        monitor.stop()

    print("Done.")


if __name__ == "__main__":
    main()
