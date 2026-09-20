"""
PipeMix — entry point.

No arguments launches the GUI; --cli is the interactive text dashboard,
and --list, --share and --refresh are one-shot commands.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from gi.repository import GLib

from pipemix.linux.app import run_gui
from pipemix.linux.controller import Controller
from pipemix.linux.services.backend.pactl_backend import PactlBackend
from pipemix.linux.services.config.config_manager import ConfigManager

log = logging.getLogger("pipemix.main")


def setup_logging(debug: bool) -> None:
    """Rotating file in ~/.local/share/pipemix, plus the console."""
    log_dir = Path.home() / ".local" / "share" / "pipemix"
    log_dir.mkdir(parents=True, exist_ok=True)

    to_file = RotatingFileHandler(log_dir / "pipemix.log", maxBytes=5_000_000, backupCount=3)
    to_file.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)-25s %(message)s"
    ))

    to_console = logging.StreamHandler(sys.stdout)
    to_console.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-5s: %(message)s", datefmt="%H:%M:%S"
    ))

    root = logging.getLogger()
    root.addHandler(to_file)
    root.addHandler(to_console)
    root.setLevel(logging.DEBUG if debug else logging.INFO)


def print_status(ctrl: Controller) -> None:
    bar = "=" * 60
    print(f"\n{bar}\n  PipeMix Status — State: {ctrl.session.state.value.upper()}\n{bar}")

    if ctrl.session.is_active:
        print(f"  Active session sink: {ctrl.active_sink()}")
        print("  Sharing to:")
        for d in ctrl.session.devices:
            print(f"    - {d.name} ({d.kind.value}) [sink={d.sink}]")
    else:
        print("  No active sharing session.")

    print("\n  Available Outputs:")
    for d in ctrl.devices.values():
        battery = f" (Battery: {d.battery}%)" if d.battery is not None else ""
        status = "Connected" if d.connected else "Disconnected"
        print(f"    [{'✓' if d.connected else ' '}] [{d.kind.value:9}] "
              f"{d.name:<30} ID: {d.id:<36} {status}{battery}")

    print(f"{bar}\n  [s] share all connected  [t] stop  [r] rescan devices  [q] quit\n{bar}\n")


def run_console(ctrl: Controller) -> None:
    """Dashboard plus a GLib watch on stdin, so keys never block Bluetooth events."""
    ctrl.connect("state-changed", lambda _c, state: log.info("Session state: %s", state.value.upper()))
    ctrl.connect("devices-changed", lambda _c, devs: log.info("Device list refreshed (%d known)", len(devs)))

    ctrl.start()
    print_status(ctrl)
    loop = GLib.MainLoop()

    def stop():
        ctrl.stop()
        loop.quit()

    def on_key(_fd, _condition) -> bool:
        line = sys.stdin.readline()
        if not line:  # stdin closed
            stop()
            return False
        key = line.strip().lower()
        if not key:
            return True
        if key == "q":
            stop()
            return False

        try:
            if key == "s":
                connected = [d for d in ctrl.devices.values() if d.connected]
                if connected:
                    ctrl.start_sharing(connected)
                else:
                    print("  No connected outputs to share.")
            elif key == "t":
                ctrl.stop_sharing()
            elif key == "r":
                print("  Rescanning audio devices...")
                ctrl.refresh()
        except Exception as e:
            print(f"  Failed: {e}")

        print_status(ctrl)
        return True

    # Watch the raw fd, not a GLib.IOChannel: the channel buffers ahead of
    # sys.stdin and then stops reporting, including at EOF.
    GLib.unix_fd_add_full(
        GLib.PRIORITY_DEFAULT, sys.stdin.fileno(),
        GLib.IOCondition.IN | GLib.IOCondition.HUP, on_key,
    )
    signal.signal(signal.SIGINT, lambda _s, _f: stop())
    loop.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="PipeMix: Linux Multi-Output Audio Router")
    parser.add_argument("--cli", action="store_true", help="Interactive text dashboard")
    parser.add_argument("--list", action="store_true", help="List all detected audio outputs")
    parser.add_argument("--share", metavar="IDS", help="Share to comma-separated device IDs")
    parser.add_argument("--refresh", action="store_true", help="Re-scan audio outputs (startup also clears orphaned sinks)")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    setup_logging(args.debug)

    if not (args.cli or args.list or args.share or args.refresh):
        sys.exit(run_gui())

    ctrl = Controller(PactlBackend(), ConfigManager())

    if args.cli:
        run_console(ctrl)
        sys.exit(0)

    if args.list:
        ctrl.refresh()
        print("\nPipeMix Detected Output Devices:")
        print("-" * 60)
        for d in ctrl.devices.values():
            status = "Connected" if d.connected else "Disconnected"
            print(f"[{d.kind.value:9}] {d.name:<30} ID: {d.id:<36} ({status})")
        print("-" * 60)
        sys.exit(0)

    if args.refresh:
        ctrl.refresh()
        sys.exit(0)

    ctrl.refresh()
    targets = []
    for dev_id in (i.strip() for i in args.share.split(",") if i.strip()):
        dev = ctrl.devices.get(dev_id)
        if not dev:
            log.error("Device ID '%s' not found. Run --list to verify IDs.", dev_id)
            sys.exit(1)
        if not dev.connected:
            log.error("Device '%s' (%s) is disconnected.", dev.name, dev_id)
            sys.exit(1)
        targets.append(dev)

    try:
        ctrl.start_sharing(targets)
    except Exception as e:
        log.error("Failed to share audio: %s", e)
        sys.exit(1)

    log.info("Sharing active. Press Ctrl+C to stop sharing and exit.")
    loop = GLib.MainLoop()

    def quit(_sig, _frame):
        print("\nStopping sharing...")
        ctrl.stop_sharing()
        loop.quit()

    signal.signal(signal.SIGINT, quit)
    loop.run()


if __name__ == "__main__":
    main()
