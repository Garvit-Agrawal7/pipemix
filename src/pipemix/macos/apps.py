"""Per-app audio on macOS: which apps are playing, and sending one elsewhere.

macOS has no way to move another app's stream to a device. What it does
have, since 14.2, is the process tap: capture one app's mix, and optionally
mute it at the source. So "route Spotify to the kitchen speaker" is

    tap(Spotify, muted while tapped)
      → private aggregate [kitchen speaker] + the tap
      → an IOProc (`_tapcopy.c`) copying the tap to the speaker

and "mute Spotify" is a tap with mute and nobody reading it. Everything here
is torn down with the process — taps and private aggregates die with their
creator — so a crash cannot strand an app muted.

Taps need the "System Audio Recording" permission. Without it they deliver
silence and say nothing; `Route.silent()` is how the backend notices.
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
import uuid
from dataclasses import dataclass, field

from pipemix.macos import coreaudio as ca
from pipemix.macos import tapcopy

log = logging.getLogger(__name__)

ROUTE_UID_PREFIX = "com.pipemix.route"

# How long an app may play into a tap that delivers nothing before we call
# it a permission problem rather than a quiet passage.
SILENT_AFTER_S = 3.0


# ---------- Permission ----------

PERMISSION_GRANTED, PERMISSION_DENIED, PERMISSION_UNKNOWN = 0, 1, 2


def permission() -> int:
    """System Audio Recording for this app: granted, denied, or not asked yet.

    TCCAccessPreflight is private, but it is how Apple's own tap sample
    checks without prompting; if it is missing, say "not asked" and let the
    tap raise the prompt itself.
    """
    try:
        tcc = ctypes.CDLL("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC")
        fn = tcc.TCCAccessPreflight
        fn.argtypes, fn.restype = [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_long
        service = ca._cf_str("kTCCServiceAudioCapture")
        try:
            result = fn(service, None)
        finally:
            ca._release(service)
        return result if result in (PERMISSION_GRANTED, PERMISSION_DENIED) else PERMISSION_UNKNOWN
    except Exception:
        return PERMISSION_UNKNOWN


# ---------- Which apps are playing ----------

def _responsible_pid(pid: int) -> int:
    """The app a helper process works for (Safari for WebKit.GPU, Chrome for
    its helpers), via the same lookup Activity Monitor uses."""
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        fn = libc.responsibility_get_pid_responsible_for_pid
        fn.argtypes, fn.restype = [ctypes.c_int], ctypes.c_int
        rpid = fn(pid)
        return rpid if rpid > 0 else pid
    except Exception:
        return pid


def _proc_name(pid: int) -> str:
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        buf = ctypes.create_string_buffer(256)
        if libc.proc_name(pid, buf, 256) > 0:
            return buf.value.decode("utf-8", "replace")
    except Exception:
        pass
    return ""


def app_name(pid: int, bundle_id: str) -> str:
    """What the user would call it: the app itself if it is one (Music,
    Spotify), the app a helper works for (Safari for WebKit's GPU process,
    Chrome for its helpers), else the process name (afplay from a shell)."""
    try:
        from AppKit import NSApplicationActivationPolicyRegular, NSRunningApplication
        for p in (pid, _responsible_pid(pid)):
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(p)
            if (app is not None and app.localizedName()
                    and app.activationPolicy() == NSApplicationActivationPolicyRegular):
                return str(app.localizedName())
    except Exception:
        pass
    return _proc_name(pid) or bundle_id or f"pid {pid}"


def playing(extra_pids: set[int] = frozenset()) -> list[dict]:
    """Apps now playing (plus `extra_pids`, which we are routing or muting and
    so want to keep showing while they pause)."""
    out = []
    me = os.getpid()
    for proc in ca.process_ids():
        try:
            pid = ca.process_pid(proc)
        except ca.CoreAudioError:
            continue
        if pid == me:
            continue
        active = ca.process_is_playing(proc)
        if not (active or pid in extra_pids):
            continue
        bundle = ca.process_bundle_id(proc)
        devices = ca.process_output_devices(proc)
        try:
            endpoint = ca.uid(devices[0]) if devices else None
        except ca.CoreAudioError:
            endpoint = None
        out.append({
            "id": pid,
            "name": app_name(pid, bundle),
            "exe": bundle or _proc_name(pid),
            "sink": endpoint,
            "endpoint": endpoint,
            "active": active,
            "mute": False,
        })
    out.sort(key=lambda s: (s["name"].lower(), s["id"]))
    return out


# ---------- Routing one app ----------

@dataclass
class Route:
    """One app's tap, and — unless it is only muted — where its audio goes."""
    pid: int
    devices: list[str] = field(default_factory=list)  # empty: muted, nowhere
    tap: int = 0
    agg: int = 0
    proc_id: int = 0
    meter: tapcopy.Meter | None = None
    started: float = 0.0
    last_sound: float = 0.0

    @property
    def muted(self) -> bool:
        return not self.devices

    def start(self) -> None:
        proc = ca.process_for_pid(self.pid)
        if proc is None:
            raise ca.CoreAudioError(f"no audio process for pid {self.pid}", -1)
        mute = ca.TAP_MUTED if self.muted else ca.TAP_MUTED_WHEN_TAPPED
        self.tap, tap_uuid = ca.create_process_tap(proc, mute, f"PipeMix pid {self.pid}")
        self.started = self.last_sound = time.monotonic()
        if self.muted:
            log.info("Muted pid %d", self.pid)
            return
        try:
            self._render(tap_uuid)
        except Exception:
            self.stop()
            raise
        log.info("Routing pid %d → %s", self.pid, self.devices)

    def _render(self, tap_uuid: str) -> None:
        from pipemix.macos.devices import clock_rank
        main = min(self.devices, key=clock_rank)
        subs = [main] + [d for d in self.devices if d != main]
        self.agg = ca.create_aggregate(
            f"{ROUTE_UID_PREFIX}.{self.pid}.{uuid.uuid4().hex[:6]}", f"PipeMix route {self.pid}",
            subs, main, private=True, stacked=False, taps=[tap_uuid])
        self.meter = tapcopy.Meter()
        self.proc_id = ca.create_ioproc(self.agg, tapcopy.proc_pointer(), ctypes.pointer(self.meter))
        ca.only_last_inputs(self.agg, self.proc_id, keep=1)
        ca.start_ioproc(self.agg, self.proc_id)

    def stop(self) -> None:
        if self.proc_id and self.agg:
            try:
                ca.destroy_ioproc(self.agg, self.proc_id)
            except Exception as e:
                log.debug("IOProc teardown for pid %d: %s", self.pid, e)
        if self.agg:
            try:
                ca.destroy_aggregate(self.agg)
            except Exception as e:
                log.debug("Aggregate teardown for pid %d: %s", self.pid, e)
        if self.tap:
            try:
                ca.destroy_process_tap(self.tap)
            except Exception as e:
                log.debug("Tap teardown for pid %d: %s", self.pid, e)
        self.tap = self.agg = self.proc_id = 0
        self.meter = None

    def silent(self, app_playing: bool) -> bool:
        """True once the app has played into this tap for a while and the tap
        has delivered nothing — the signature of a missing permission."""
        if self.muted or self.meter is None:
            return False
        now = time.monotonic()
        if self.meter.peak > 0.0:
            self.last_sound = now
            self.meter.peak = 0.0
            return False
        return app_playing and now - self.last_sound > SILENT_AFTER_S


class AppRouter:
    """Every app PipeMix is currently routing or muting, keyed by pid."""

    def __init__(self) -> None:
        self.routes: dict[int, Route] = {}

    def apply(self, pid: int, devices: list[str] | None, mute: bool) -> None:
        """Make `pid` muted, routed to `devices`, or (neither) left alone."""
        want = [] if mute else list(devices or [])
        current = self.routes.get(pid)
        if current and current.devices == want:
            return
        if current:
            current.stop()
            del self.routes[pid]
        if not mute and not want:
            return
        route = Route(pid, want)
        route.start()
        self.routes[pid] = route

    def clear(self, pid: int) -> None:
        route = self.routes.pop(pid, None)
        if route:
            route.stop()

    def clear_all(self) -> None:
        for pid in list(self.routes):
            self.clear(pid)
