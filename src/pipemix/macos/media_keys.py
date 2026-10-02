"""The keyboard's volume keys, while the hub is the default output.

A Multi-Output Device has no volume of its own, so while PipeMix shares,
macOS greys out the volume keys and shows the "unavailable" bezel. The key
presses still reach every app as NSEventTypeSystemDefined events (subtype 8,
the media keys), so PipeMix listens for them and moves its master fader
instead. Media keys are not "key events" in AppKit's sense, so watching them
needs no Accessibility permission.

Only the GUI installs this: it needs the Cocoa run loop pywebview runs.
"""

from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)

NX_SUBTYPE_AUX_CONTROL_BUTTONS = 8
NX_KEYTYPE_SOUND_UP   = 0
NX_KEYTYPE_SOUND_DOWN = 1
NX_KEYTYPE_MUTE       = 7
KEY_DOWN = 0xA

STEP = 100 / 16                 # macOS moves volume in sixteenths
FINE_STEP = 100 / 64            # ⌥⇧ + volume key: quarter steps
FLAG_SHIFT, FLAG_OPTION = 1 << 17, 1 << 19


def decode(data1: int, modifier_flags: int = 0) -> tuple[str, float] | None:
    """("up"|"down"|"mute", step) for a media-key press, None for anything else
    (other keys, key-up, auto-repeat is kept)."""
    code = (data1 & 0xFFFF0000) >> 16
    state = (data1 & 0xFF00) >> 8
    if state != KEY_DOWN:
        return None
    fine = (modifier_flags & FLAG_SHIFT) and (modifier_flags & FLAG_OPTION)
    step = FINE_STEP if fine else STEP
    if code == NX_KEYTYPE_SOUND_UP:
        return "up", step
    if code == NX_KEYTYPE_SOUND_DOWN:
        return "down", step
    if code == NX_KEYTYPE_MUTE:
        return "mute", 0
    return None


class MediaKeys:
    """Calls `on_key(action, step)` for each volume-key press, from the main thread."""

    def __init__(self, on_key: Callable[[str, float], bool]) -> None:
        # on_key returns True when it handled the key (a session is live);
        # the local monitor then swallows it so macOS does not beep.
        self._on_key = on_key
        self._monitors: list = []

    def install(self) -> None:
        """Must run on the main thread, inside the Cocoa app."""
        from AppKit import NSEvent

        mask = 1 << 14  # NSEventMaskSystemDefined

        def handle(event) -> bool:
            try:
                if event.subtype() != NX_SUBTYPE_AUX_CONTROL_BUTTONS:
                    return False
                key = decode(event.data1(), event.modifierFlags())
                return bool(key and self._on_key(*key))
            except Exception:
                log.exception("Volume key handler failed")
                return False

        # Global: presses while another app is in front. Local: while PipeMix is.
        self._monitors.append(NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, handle))
        self._monitors.append(NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            mask, lambda e: None if handle(e) else e))
        log.info("Listening for the volume keys.")

    def remove(self) -> None:
        if not self._monitors:
            return
        from AppKit import NSEvent
        for m in self._monitors:
            if m is not None:
                NSEvent.removeMonitor_(m)
        self._monitors.clear()
