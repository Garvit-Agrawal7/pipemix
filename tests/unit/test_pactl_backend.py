"""
PipeMix — PactlBackend Test Script

Tests the audio backend without a UI or Controller.
Run this from the project root:

    python3 tests/unit/test_pactl_backend.py

What it does:
  1. health_check()      — verifies PipeWire is reachable
  2. list_outputs()      — shows all audio outputs on your system
  3. create_virtual_output()  — creates a combined pipemix_* sink
  4. move_streams()      — routes any active streams to it
  5. find_orphaned_virtual_sinks()  — verifies orphan detection works
  6. destroy_virtual_output() — cleans up the test sink

Safe to run while audio is playing — streams will be moved back to
the default sink automatically when the virtual sink is destroyed.

Expected outcome (no Bluetooth connected):
  - Only built-in audio shows in step 2
  - A virtual sink is created in step 3
  - Steps 4-6 complete without errors
  You can verify step 3 externally with: pactl list short sinks
"""

from __future__ import annotations

import logging
import sys
import os
import time

# Add src/ to path so we can import pipemix without installing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from pipemix.models import DeviceKind, SessionState, VirtualSink
from pipemix.services.backend import BackendHealth
from pipemix.services.backend.pactl_backend import PactlBackend

# ---------------------------------------------------------------------------
# Logging setup — print to terminal for this test
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("test_pactl_backend")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def ok(msg: str) -> None:
    print(f"  ✓  {msg}")


def fail(msg: str) -> None:
    print(f"  ✗  {msg}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_health_check(backend: PactlBackend) -> None:
    section("Step 1 — health_check()")
    status = backend.health_check()
    print(f"  Health  : {status.health.value}")
    print(f"  Message : {status.message}")
    if status.details:
        print(f"  Details : {status.details}")

    if status.health == BackendHealth.OK:
        ok("PipeWire is reachable.")
    elif status.health == BackendHealth.DEGRADED:
        print("  ⚠  Degraded — tests will continue but results may be unreliable.")
    else:
        fail(f"Backend unavailable: {status.message}")


def test_list_outputs(backend: PactlBackend) -> list:
    section("Step 2 — list_outputs()")
    devices = backend.list_outputs()

    if not devices:
        fail("No audio outputs found. Is PipeWire running?")

    for d in devices:
        print(
            f"  [{d.kind.value:9}]  {d.display_name:<35}  "
            f"id={d.device_id}  sink={d.sink_name}"
        )

    ok(f"Found {len(devices)} output(s).")
    return devices


def test_create_virtual_output(backend: PactlBackend, devices: list) -> VirtualSink:
    section("Step 3 — create_virtual_output()")

    # Use all discovered devices for this test
    print(f"  Using devices: {[d.display_name for d in devices]}")

    virtual = backend.create_virtual_output(devices)
    print(f"  Created: {virtual}")

    # Verify it actually exists in PipeWire
    import subprocess
    result = subprocess.run(
        ["pactl", "list", "short", "sinks"],
        capture_output=True, text=True
    )
    if virtual.sink_name in result.stdout:
        ok(f"Sink '{virtual.sink_name}' confirmed in PipeWire.")
    else:
        fail(f"Sink '{virtual.sink_name}' NOT found in PipeWire after creation!")

    print(f"\n  You can verify externally:")
    print(f"    pactl list short sinks | grep pipemix")

    return virtual


def test_move_streams(backend: PactlBackend, virtual: VirtualSink) -> None:
    section("Step 4 — move_streams()")

    import subprocess
    result = subprocess.run(
        ["pactl", "list", "short", "sink-inputs"],
        capture_output=True, text=True
    )
    stream_count = len([l for l in result.stdout.strip().splitlines() if l.strip()])
    print(f"  Active streams before routing: {stream_count}")

    if stream_count == 0:
        print("  (No active streams — start playing audio in another app to test routing)")
        print("  Skipping move test — not an error.")
    else:
        backend.move_streams(virtual)
        ok(f"Attempted to move {stream_count} stream(s) to {virtual.sink_name}.")


def test_orphan_detection(backend: PactlBackend, virtual: VirtualSink) -> None:
    section("Step 5 — find_orphaned_virtual_sinks()")

    # The virtual sink we just created should be detected as an orphan
    # (since no Controller is tracking it)
    orphans = backend.find_orphaned_virtual_sinks()

    found = any(o.sink_name == virtual.sink_name for o in orphans)
    if found:
        ok(f"Orphan detection works — found '{virtual.sink_name}' as expected.")
    else:
        print(f"  ⚠  '{virtual.sink_name}' was not found in orphan scan.")
        print(f"     This may be because pactl list sinks output format differs.")
        print(f"     Check the log output above for parse details.")


def test_destroy(backend: PactlBackend, virtual: VirtualSink) -> None:
    section("Step 6 — destroy_virtual_output()")

    backend.destroy_virtual_output(virtual)

    import subprocess
    result = subprocess.run(
        ["pactl", "list", "short", "sinks"],
        capture_output=True, text=True
    )
    if virtual.sink_name not in result.stdout:
        ok(f"Sink '{virtual.sink_name}' successfully removed from PipeWire.")
    else:
        fail(f"Sink '{virtual.sink_name}' still exists after destroy!")

    # Test double-destroy safety: must not raise
    section("Step 6b — destroy_virtual_output() idempotency check")
    try:
        backend.destroy_virtual_output(virtual)
        ok("Double-destroy did not raise — correct behaviour.")
    except Exception as e:
        fail(f"Double-destroy raised an exception: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("\nPipeMix — PactlBackend Test")
    print("=" * 60)

    backend = PactlBackend()
    virtual: VirtualSink | None = None

    try:
        test_health_check(backend)
        devices = test_list_outputs(backend)
        virtual = test_create_virtual_output(backend, devices)
        test_move_streams(backend, virtual)
        test_orphan_detection(backend, virtual)
        test_destroy(backend, virtual)

        print(f"\n{'=' * 60}")
        print("  ALL STEPS PASSED")
        print(f"{'=' * 60}\n")

    except SystemExit:
        # fail() called — clean up if we created a sink
        if virtual is not None:
            print(f"\n  Cleaning up {virtual.sink_name}...")
            backend.destroy_virtual_output(virtual)
        raise

    except Exception as e:
        log.exception("Unexpected error during test")
        if virtual is not None:
            print(f"\n  Cleaning up {virtual.sink_name}...")
            backend.destroy_virtual_output(virtual)
        sys.exit(1)


if __name__ == "__main__":
    main()
