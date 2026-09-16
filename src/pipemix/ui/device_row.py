"""
DeviceRow — one card in the device list.

A QFrame styled as a floating card containing:
  - Device type icon (system theme)
  - Device name + subtitle (kind + connection status)
  - Battery label (Bluetooth only)
  - Toggle checkbox (styled as an on/off switch)
  - Per-device volume slider

Callbacks:
  on_toggle(device: AudioDevice, active: bool)
  on_volume(device: AudioDevice, volume: int)

The set_active() method sets the toggle without firing the callback
(equivalent to handler_block/unblock in the old GTK code).
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
)

from pipemix.models import AudioDevice, DeviceKind

log = logging.getLogger(__name__)

# Map device kinds to XDG/system-theme icon names
_ICONS: dict[DeviceKind, str] = {
    DeviceKind.BLUETOOTH: "audio-headphones",
    DeviceKind.HDMI:      "video-display",
    DeviceKind.USB:       "audio-card",
    DeviceKind.BUILTIN:   "audio-speakers",
}


class DeviceRow(QFrame):
    """A floating card for one audio output device."""

    def __init__(
        self,
        device: AudioDevice,
        on_toggle: callable,
        on_volume: callable,
    ) -> None:
        super().__init__()
        self.device = device
        self._on_toggle_cb = on_toggle
        self._on_volume_cb = on_volume

        self.setObjectName("deviceRow")
        if not device.connected:
            self.setProperty("offline", True)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 10, 16, 10)
        root.setSpacing(6)

        # ── Top row: icon / name+subtitle / battery / toggle ────────────────
        top = QHBoxLayout()
        top.setSpacing(12)

        # Icon
        icon_name = _ICONS.get(device.kind, "audio-speakers")
        icon_lbl = QLabel()
        icon_lbl.setPixmap(
            QIcon.fromTheme(icon_name, QIcon.fromTheme("audio-speakers"))
            .pixmap(28, 28)
        )
        icon_lbl.setFixedSize(28, 28)
        top.addWidget(icon_lbl)

        # Name + subtitle
        text_col = QVBoxLayout()
        text_col.setSpacing(2)

        name_lbl = QLabel(device.name)
        name_lbl.setObjectName("deviceName")

        status = "Connected" if device.connected else "Disconnected"
        sub_lbl = QLabel(f"{device.kind.value.capitalize()} • {status}")
        sub_lbl.setObjectName("deviceSubtitle")

        text_col.addWidget(name_lbl)
        text_col.addWidget(sub_lbl)
        top.addLayout(text_col, stretch=1)

        # Battery (Bluetooth + connected only)
        self._battery_lbl = QLabel()
        self._battery_lbl.setObjectName("deviceBattery")
        has_battery = (
            device.kind == DeviceKind.BLUETOOTH
            and device.battery is not None
            and device.connected
        )
        if has_battery:
            self._battery_lbl.setText(f"{device.battery}% 🔋")
        self._battery_lbl.setVisible(has_battery)
        top.addWidget(self._battery_lbl)

        # Toggle checkbox (styled as a switch via QSS)
        self.toggle = QCheckBox()
        self.toggle.setObjectName("deviceToggle")
        self.toggle.setEnabled(device.connected)
        self.toggle.stateChanged.connect(self._on_toggle)
        top.addWidget(self.toggle)

        root.addLayout(top)

        # ── Bottom row: volume slider ─────────────────────────────────────
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        bottom.setContentsMargins(36, 0, 0, 0)  # indent to align under name

        vol_icon = QLabel()
        vol_icon.setPixmap(
            QIcon.fromTheme("audio-volume-medium").pixmap(18, 18)
        )
        bottom.addWidget(vol_icon)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(device.volume)
        self.slider.setEnabled(False)   # enabled only when toggle is on
        self.slider.valueChanged.connect(self._on_slider)
        bottom.addWidget(self.slider)

        root.addLayout(bottom)

    # ── Callbacks ──────────────────────────────────────────────────────────

    def _on_toggle(self, state: int) -> None:
        active = state == Qt.CheckState.Checked.value
        self.slider.setEnabled(self.device.connected and active)
        if self._on_toggle_cb:
            self._on_toggle_cb(self.device, active)

    def _on_slider(self, value: int) -> None:
        if self._on_volume_cb:
            self._on_volume_cb(self.device, value)

    # ── Public API (called by MainWindow) ──────────────────────────────────

    def set_active(self, active: bool) -> None:
        """Set the toggle without firing the toggle callback."""
        self.toggle.blockSignals(True)
        try:
            self.toggle.setChecked(active)
            self.slider.setEnabled(self.device.connected and active)
        finally:
            self.toggle.blockSignals(False)
