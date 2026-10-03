from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Callable

from pipemix.models import BackendError
from pipemix.bridge import to_json

if TYPE_CHECKING:
    from pipemix.linux.controller import Controller

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
        # Underscored: pywebview recurses into public attributes to build the JS
        # surface and crashes on the unhashable SharingSession inside the Controller.
        self._controller = controller
        self._selected: dict[str, bool] = {}

    # ---------- Selection ----------

    def _devices_payload(self) -> list[dict]:
        """Every device as the page sees it, selection bookkeeping refreshed first."""
        preset = self._controller.config.presets.get(self._controller.config.last_preset or "", {})
        wanted = preset.get("devices", [])

        out = []
        for dev in self._controller.devices.values():
            # An offline device cannot be shared to, so it cannot stay ticked, and
            # one the session takes back when it returns has to be ticked again.
            if not dev.connected:
                self._selected[dev.id] = False
            elif dev in self._controller.session.devices:
                self._selected[dev.id] = True
            self._selected.setdefault(dev.id, dev.id in wanted)
            # "target" tells a device that dropped out of the session from one never enabled.
            out.append({
                **to_json(dev),
                "selected": self._selected[dev.id],
                "target": dev.id in self._controller.targets
                          or any(d.id == dev.id for d in self._controller.session.devices),
                "primary": dev.id == getattr(self._controller.backend, "leader", None),
            })
        return out

    def _apply_selection(self) -> None:
        """Share to whatever is ticked, or stop if nothing is. Locked so two quick
        toggles apply in order and the last one wins (RLock, re-entrant with @locked)."""
        with self._controller._lock:
            devices = [
                self._controller.devices[i]
                for i, on in self._selected.items()
                if on and i in self._controller.devices
            ]
            if devices:
                self._controller.start_sharing(devices)
            else:
                self._controller.stop_sharing()

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
            "preset":  self._controller.config.last_preset,
        }

    # ---------- Devices ----------

    @call
    def toggle_device(self, dev_id: str, active: bool) -> list[dict]:
        with self._controller._lock:
            self._selected[dev_id] = active
            # A hand-toggled device no longer matches the preset.
            if self._controller.config.last_preset:
                self._controller.config.last_preset = None
            # A live session follows the ticks immediately.
            if self._controller.session.is_active:
                self._apply_selection()
        return self._devices_payload()

    @call
    def set_device_volume(self, dev_id: str, volume: int) -> int:
        """Answers with the master level, which follows a lone output."""
        self._controller.set_device_volume(dev_id, int(volume))
        return self._controller.master_volume

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
    def stop_sharing(self) -> None:
        self._controller.stop_sharing()

    @call
    def refresh(self) -> None:
        self._controller.refresh()

    # ---------- Per-app routing ----------

    @call
    def list_streams(self) -> list[dict]:
        return self._controller.streams()

    @call
    def route_stream(self, stream_id: int, devices: list[str] | None) -> None:
        self._controller.route_stream(int(stream_id), devices)

    @call
    def set_stream_mute(self, stream_id: int, mute: bool) -> None:
        self._controller.backend.set_stream_mute(int(stream_id), bool(mute))

    # ---------- Presets ----------

    def _presets(self) -> list[dict]:
        return [
            {"id": pid, "name": p.get("name", pid), "devices": p.get("devices", [])}
            for pid, p in self._controller.config.presets.items()
        ]

    @call
    def select_preset(self, preset_id: str) -> dict:
        """Load a preset's device set."""
        preset = self._controller.config.presets.get(preset_id)
        if not preset:
            raise BackendError(f"No preset named '{preset_id}'.")

        wanted = preset.get("devices", [])
        log.info("Loading preset '%s': %s", preset.get("name"), wanted)
        self._controller.config.last_preset = preset_id

        with self._controller._lock:
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

        preset_id = self._controller.config.save_preset(name, device_ids)
        self._controller.config.last_preset = preset_id
        return {"presets": self._presets(), "preset": preset_id}

    @call
    def delete_preset(self, preset_id: str) -> dict:
        self._controller.config.delete_preset(preset_id)
        if self._controller.config.last_preset == preset_id:
            self._controller.config.last_preset = None
        return {"presets": self._presets(), "preset": self._controller.config.last_preset}

    # ---------- Recovery ----------

    @call
    def shutdown(self) -> None:
        import webview
        for window in webview.windows:
            window.destroy()
