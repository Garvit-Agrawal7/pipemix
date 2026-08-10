"""
PipeMix — DeviceRow Widget

A custom GTK4 ListBoxRow representing a single physical or Bluetooth audio output.
Features:
  - Type-specific icon (headset, speaker, etc.)
  - Device display name and stable ID/MAC subtitle.
  - Battery indicator (if supported and available).
  - Modern toggle switch.
  - Capability-aware styling (disables elements if device is offline).
"""

from __future__ import annotations

import logging
from gi.repository import Gtk

from pipemix.models import AudioDevice, DeviceKind

log = logging.getLogger(__name__)

class DeviceRow(Gtk.ListBoxRow):
    """
    A custom Gtk.ListBoxRow displaying device details, type icon,
    and a toggle switch. Emulates Libadwaita ActionRow styling.
    """

    def __init__(self, device: AudioDevice, on_toggled: callable, on_volume_changed: callable) -> None:
        super().__init__()
        self.device = device
        self.on_toggled_callback = on_toggled
        self.on_volume_changed_callback = on_volume_changed

        # Root vertical container
        root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        root_box.set_margin_top(8)
        root_box.set_margin_bottom(8)
        root_box.set_margin_start(16)
        root_box.set_margin_end(16)

        # Top row: Details and Toggle (Horizontal)
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        # 1. Device Type Icon
        icon_name = self._get_icon_name(device.kind)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_icon_size(Gtk.IconSize.LARGE)
        row_box.append(icon)

        # 2. Text Details Box (Title + Subtitle)
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)

        title_label = Gtk.Label(label=device.display_name)
        title_label.set_halign(Gtk.Align.START)
        title_label.add_css_class("device-row-title")

        # Subtitle containing kind/ID and connection status
        status_text = "Connected" if device.is_connected else "Disconnected"
        sub_text = f"{device.kind.value.capitalize()} • {status_text}"
        subtitle_label = Gtk.Label(label=sub_text)
        subtitle_label.set_halign(Gtk.Align.START)
        subtitle_label.add_css_class("device-row-subtitle")

        text_box.append(title_label)
        text_box.append(subtitle_label)
        row_box.append(text_box)

        # 3. Optional Battery Label
        self.battery_label = Gtk.Label()
        self.battery_label.set_margin_end(8)
        self.battery_label.add_css_class("device-row-battery")
        self.update_battery()
        row_box.append(self.battery_label)

        # 4. Toggle Switch
        self.switch = Gtk.Switch()
        self.switch.set_active(False)
        self.switch.set_valign(Gtk.Align.CENTER)
        
        # Disable switch if device is disconnected
        self.switch.set_sensitive(device.is_connected)
        
        # Connect change signal and save handler ID to allow blocking
        self._handler_id = self.switch.connect("state-set", self._on_switch_state_set)
        row_box.append(self.switch)
        root_box.append(row_box)

        # Bottom row: Volume slider (Horizontal)
        self.volume_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.volume_box.set_margin_start(40) # Align under details
        self.volume_box.set_margin_end(8)
        self.volume_box.set_margin_bottom(4)

        vol_icon = Gtk.Image.new_from_icon_name("audio-volume-medium")
        self.volume_box.append(vol_icon)

        # Scale slider
        self.volume_slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.volume_slider.set_hexpand(True)
        self.volume_slider.set_draw_value(False)
        self.volume_slider.set_value(device.volume)
        
        # Set sensitivity: only sensitive if connected and toggle switch is ON
        self.volume_slider.set_sensitive(device.is_connected and self.switch.get_active())
        
        # Connect volume changed event and track handler
        self._vol_handler_id = self.volume_slider.connect("value-changed", self._on_volume_slider_changed)
        self.volume_box.append(self.volume_slider)
        root_box.append(self.volume_box)

        self.set_child(root_box)
        self.add_css_class("device-row")

        # Apply offline styling if disconnected
        if not device.is_connected:
            self.add_css_class("device-row-offline")

    def _get_icon_name(self, kind: DeviceKind) -> str:
        """Map DeviceKind to standard Linux desktop system icons."""
        if kind == DeviceKind.BLUETOOTH:
            return "audio-headphones"  # Standard BT headset icon
        if kind == DeviceKind.HDMI:
            return "video-display"     # Monitor icon
        if kind == DeviceKind.USB:
            return "audio-card"        # USB Soundcard icon
        return "audio-speakers"        # Built-in speakers icon

    def update_battery(self) -> None:
        """Update the visible battery percentage label."""
        if (self.device.capabilities.supports_battery and 
                self.device.is_connected and 
                self.device.battery_level is not None):
            self.battery_label.set_text(f"{self.device.battery_level}% 🔋")
            self.battery_label.set_visible(True)
        else:
            self.battery_label.set_visible(False)

    def _on_switch_state_set(self, widget: Gtk.Switch, state: bool) -> bool:
        """Fires when the toggle switch state is changed."""
        log.debug("Device %s switch toggled to %s", self.device.device_id, state)
        self.volume_slider.set_sensitive(self.device.is_connected and state)
        if self.on_toggled_callback:
            self.on_toggled_callback(self.device, state)
        return False  # Let the Gtk.Switch transition normally

    def _on_volume_slider_changed(self, scale: Gtk.Scale) -> None:
        """Fires when volume slider is adjusted."""
        val = int(scale.get_value())
        log.debug("Device %s volume slider adjusted to %d", self.device.device_id, val)
        if self.on_volume_changed_callback:
            self.on_volume_changed_callback(self.device, val)

    def set_active(self, active: bool) -> None:
        """Set switch state programmatically without triggering the callback."""
        if self._handler_id:
            self.switch.handler_block(self._handler_id)
        try:
            self.switch.set_active(active)
            self.volume_slider.set_sensitive(self.device.is_connected and active)
        finally:
            if self._handler_id:
                self.switch.handler_unblock(self._handler_id)

    def set_volume(self, volume: int) -> None:
        """Set volume slider value programmatically without triggering the callback."""
        if hasattr(self, "_vol_handler_id") and self._vol_handler_id:
            self.volume_slider.handler_block(self._vol_handler_id)
        try:
            self.volume_slider.set_value(volume)
        finally:
            if hasattr(self, "_vol_handler_id") and self._vol_handler_id:
                self.volume_slider.handler_unblock(self._vol_handler_id)
