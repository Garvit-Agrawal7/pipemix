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
        self.controller = controller
        self.selected: dict[str, bool] = {}

    # ---------- Selection ----------

    def devices_payload(self, devices: list[AudioDevice] | None = None) -> list[dict]:
        """Every device as the page sees it, selection bookkeeping refreshed first."""
        if devices is None:
            devices = list(self.controller.devices.values())

        preset = self.controller.presets.get(self.controller.last_preset or "", {})
        wanted = preset.get("devices", [])

        out = []
        for dev in devices:
            # An offline device cannot be shared to, so it cannot stay ticked.
            if not dev.connected:
                self.selected[dev.id] = False
            self.selected.setdefault(dev.id, dev.id in wanted)
            # "target" is what lets the page tell a device that dropped out of a
            # live session apart from one that was simply never enabled.
            out.append({
                **to_json(dev),
                "selected": self.selected[dev.id],
                "target": dev.id in self.controller.targets
                          or any(d.id == dev.id for d in self.controller.session.devices),
            })
        return out

    def _apply_selection(self) -> None:
        """Share to whatever is ticked, or stop if nothing is."""
        devices = [
            self.controller.devices[i]
            for i, on in self.selected.items()
            if on and i in self.controller.devices
        ]
        if devices:
            self.controller.start_sharing(devices)
        else:
            self.controller.stop_sharing()

    def _clear_preset(self) -> None:
        """A hand-toggled device no longer matches the preset."""
        if self.controller.last_preset:
            self.controller.last_preset = None

    # ---------- What the page asks for on load ----------

    @call
    def snapshot(self) -> dict:
        """First paint: signals may have fired before the page was listening."""
        return {
            "devices": self.devices_payload(),
            "state":   to_json(self.controller.session.state),
            "health":  to_json(self.controller.backend.health()),
            "master":  self.controller.master_volume,
            "sink":    self.controller.active_sink(),
            "presets": self._presets(),
            "preset":  self.controller.last_preset,
        }

    # ---------- Devices ----------

    @call
    def toggle_device(self, dev_id: str, active: bool) -> list[dict]:
        self.selected[dev_id] = active
        self._clear_preset()
        # A live session follows the ticks immediately.
        if self.controller.session.is_active:
            self._apply_selection()
        return self.devices_payload()

    @call
    def set_device_volume(self, dev_id: str, volume: int) -> None:
        self.controller.set_device_volume(dev_id, int(volume))

    @call
    def set_master_volume(self, volume: int) -> None:
        self.controller.set_master_volume(int(volume))

    # ---------- Sharing ----------

    @call
    def start_sharing(self) -> None:
        if not any(self.selected.values()):
            raise BackendError("Enable at least one output before sharing.")
        self._apply_selection()

    @call
    def active_sink(self) -> str | None:
        """Whatever the session is playing through, so streams can name it."""
        return self.controller.active_sink()

    @call
    def stop_sharing(self) -> None:
        self.controller.stop_sharing()

    @call
    def reset_audio(self) -> None:
        self.controller.reset_audio()

    @call
    def clean_orphans(self) -> None:
        self.controller.clean_orphans()

    # ---------- Per-app routing ----------

    @call
    def list_streams(self) -> list[dict]:
        return self.controller.backend.list_streams()

    @call
    def route_stream(self, stream_id: int, sink: str) -> None:
        self.controller.route_stream(int(stream_id), sink)

    @call
    def set_stream_mute(self, stream_id: int, mute: bool) -> None:
        self.controller.backend.set_stream_mute(int(stream_id), bool(mute))

    # ---------- Presets ----------

    def _presets(self) -> list[dict]:
        return [
            {"id": pid, "name": p.get("name", pid), "devices": p.get("devices", [])}
            for pid, p in self.controller.presets.items()
        ]

    @call
    def presets(self) -> list[dict]:
        return self._presets()

    @call
    def select_preset(self, preset_id: str | None) -> dict:
        """Load a preset's device set, or clear back to a hand-made selection."""
        if preset_id is None:
            self.controller.last_preset = None
            return {"devices": self.devices_payload(), "preset": None}

        preset = self.controller.presets.get(preset_id)
        if not preset:
            raise BackendError(f"No preset named '{preset_id}'.")

        wanted = preset.get("devices", [])
        log.info("Loading preset '%s': %s", preset.get("name"), wanted)
        self.controller.last_preset = preset_id

        for dev_id in self.selected:
            self.selected[dev_id] = dev_id in wanted
        # A preset can name a device this session has not seen yet.
        for dev_id in wanted:
            self.selected.setdefault(dev_id, True)

        if self.controller.session.is_active:
            self._apply_selection()
        return {"devices": self.devices_payload(), "preset": preset_id}

    @call
    def save_preset(self, name: str, device_ids: list[str] | None = None) -> dict:
        if device_ids is None:
            device_ids = [i for i, on in self.selected.items() if on]
        if not device_ids:
            raise BackendError("Enable at least one output before saving a preset.")

        preset_id = self.controller.save_preset(name, device_ids)
        self.controller.last_preset = preset_id
        return {"presets": self._presets(), "preset": preset_id}

    @call
    def delete_preset(self, preset_id: str) -> dict:
        self.controller.delete_preset(preset_id)
        if self.controller.last_preset == preset_id:
            self.controller.last_preset = None
        return {"presets": self._presets(), "preset": self.controller.last_preset}

    # ---------- Recovery ----------

    @call
    def shutdown(self) -> None:
        import webview
        for window in webview.windows:
            window.destroy()
