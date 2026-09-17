"""
PipeMix — the JS-callable surface.

One Api instance is handed to pywebview as js_api. Every method answers
{"ok": True, "value": ...} or {"ok": False, "error": ...}; nothing raises
across the bridge.

Which devices are ticked used to live in the GTK MainWindow. That view is
gone and the backend is off-limits, so the selection lives here, along with
its two rules: a live session follows the ticks immediately, an idle one only
stages them, and hand-toggling anything clears the active preset.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any, Callable

from pipemix.models import AudioDevice
from pipemix.services.backend import BackendError
from pipemix.ui.bridge import to_json

if TYPE_CHECKING:
    from pipemix.controller import Controller

log = logging.getLogger(__name__)


def call(fn: Callable) -> Callable:
    """Called from JS, so it answers with an error rather than raising into the bridge."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> dict:
        try:
            return {"ok": True, "value": fn(*args, **kwargs)}
        except Exception as e:
            log.error("%s failed: %s", fn.__name__, e)
            return {"ok": False, "error": str(e)}
    return wrapper


class Api:

    def __init__(self, controller: Controller) -> None:
        # Underscored on purpose: pywebview walks every public non-callable
        # attribute of js_api to build the JS surface, and recursing into the
        # Controller reaches SharingSession, an unhashable dataclass, which
        # blows up its exposed-object set. Only the @call methods are public.
        self._controller = controller
        self._selected: dict[str, bool] = {}

    # ---------- Selection ----------

    def _devices_payload(self, devices: list[AudioDevice] | None = None) -> list[dict]:
        """Every device as the page sees it, selection bookkeeping refreshed first."""
        if devices is None:
            devices = list(self._controller.devices.values())

        preset = self._controller.presets.get(self._controller.last_preset or "", {})
        wanted = preset.get("devices", [])

        out = []
        for dev in devices:
            # An offline device cannot be shared to, so it cannot stay ticked.
            if not dev.connected:
                self._selected[dev.id] = False
            self._selected.setdefault(dev.id, dev.id in wanted)
            # "target" is what lets the page tell a device that dropped out of a
            # live session apart from one that was simply never enabled.
            out.append({
                **to_json(dev),
                "selected": self._selected[dev.id],
                "target": dev.id in self._controller.targets
                          or any(d.id == dev.id for d in self._controller.session.devices),
            })
        return out

    def _apply_selection(self) -> None:
        """Share to whatever is ticked, or stop if nothing is."""
        devices = [
            self._controller.devices[i]
            for i, on in self._selected.items()
            if on and i in self._controller.devices
        ]
        if devices:
            self._controller.start_sharing(devices)
        else:
            self._controller.stop_sharing()

    def _clear_preset(self) -> None:
        """A hand-toggled device no longer matches the preset."""
        if self._controller.last_preset:
            self._controller.last_preset = None

    # ---------- What the page asks for on load ----------

    @call
    def snapshot(self) -> dict:
        """First paint: signals may have fired before the page was listening."""
        return {
            "devices": self._devices_payload(),
            "state":   to_json(self._controller.session.state),
            "health":  to_json(self._controller.backend.health()),
            "master":  self._controller.master_volume,
            "sink":    self._controller.active_sink(),
            "presets": self._presets(),
            "preset":  self._controller.last_preset,
        }

    # ---------- Devices ----------

    @call
    def toggle_device(self, dev_id: str, active: bool) -> list[dict]:
        self._selected[dev_id] = active
        self._clear_preset()
        # A live session follows the ticks immediately.
        if self._controller.session.is_active:
            self._apply_selection()
        return self._devices_payload()

    @call
    def set_device_volume(self, dev_id: str, volume: int) -> None:
        self._controller.set_device_volume(dev_id, int(volume))

    @call
    def set_master_volume(self, volume: int) -> None:
        self._controller.set_master_volume(int(volume))

    # ---------- Sharing ----------

    @call
    def start_sharing(self) -> None:
        if not any(self._selected.values()):
            raise BackendError("Enable at least one output before sharing.")
        self._apply_selection()

    @call
    def active_sink(self) -> str | None:
        """Whatever the session is playing through, so streams can name it."""
        return self._controller.active_sink()

    @call
    def stop_sharing(self) -> None:
        self._controller.stop_sharing()

    @call
    def refresh(self) -> None:
        self._controller.refresh()

    # ---------- Per-app routing ----------

    @call
    def list_streams(self) -> list[dict]:
        return self._controller.backend.list_streams()

    @call
    def route_stream(self, stream_id: int, sink: str) -> None:
        self._controller.route_stream(int(stream_id), sink)

    @call
    def set_stream_mute(self, stream_id: int, mute: bool) -> None:
        self._controller.backend.set_stream_mute(int(stream_id), bool(mute))

    # ---------- Presets ----------

    def _presets(self) -> list[dict]:
        return [
            {"id": pid, "name": p.get("name", pid), "devices": p.get("devices", [])}
            for pid, p in self._controller.presets.items()
        ]

    @call
    def select_preset(self, preset_id: str | None) -> dict:
        """Load a preset's device set, or clear back to a hand-made selection."""
        if preset_id is None:
            self._controller.last_preset = None
            return {"devices": self._devices_payload(), "preset": None}

        preset = self._controller.presets.get(preset_id)
        if not preset:
            raise BackendError(f"No preset named '{preset_id}'.")

        wanted = preset.get("devices", [])
        log.info("Loading preset '%s': %s", preset.get("name"), wanted)
        self._controller.last_preset = preset_id

        for dev_id in self._selected:
            self._selected[dev_id] = dev_id in wanted
        # A preset can name a device this session has not seen yet.
        for dev_id in wanted:
            self._selected.setdefault(dev_id, True)

        if self._controller.session.is_active:
            self._apply_selection()
        return {"devices": self._devices_payload(), "preset": preset_id}

    @call
    def save_preset(self, name: str) -> dict:
        device_ids = [i for i, on in self._selected.items() if on]
        if not device_ids:
            raise BackendError("Enable at least one output before saving a preset.")

        preset_id = self._controller.save_preset(name, device_ids)
        self._controller.last_preset = preset_id
        return {"presets": self._presets(), "preset": preset_id}

    @call
    def delete_preset(self, preset_id: str) -> dict:
        self._controller.delete_preset(preset_id)
        if self._controller.last_preset == preset_id:
            self._controller.last_preset = None
        return {"presets": self._presets(), "preset": self._controller.last_preset}

    # ---------- Recovery ----------

    @call
    def shutdown(self) -> None:
        import webview
        for window in webview.windows:
            window.destroy()
