"""One row in the device list: icon, name, battery, on/off switch, volume."""

from __future__ import annotations

import logging
from gi.repository import Gtk

from pipemix.models import AudioDevice, DeviceKind

log = logging.getLogger(__name__)

ICONS = {
    DeviceKind.BLUETOOTH: "audio-headphones",
    DeviceKind.HDMI:      "video-display",
    DeviceKind.USB:       "audio-card",
}


class DeviceRow(Gtk.ListBoxRow):

    def __init__(self, device: AudioDevice, on_toggle: callable, on_volume: callable) -> None:
        super().__init__()
        self.device = device
        self.on_toggle = on_toggle
        self.on_volume = on_volume

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        root.set_margin_top(8)
        root.set_margin_bottom(8)
        root.set_margin_start(16)
        root.set_margin_end(16)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        icon = Gtk.Image.new_from_icon_name(ICONS.get(device.kind, "audio-speakers"))
        icon.set_icon_size(Gtk.IconSize.LARGE)
        top.append(icon)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        status = "Connected" if device.connected else "Disconnected"
        for label, css in ((device.name, "device-row-title"),
                           (f"{device.kind.value.capitalize()} • {status}", "device-row-subtitle")):
            widget = Gtk.Label(label=label)
            widget.set_halign(Gtk.Align.START)
            widget.add_css_class(css)
            text.append(widget)
        top.append(text)

        has_battery = device.kind == DeviceKind.BLUETOOTH and device.battery is not None
        battery = Gtk.Label(label=f"{device.battery}% 🔋" if has_battery else "")
        battery.set_margin_end(8)
        battery.set_visible(has_battery and device.connected)
        battery.add_css_class("device-row-battery")
        top.append(battery)

        self.switch = Gtk.Switch()
        self.switch.set_valign(Gtk.Align.CENTER)
        self.switch.set_sensitive(device.connected)
        self._handler = self.switch.connect("state-set", self._on_switch)
        top.append(self.switch)
        root.append(top)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bottom.set_margin_start(40)  # line up under the name
        bottom.set_margin_end(8)
        bottom.set_margin_bottom(4)
        bottom.append(Gtk.Image.new_from_icon_name("audio-volume-medium"))

        self.slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.slider.set_hexpand(True)
        self.slider.set_draw_value(False)
        self.slider.set_value(device.volume)
        self.slider.set_sensitive(False)
        self.slider.connect("value-changed", self._on_slider)
        bottom.append(self.slider)
        root.append(bottom)

        self.set_child(root)
        self.add_css_class("device-row")
        if not device.connected:
            self.add_css_class("device-row-offline")

    def _on_switch(self, _widget: Gtk.Switch, state: bool) -> bool:
        self.slider.set_sensitive(self.device.connected and state)
        if self.on_toggle:
            self.on_toggle(self.device, state)
        return False  # let the switch animate normally

    def _on_slider(self, scale: Gtk.Scale) -> None:
        if self.on_volume:
            self.on_volume(self.device, int(scale.get_value()))

    def set_active(self, active: bool) -> None:
        """Set the switch without firing the toggle callback."""
        self.switch.handler_block(self._handler)
        try:
            self.switch.set_active(active)
            self.slider.set_sensitive(self.device.connected and active)
        finally:
            self.switch.handler_unblock(self._handler)
