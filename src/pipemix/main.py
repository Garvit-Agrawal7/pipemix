"""
PipeMix — Command-Line Interface and Entry Point

Acts as both a command-line tool and the entry point for the service/GUI app.
Uses argparse to parse arguments and interacts with the Controller.

Modes:
  1. Default: Interactive CLI loop. Starts Controller, runs GLib event loop,
     and monitors Bluetooth connect/disconnect events in real-time.
  2. CLI commands:
     - --list: Enumerate all output devices and their status.
     - --share <ids>: Start a sharing session with specified device IDs (comma-separated).
     - --reset: Run the Reset Audio routine to clean up virtual sinks.
     - --debug: Enable verbose debug logging.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

# Add src/ to path so it can run directly from source checkout
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from gi.repository import GLib

from pipemix.controller import Controller
from pipemix.models import DeviceKind, SessionState
from pipemix.services.backend.pactl_backend import PactlBackend
from pipemix.services.config.config_manager import ConfigManager
from pipemix.app import PipeMixApplication

log = logging.getLogger("pipemix.main")

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------

def setup_logging(debug: bool) -> None:
    """Configures Python standard logging with rotating files and console outputs."""
    import logging
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    # 1. Config path
    log_dir = Path.home() / ".local" / "share" / "pipemix"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "pipemix.log"

    # 2. File handler (Rotating: 3x 5MB)
    file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3)
    file_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)-25s %(message)s"
    )
    file_handler.setFormatter(file_formatter)

    # 3. Stream handler (Console)
    console_handler = logging.StreamHandler(sys.stdout)
    console_formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)-5s: %(message)s", datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(console_formatter)

    # 4. Root logger setup
    root = logging.getLogger()
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    # Silence verbose 3rd-party loggers
    logging.getLogger("dbus").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Interactive Loop Callbacks
# ---------------------------------------------------------------------------

def print_status(controller: Controller) -> None:
    """Prints current devices and sharing session status to console."""
    print(f"\n{'='*60}")
    print(f"  PipeMix Status — State: {controller.session.state.value.upper()}")
    print(f"{'='*60}")
    
    if controller.session.is_active and controller.session.virtual_sink:
        print(f"  Active Session Sink: {controller.session.virtual_sink.sink_name}")
        print("  Sharing to:")
        for dev in controller.session.selected_devices:
            print(f"    - {dev.display_name} ({dev.kind.value}) [sink={dev.sink_name}]")
    else:
        print("  No active sharing session.")

    print("\n  Available Outputs:")
    for d in controller.known_devices.values():
        status = "Connected" if d.is_connected else "Disconnected"
        battery = f" (Battery: {d.battery_level}%)" if d.battery_level is not None else ""
        print(
            f"    [{'✓' if d.is_connected else ' '}] [{d.kind.value:9}] "
            f"{d.display_name:<30} ID: {d.device_id:<36} {status}{battery}"
        )
    print(f"{'='*60}")
    print("  Controls: [s] Start sharing all connected  [t] Stop sharing  [r] Reset audio  [q] Quit")
    print(f"{'='*60}\n")


def on_state_changed(controller: Controller, state: SessionState) -> None:
    log.info("Session state transitioned to: %s", state.value.upper())


def on_device_list_updated(controller: Controller, devices: list) -> None:
    log.info("Device list refreshed (%d total devices known)", len(devices))


# ---------------------------------------------------------------------------
# Interactive Keyboard Input via GLib
# ---------------------------------------------------------------------------

def setup_keyboard_listener(controller: Controller, loop: GLib.MainLoop) -> None:
    """Uses GLib IO channels to asynchronously listen for stdin input without blocking the main loop."""
    
    def on_stdin_readable(channel, condition):
        line = sys.stdin.readline().strip().lower()
        if not line:
            return True

        if line == "q":
            log.info("Quitting PipeMix...")
            controller.stop()
            loop.quit()
            return False
        
        elif line == "s":
            connected = [d for d in controller.known_devices.values() if d.is_connected]
            if not connected:
                print("Error: No connected outputs found to share.")
            else:
                try:
                    controller.start_sharing(connected)
                    print_status(controller)
                except Exception as e:
                    print(f"Failed to start sharing: {e}")
        
        elif line == "t":
            try:
                controller.stop_sharing()
                print_status(controller)
            except Exception as e:
                print(f"Failed to stop sharing: {e}")

        elif line == "r":
            print("Resetting audio system...")
            controller.reset_audio()
            print_status(controller)

        else:
            print_status(controller)

        return True

    # Register stdin watch with GLib
    channel = GLib.IOChannel(sys.stdin.fileno())
    GLib.io_add_watch(channel, GLib.PRIORITY_DEFAULT, GLib.IO_IN, on_stdin_readable)


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="PipeMix: Linux Multi-Output Audio Router")
    parser.add_argument("--cli", action="store_true", help="Launch the text-based interactive console dashboard")
    parser.add_argument("--list", action="store_true", help="List all detected audio output devices")
    parser.add_argument("--share", metavar="IDS", help="Start sharing to comma-separated device IDs")
    parser.add_argument("--reset", action="store_true", help="Reset audio routing and clear virtual sinks")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging")
    args = parser.parse_args()

    # Setup standard Python logging
    setup_logging(args.debug)

    # 1. Run GUI Application (default mode)
    if not (args.cli or args.list or args.share or args.reset):
        app = PipeMixApplication()
        sys.exit(app.run(sys.argv))

    # Instantiate core components for CLI paths
    config = ConfigManager()
    backend = PactlBackend()
    controller = Controller(backend, config)

    # 2. Run standalone actions (non-looping)
    if args.list:
        controller.refresh_devices()
        print("\nPipeMix Detected Output Devices:")
        print("-" * 60)
        for d in controller.known_devices.values():
            status = "Connected" if d.is_connected else "Disconnected"
            print(f"[{d.kind.value:9}] {d.display_name:<30} ID: {d.device_id:<36} ({status})")
        print("-" * 60)
        sys.exit(0)

    if args.reset:
        log.info("Executing manual audio system reset...")
        controller.reset_audio()
        sys.exit(0)

    if args.share:
        target_ids = [i.strip() for i in args.share.split(",") if i.strip()]
        log.info("Triggering sharing session to: %s", target_ids)
        controller.refresh_devices()
        
        # Resolve target devices
        targets = []
        for dev_id in target_ids:
            dev = controller.known_devices.get(dev_id)
            if not dev:
                log.error("Device ID '%s' not found. Run --list to verify IDs.", dev_id)
                sys.exit(1)
            if not dev.is_connected:
                log.error("Device '%s' (%s) is disconnected.", dev.display_name, dev_id)
                sys.exit(1)
            targets.append(dev)

        try:
            controller.start_sharing(targets)
            log.info("Sharing active. Press Ctrl+C to stop sharing and exit.")
            
            # Simple synchronous wait loop for CLI mode
            # Restores default on SIGINT
            loop = GLib.MainLoop()
            
            import signal
            def sigint_handler(sig, frame):
                print("\nStopping sharing...")
                controller.stop_sharing()
                loop.quit()
                
            signal.signal(signal.SIGINT, sigint_handler)
            loop.run()
            sys.exit(0)
        except Exception as e:
            log.error("Failed to share audio: %s", e)
            sys.exit(1)

    # 3. Run Interactive Console Daemon Mode (if --cli is passed)
    # Connect signal listeners
    controller.connect("session-state-changed", on_state_changed)
    controller.connect("device-list-updated", on_device_list_updated)

    # Start controller & start monitor loops
    controller.start()

    # Print initial status menu
    print_status(controller)

    loop = GLib.MainLoop()
    
    # Listen for keystrokes asynchronously using GLib
    setup_keyboard_listener(controller, loop)

    # Graceful SIGINT handling
    import signal
    def sigint_handler(sig, frame):
        print("\nStopping PipeMix...")
        controller.stop()
        loop.quit()
    signal.signal(signal.SIGINT, sigint_handler)

    try:
        loop.run()
    except KeyboardInterrupt:
        controller.stop()

if __name__ == "__main__":
    main()
