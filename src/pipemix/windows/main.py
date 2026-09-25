"""
PipeMix — entry point (Windows).

No arguments launches the GUI; --cli is the interactive text dashboard,
and --list, --share and --refresh are one-shot commands.

Forked from `pipemix.linux.main`. What changed and why:

- No GLib: the CLI paths are straight-line calls, and the GUI path blocks in
  `webview.start()`. `--share` keeps the process alive with a plain
  `threading.Event` instead of `GLib.MainLoop`, and `--cli` reads stdin in a
  blocking loop instead of watching a GLib-registered fd — the notification
  worker thread that drives hotplug already runs on its own thread, so there
  is no main loop to pump for it.
- The log directory comes from `config_manager.default_log_dir()`, which is
  already platform-aware and resolves to `%LOCALAPPDATA%\\PipeMix\\logs` here.
- `WasapiBackend` instead of `PactlBackend`.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler

from pipemix.windows.backend import WasapiBackend
from pipemix.windows.controller import Controller
from pipemix.linux.services.config.config_manager import ConfigManager, default_log_dir

log = logging.getLogger("pipemix.main")


def setup_logging(debug: bool) -> None:
    """Rotating file in %LOCALAPPDATA%\\PipeMix\\logs, plus the console."""
    log_dir = default_log_dir()
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
    """Dashboard plus a blocking read of stdin.

    Unlike the Linux version there is no GLib main loop to share, so hotplug
    events do not need a fd watch to be pumped — `wasapi.notify`'s worker
    thread already calls back into the Controller on its own, whether or not
    anyone is reading a key right now.
    """
    ctrl.connect("state-changed", lambda state: log.info("Session state: %s", state.value.upper()))
    ctrl.connect("devices-changed", lambda devs: log.info("Device list refreshed (%d known)", len(devs)))

    ctrl.start()
    print_status(ctrl)

    try:
        while True:
            line = sys.stdin.readline()
            if not line:  # stdin closed
                break
            key = line.strip().lower()
            if not key:
                continue
            if key == "q":
                break

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
    except KeyboardInterrupt:
        pass
    finally:
        ctrl.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="PipeMix: Windows Multi-Output Audio Router")
    parser.add_argument("--cli", action="store_true", help="Interactive text dashboard")
    parser.add_argument("--list", action="store_true", help="List all detected audio outputs")
    parser.add_argument("--share", metavar="IDS", help="Share to comma-separated device IDs")
    parser.add_argument("--refresh", action="store_true", help="Re-scan audio outputs (startup also clears orphaned sinks)")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    setup_logging(args.debug)

    if not (args.cli or args.list or args.share or args.refresh):
        # Deferred: pywebview is a GUI-only dependency, and --list/--cli/
        # --share/--refresh must keep working on a machine that lacks it.
        from pipemix.windows.app import run_gui
        sys.exit(run_gui())

    ctrl = Controller(WasapiBackend(), ConfigManager())

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
    stop_event = threading.Event()

    def quit(_sig, _frame):
        print("\nStopping sharing...")
        ctrl.stop_sharing()
        stop_event.set()

    signal.signal(signal.SIGINT, quit)
    stop_event.wait()


if __name__ == "__main__":
    main()
