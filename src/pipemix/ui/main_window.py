"""
PipeMix — MainWindow (PyQt6)

Follows the same "dumb UI" principle as the GTK version:
  - Only displays state pushed from the ControllerAdapter via Qt signals.
  - Sends user actions to the Controller via the adapter's proxy methods.
  - Contains no business logic, no subprocess calls, no direct audio controls.
  - Uses QSS (Qt Style Sheets) for a consistent dark-mode aesthetic that is
    identical on every desktop environment (forced Fusion style in app.py).
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from pipemix.models import AudioDevice, SessionState
from pipemix.ui.controller_adapter import ControllerAdapter
from pipemix.ui.device_row import DeviceRow
from pipemix.services.backend import BackendHealth, BackendStatus

log = logging.getLogger(__name__)

# ── Catppuccin Mocha palette (same as the GTK version) ────────────────────────
QSS = """
QMainWindow, QWidget#root {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: 'Outfit', 'Inter', 'Segoe UI', sans-serif;
    font-size: 13px;
}

/* ── Sidebar ─────────────────────────────────────────────────────────────── */
QListWidget#sidebar {
    background-color: #181825;
    border-right: 1px solid #313244;
    border-top: none;
    border-bottom: none;
    border-left: none;
    outline: none;
    padding-top: 12px;
}
QListWidget#sidebar::item {
    color: #a6adc8;
    font-weight: 600;
    padding: 12px 18px;
    border-radius: 12px;
    margin: 4px 12px;
}
QListWidget#sidebar::item:hover {
    background-color: #313244;
    color: #cdd6f4;
}
QListWidget#sidebar::item:selected {
    background-color: #313244;
    color: #89b4fa;
}

/* ── Content area ─────────────────────────────────────────────────────────── */
QWidget#contentArea {
    background-color: #1e1e2e;
    padding: 0px;
}
QScrollArea {
    border: none;
    background-color: transparent;
}
QScrollArea > QWidget > QWidget {
    background-color: transparent;
}

/* ── Tab title / subtitle ─────────────────────────────────────────────────── */
QLabel#tabTitle {
    font-size: 22px;
    font-weight: 800;
    color: #cdd6f4;
}
QLabel#tabSubtitle {
    font-size: 12px;
    color: #a6adc8;
    margin-bottom: 12px;
}

/* ── Device row card ──────────────────────────────────────────────────────── */
QFrame#deviceRow {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 16px;
    margin: 0px 4px 12px 4px;
}
QFrame#deviceRow:hover {
    background-color: #252636;
    border-color: #45475a;
}
QFrame#deviceRow[offline="true"] {
    opacity: 0.45;
}
QLabel#deviceName {
    font-weight: 600;
    font-size: 14px;
    color: #cdd6f4;
}
QLabel#deviceSubtitle {
    font-size: 11px;
    color: #bac2de;
}
QLabel#deviceBattery {
    font-size: 11px;
    font-weight: bold;
    color: #a6e3a1;
}

/* ── Toggle (QCheckBox styled as a switch-like button) ───────────────────── */
QCheckBox#deviceToggle {
    spacing: 0px;
}
QCheckBox#deviceToggle::indicator {
    width: 38px;
    height: 22px;
    border-radius: 11px;
    background-color: #45475a;
    border: none;
}
QCheckBox#deviceToggle::indicator:checked {
    background-color: #89b4fa;
}
QCheckBox#deviceToggle::indicator:disabled {
    background-color: #313244;
}

/* ── Volume slider ────────────────────────────────────────────────────────── */
QSlider::groove:horizontal {
    height: 6px;
    background-color: #313244;
    border-radius: 3px;
}
QSlider::sub-page:horizontal {
    background-color: #89b4fa;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    width: 14px;
    height: 14px;
    background-color: #cdd6f4;
    border-radius: 7px;
    margin: -4px 0;
    border: 1px solid #11111b;
}
QSlider::handle:horizontal:hover {
    background-color: #89b4fa;
}
QSlider::handle:horizontal:disabled {
    background-color: #45475a;
}

/* ── Presets panel ────────────────────────────────────────────────────────── */
QFrame#presetsPanel {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 16px;
    padding: 8px;
    margin-bottom: 12px;
}
QLabel#presetsLabel {
    font-weight: 600;
    color: #cdd6f4;
}
QComboBox {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 10px;
    padding: 6px 12px;
    font-weight: 500;
    min-width: 160px;
}
QComboBox:hover {
    background-color: #45475a;
}
QComboBox::drop-down {
    border: none;
    width: 20px;
}
QComboBox QAbstractItemView {
    background-color: #313244;
    color: #cdd6f4;
    selection-background-color: #45475a;
    border: 1px solid #45475a;
    border-radius: 10px;
}

/* ── Status panel ─────────────────────────────────────────────────────────── */
QFrame#statusPanel {
    border-radius: 24px;
    padding: 4px 18px;
    margin-top: 12px;
}
QFrame#statusPanel[state="idle"] {
    background-color: rgba(69, 71, 90, 51);
}
QFrame#statusPanel[state="active"] {
    background-color: rgba(137, 180, 250, 38);
    border: 1px solid rgba(137, 180, 250, 76);
}
QFrame#statusPanel[state="repairing"] {
    background-color: rgba(249, 226, 175, 38);
    border: 1px solid rgba(249, 226, 175, 76);
}
QFrame#statusPanel[state="error"] {
    background-color: rgba(212, 125, 133, 20);
    border: 1px solid rgba(212, 125, 133, 38);
}
QLabel#statusLabel {
    font-weight: 700;
    letter-spacing: 0.2px;
    padding: 8px 0px;
}
QLabel#statusLabel[state="idle"]     { color: #bac2de; }
QLabel#statusLabel[state="active"]   { color: #89b4fa; }
QLabel#statusLabel[state="repairing"]{ color: #f9e2af; }
QLabel#statusLabel[state="error"]    { color: #d47d85; }

/* ── Buttons ──────────────────────────────────────────────────────────────── */
QPushButton#btnShare {
    background-color: #fab387;
    color: #11111b;
    border: none;
    border-radius: 14px;
    font-weight: 700;
    font-size: 14px;
    padding: 12px 24px;
    margin-top: 12px;
}
QPushButton#btnShare:hover {
    background-color: #f9e2af;
}
QPushButton#btnShare:pressed {
    background-color: #fab387;
}
QPushButton#btnShare[active="true"] {
    background-color: #89b4fa;
    color: #11111b;
}
QPushButton#btnShare[active="true"]:hover {
    background-color: #b4befe;
}

QPushButton#btnReset {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 14px;
    padding: 12px 20px;
    margin-top: 12px;
    font-weight: 600;
}
QPushButton#btnReset:hover {
    background-color: #45475a;
}

QPushButton#btnSavePreset, QPushButton#btnDeletePreset {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 10px;
    padding: 6px 16px;
    font-weight: 600;
}
QPushButton#btnSavePreset:hover {
    background-color: #45475a;
}
QPushButton#btnDeletePreset:hover {
    background-color: rgba(212, 125, 133, 20);
    color: #d47d85;
    border-color: #d47d85;
}

QPushButton#btnShutdown {
    background-color: transparent;
    color: #a6adc8;
    border: none;
    border-radius: 18px;
    padding: 6px;
    font-size: 16px;
}
QPushButton#btnShutdown:hover {
    background-color: rgba(212, 125, 133, 20);
    color: #d47d85;
}

/* ── App stream rows (Split tab) ─────────────────────────────────────────── */
QFrame#appRow {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 16px;
    margin: 0px 4px 12px 4px;
    padding: 12px;
}
QFrame#appRow:hover {
    background-color: #252636;
    border-color: #45475a;
}
QLabel#appName {
    font-weight: bold;
    font-size: 14px;
    color: #cdd6f4;
}
QLabel#appStreamId {
    font-size: 10px;
    color: #a6adc8;
}

/* ── Master volume area ──────────────────────────────────────────────────── */
QLabel#volLabel {
    color: #cdd6f4;
    font-weight: 500;
}

/* ── Dialogs ─────────────────────────────────────────────────────────────── */
QDialog, QMessageBox {
    background-color: #1e1e2e;
    color: #cdd6f4;
}
QLineEdit {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 8px;
    padding: 8px 12px;
}
QDialogButtonBox QPushButton {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 8px;
    padding: 6px 18px;
    font-weight: 600;
}
QDialogButtonBox QPushButton:hover {
    background-color: #45475a;
}
"""


def _set_qss_property(widget: QWidget, prop: str, value: str) -> None:
    """Set a dynamic QSS property and force a style refresh."""
    widget.setProperty(prop, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class MainWindow(QMainWindow):
    def __init__(self, adapter: ControllerAdapter) -> None:
        super().__init__()
        self.adapter = adapter

        self.setWindowTitle("PipeMix")
        self.setMinimumSize(820, 580)
        self.resize(880, 620)

        # Track which devices are toggled on
        self.selected: dict[str, bool] = {}
        self._split_sig: tuple | None = None

        # Apply global stylesheet
        self.setStyleSheet(QSS)

        # Window icon
        self._set_window_icon()

        # Root layout
        root_widget = QWidget()
        root_widget.setObjectName("root")
        self.setCentralWidget(root_widget)
        root_layout = QHBoxLayout(root_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Sidebar
        self._build_sidebar(root_layout)

        # Stacked pages
        self.stack = QStackedWidget()
        self.stack.setObjectName("contentArea")
        root_layout.addWidget(self.stack, stretch=1)

        self._build_combine_page()
        self._build_split_page()

        # Connect adapter signals
        self.adapter.devices_changed.connect(self._on_devices)
        self.adapter.state_changed.connect(self._on_state)
        self.adapter.health_changed.connect(self._on_health)

        # Split-tab auto-refresh (every 3 s, same as the GTK version)
        self._split_timer = QTimer(self)
        self._split_timer.setInterval(3000)
        self._split_timer.timeout.connect(self._maybe_refresh_split)
        self._split_timer.start()

        # Start on Combine tab
        self.sidebar.setCurrentRow(0)

        # Load presets
        self._refresh_presets(select_id=self.adapter.last_preset)

    # ── Window icon ────────────────────────────────────────────────────────

    def _set_window_icon(self) -> None:
        local = Path(__file__).parent.parent.parent / "data" / "icons" / "pipemix.png"
        installed = Path("/usr/share/icons/hicolor/512x512/apps/pipemix.png")
        for path in (local, installed):
            if path.exists():
                self.setWindowIcon(QIcon(str(path)))
                return
        self.setWindowIcon(QIcon.fromTheme("audio-card"))

    # ── Sidebar ────────────────────────────────────────────────────────────

    def _build_sidebar(self, parent_layout: QHBoxLayout) -> None:
        self.sidebar = QListWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(210)
        self.sidebar.setSpacing(2)

        for label, icon_name in (
            ("Combine Sinks", "audio-speakers"),
            ("Split Audio",   "audio-card"),
        ):
            item = QListWidgetItem(
                QIcon.fromTheme(icon_name, QIcon.fromTheme("audio-card")),
                f"  {label}",
            )
            item.setSizeHint(item.sizeHint().__class__(210, 48))
            self.sidebar.addItem(item)

        self.sidebar.currentRowChanged.connect(self._on_sidebar)
        parent_layout.addWidget(self.sidebar)

    def _on_sidebar(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        if index == 1:
            self._refresh_split()

    # ── Combine page ───────────────────────────────────────────────────────

    def _build_combine_page(self) -> None:
        page = QWidget()
        page.setObjectName("contentArea")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(0)

        # Title
        title = QLabel("Combine Sinks")
        title.setObjectName("tabTitle")
        layout.addWidget(title)

        subtitle = QLabel("Select multiple devices to play audio through them simultaneously.")
        subtitle.setObjectName("tabSubtitle")
        layout.addWidget(subtitle)
        layout.addSpacing(8)

        # Presets panel
        presets_frame = QFrame()
        presets_frame.setObjectName("presetsPanel")
        presets_layout = QHBoxLayout(presets_frame)
        presets_layout.setContentsMargins(12, 8, 12, 8)
        presets_layout.setSpacing(10)

        presets_lbl = QLabel("Presets:")
        presets_lbl.setObjectName("presetsLabel")
        presets_layout.addWidget(presets_lbl)

        self.preset_combo = QComboBox()
        self.preset_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        presets_layout.addWidget(self.preset_combo, stretch=1)

        self.btn_save = QPushButton("Add Preset")
        self.btn_save.setObjectName("btnSavePreset")
        self.btn_save.setToolTip("Save current selection as preset")
        self.btn_save.clicked.connect(self._on_save_preset)
        presets_layout.addWidget(self.btn_save)

        self.btn_delete = QPushButton("Delete")
        self.btn_delete.setObjectName("btnDeletePreset")
        self.btn_delete.setToolTip("Delete selected preset")
        self.btn_delete.clicked.connect(self._on_delete_preset)
        presets_layout.addWidget(self.btn_delete)

        layout.addWidget(presets_frame)
        layout.addSpacing(8)

        # Device list (scrollable)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(280)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._device_list_widget = QWidget()
        self._device_list_widget.setObjectName("contentArea")
        self._device_list_layout = QVBoxLayout(self._device_list_widget)
        self._device_list_layout.setContentsMargins(0, 0, 0, 0)
        self._device_list_layout.setSpacing(0)
        self._device_list_layout.addStretch()

        scroll.setWidget(self._device_list_widget)
        layout.addWidget(scroll)

        # Status panel
        self._status_frame = QFrame()
        self._status_frame.setObjectName("statusPanel")
        status_layout = QHBoxLayout(self._status_frame)
        status_layout.setContentsMargins(18, 0, 18, 0)

        self._status_label = QLabel("Idle — Ready to share")
        self._status_label.setObjectName("statusLabel")
        status_layout.addWidget(self._status_label)

        _set_qss_property(self._status_frame, "state", "idle")
        _set_qss_property(self._status_label, "state", "idle")
        layout.addWidget(self._status_frame)

        # Master volume
        vol_row = QHBoxLayout()
        vol_row.setSpacing(10)
        vol_row.setContentsMargins(0, 16, 0, 0)

        vol_icon = QLabel()
        vol_icon.setPixmap(QIcon.fromTheme("audio-volume-high").pixmap(20, 20))
        vol_row.addWidget(vol_icon)

        vol_lbl = QLabel("Master Volume")
        vol_lbl.setObjectName("volLabel")
        vol_row.addWidget(vol_lbl)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(50)
        self.volume_slider.valueChanged.connect(self._on_master_volume)
        vol_row.addWidget(self.volume_slider, stretch=1)

        layout.addLayout(vol_row)

        # Action buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(16)
        btn_row.setContentsMargins(0, 0, 0, 0)

        self.btn_share = QPushButton("Start Sharing")
        self.btn_share.setObjectName("btnShare")
        self.btn_share.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.btn_share.clicked.connect(self._on_share)
        btn_row.addWidget(self.btn_share)

        self.btn_reset = QPushButton("Reset Audio")
        self.btn_reset.setObjectName("btnReset")
        self.btn_reset.clicked.connect(self._on_reset)
        btn_row.addWidget(self.btn_reset)

        layout.addLayout(btn_row)
        layout.addStretch()

        self.stack.addWidget(page)

    # ── Split page ─────────────────────────────────────────────────────────

    def _build_split_page(self) -> None:
        page = QWidget()
        page.setObjectName("contentArea")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(0)

        title = QLabel("Split Audio")
        title.setObjectName("tabTitle")
        layout.addWidget(title)

        subtitle = QLabel("Route individual application tabs or profiles to separate output devices.")
        subtitle.setObjectName("tabSubtitle")
        layout.addWidget(subtitle)
        layout.addSpacing(8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._stream_list_widget = QWidget()
        self._stream_list_widget.setObjectName("contentArea")
        self._stream_list_layout = QVBoxLayout(self._stream_list_widget)
        self._stream_list_layout.setContentsMargins(0, 0, 0, 0)
        self._stream_list_layout.setSpacing(0)
        self._stream_list_layout.addStretch()

        scroll.setWidget(self._stream_list_widget)
        layout.addWidget(scroll, stretch=1)

        self.stack.addWidget(page)

    # ── Split audio refresh ─────────────────────────────────────────────────

    def _maybe_refresh_split(self) -> None:
        if self.stack.currentIndex() == 1:
            self._refresh_split()

    def _refresh_split(self) -> None:
        """Rebuild the per-application routing list only when something changed."""
        try:
            streams = self.adapter.backend.list_streams()
            error = None
        except Exception as e:
            log.error("Failed to list audio streams: %s", e)
            streams, error = [], "Could not query audio streams."

        devices = [
            d for d in self.adapter.devices.values()
            if d.connected and d.sink
        ]

        signature = (error, [tuple(s.values()) for s in streams], [d.sink for d in devices])
        if signature == self._split_sig:
            return
        self._split_sig = signature

        self._clear_layout(self._stream_list_layout)

        if not streams:
            self._show_stream_message(error or "No applications are currently playing audio.")
            return

        for stream in streams:
            self._stream_list_layout.insertWidget(
                self._stream_list_layout.count() - 1,
                self._build_stream_row(stream, devices),
            )

    def _build_stream_row(self, stream: dict, devices: list[AudioDevice]) -> QFrame:
        """
        One application stream row:
          - Icon + name + stream ID
          - Per-device checkboxes (one per connected output)
          - "All" button to tick every device at once
        """
        frame = QFrame()
        frame.setObjectName("appRow")
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(8)

        # ── Top: icon + stream name + id ──────────────────────────────────
        top = QHBoxLayout()
        top.setSpacing(12)

        icon_lbl = QLabel()
        icon_lbl.setPixmap(QIcon.fromTheme("audio-speakers").pixmap(28, 28))
        top.addWidget(icon_lbl)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        name_lbl = QLabel(stream["name"])
        name_lbl.setObjectName("appName")
        id_lbl = QLabel(f"Stream ID: {stream['id']}")
        id_lbl.setObjectName("appStreamId")
        text_col.addWidget(name_lbl)
        text_col.addWidget(id_lbl)
        top.addLayout(text_col, stretch=1)

        # Mute button (top-right)
        mute_btn = QPushButton()
        mute_btn.setCheckable(True)
        mute_btn.setChecked(stream["mute"])
        mute_btn.setIcon(
            QIcon.fromTheme("audio-volume-muted" if stream["mute"] else "audio-volume-high")
        )
        mute_btn.setToolTip("Toggle mute")
        mute_btn.setObjectName("btnReset")
        mute_btn.toggled.connect(
            lambda muted, sid=stream["id"], btn=mute_btn: self._on_mute(muted, sid, btn)
        )
        top.addWidget(mute_btn)
        outer.addLayout(top)

        # ── Bottom: per-device checkboxes + All button ────────────────────
        dev_row = QHBoxLayout()
        dev_row.setSpacing(10)
        dev_row.setContentsMargins(40, 0, 0, 0)  # indent under stream name

        checkboxes: list[tuple[QCheckBox, AudioDevice]] = []

        def _apply_selection():
            """Collect checked devices and route the stream."""
            checked = [dev for cb, dev in checkboxes if cb.isChecked()]
            try:
                self.adapter.route_stream_to_many(stream["id"], checked)
            except Exception as e:
                log.error("Failed to route stream %d: %s", stream["id"], e)

        def _on_all_clicked():
            """Check all device boxes, then route."""
            for cb, _ in checkboxes:
                cb.blockSignals(True)
                cb.setChecked(True)
                cb.blockSignals(False)
            _apply_selection()

        for dev in devices:
            cb = QCheckBox(dev.name)
            cb.setStyleSheet("color: #cdd6f4; font-size: 12px;")
            # Pre-select the device if the stream is already routed to its sink
            if stream.get("sink") == dev.sink:
                cb.setChecked(True)
            cb.stateChanged.connect(lambda _state, _fn=_apply_selection: _fn())
            checkboxes.append((cb, dev))
            dev_row.addWidget(cb)

        if devices:
            all_btn = QPushButton("All")
            all_btn.setObjectName("btnSavePreset")
            all_btn.setToolTip("Route to all connected outputs")
            all_btn.setFixedWidth(48)
            all_btn.clicked.connect(_on_all_clicked)
            dev_row.addWidget(all_btn)

        dev_row.addStretch()
        outer.addLayout(dev_row)

        if not devices:
            no_dev = QLabel("No connected outputs available")
            no_dev.setStyleSheet("color: #a6adc8; font-size: 11px;")
            outer.addWidget(no_dev)

        return frame

    def _show_stream_message(self, message: str) -> None:
        lbl = QLabel(message)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("color: #a6adc8; padding: 48px;")
        self._stream_list_layout.insertWidget(
            self._stream_list_layout.count() - 1, lbl
        )

    # ── User action handlers ────────────────────────────────────────────────

    def _on_mute(self, muted: bool, stream_id: int, btn: QPushButton) -> None:
        btn.setIcon(
            QIcon.fromTheme("audio-volume-muted" if muted else "audio-volume-high")
        )
        try:
            self.adapter.backend.set_stream_mute(stream_id, muted)
        except Exception as e:
            log.error("Failed to mute stream %d: %s", stream_id, e)


    def _on_device_toggle(self, device: AudioDevice, active: bool) -> None:
        self.selected[device.id] = active
        self._clear_preset_selection()
        if self.adapter.session.is_active:
            self._apply_selection()

    def _on_device_volume(self, device: AudioDevice, volume: int) -> None:
        try:
            self.adapter.set_device_volume(device.id, volume)
        except Exception as e:
            log.error("Failed to set volume on %s: %s", device.name, e)

    def _on_share(self) -> None:
        if self.adapter.session.is_active:
            try:
                self.adapter.stop_sharing()
            except Exception as e:
                self._error("Failed to Stop Sharing", str(e))
        elif not any(self.selected.values()):
            self._error("No Devices Selected",
                        "Please toggle on at least one device before sharing.")
        else:
            self._apply_selection()

    def _apply_selection(self) -> None:
        devices = [
            self.adapter.devices[i]
            for i, on in self.selected.items()
            if on and i in self.adapter.devices
        ]
        try:
            if devices:
                self.adapter.start_sharing(devices)
            else:
                self.adapter.stop_sharing()
        except Exception as e:
            self._error("Sharing failed", str(e))

    def _on_master_volume(self, value: int) -> None:
        self.adapter.set_master_volume(value)

    def _on_reset(self) -> None:
        self.adapter.reset_audio()

    # ── Presets ─────────────────────────────────────────────────────────────

    def _refresh_presets(self, select_id: str | None = None) -> None:
        self.preset_combo.blockSignals(True)
        try:
            self.preset_combo.clear()
            self.preset_combo.addItem("Select Preset..", userData="none")
            for pid, preset in self.adapter.presets.items():
                self.preset_combo.addItem(preset.get("name", pid), userData=pid)

            if select_id:
                idx = next(
                    (i for i in range(self.preset_combo.count())
                     if self.preset_combo.itemData(i) == select_id),
                    0,
                )
                self.preset_combo.setCurrentIndex(idx)
            else:
                self.preset_combo.setCurrentIndex(0)
        finally:
            self.preset_combo.blockSignals(False)

    def _on_preset_changed(self, _index: int) -> None:
        pid = self.preset_combo.currentData()
        preset = self.adapter.presets.get(pid or "none")
        if not preset:
            return

        wanted = preset.get("devices", [])
        log.info("Loading preset '%s': %s", preset.get("name"), wanted)
        self.adapter.last_preset = pid
        for dev_id in self.selected:
            self.selected[dev_id] = dev_id in wanted

        # Update all device row toggles
        layout = self._device_list_layout
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item and isinstance(item.widget(), DeviceRow):
                row: DeviceRow = item.widget()
                row.set_active(row.device.id in wanted)

        if self.adapter.session.is_active:
            self._apply_selection()

    def _clear_preset_selection(self) -> None:
        """Reset dropdown to placeholder without triggering _on_preset_changed."""
        if self.preset_combo.currentData() == "none":
            return
        self.preset_combo.blockSignals(True)
        try:
            self.preset_combo.setCurrentIndex(0)
        finally:
            self.preset_combo.blockSignals(False)
        self.adapter.last_preset = None

    def _on_save_preset(self) -> None:
        selected_ids = [i for i, on in self.selected.items() if on]
        if not selected_ids:
            self._error("No Devices Selected",
                        "Please select at least one device to save as a preset.")
            return

        name, ok = QInputDialog.getText(
            self, "Save Preset", "Enter a name for this preset:",
            QLineEdit.EchoMode.Normal, ""
        )
        if ok and name.strip():
            preset_id = self.adapter.save_preset(name.strip(), selected_ids)
            self._refresh_presets(select_id=preset_id)

    def _on_delete_preset(self) -> None:
        pid = self.preset_combo.currentData()
        if not pid or pid == "none":
            self._error("No Preset Selected",
                        "Please select a preset from the dropdown to delete.")
            return
        self.adapter.delete_preset(pid)
        if self.adapter.last_preset == pid:
            self.adapter.last_preset = None
        self._refresh_presets()

    # ── Controller signal slots ─────────────────────────────────────────────

    def _on_devices(self, devices: list[AudioDevice]) -> None:
        preset = self.adapter.presets.get(
            self.preset_combo.currentData() or "none", {}
        )
        wanted = preset.get("devices", [])

        self._clear_layout(self._device_list_layout)

        for dev in devices:
            if not dev.connected:
                self.selected[dev.id] = False
            self.selected.setdefault(dev.id, dev.id in wanted)

            row = DeviceRow(dev, self._on_device_toggle, self._on_device_volume)
            row.set_active(self.selected[dev.id])
            self._device_list_layout.insertWidget(
                self._device_list_layout.count() - 1, row
            )

    def _on_state(self, state: SessionState) -> None:
        self._update_status(state)

        if state == SessionState.ACTIVE:
            self.btn_share.setText("Stop Sharing")
            _set_qss_property(self.btn_share, "active", "true")
            self.btn_share.setEnabled(True)
            self.btn_reset.setEnabled(True)

            target = self.adapter.active_sink()
            if target:
                try:
                    self.adapter.backend.set_volume(target, self.volume_slider.value())
                except Exception as e:
                    log.warning("Failed to initialize session volume: %s", e)

        elif state in (SessionState.STARTING, SessionState.STOPPING):
            self.btn_share.setEnabled(False)
            self.btn_reset.setEnabled(False)

        else:  # IDLE, ERROR
            self.btn_share.setText("Start Sharing")
            _set_qss_property(self.btn_share, "active", "false")
            self.btn_share.setEnabled(True)
            self.btn_reset.setEnabled(True)

    def _on_health(self, status: BackendStatus) -> None:
        if status.health != BackendHealth.OK:
            self._error(
                "Audio Server Issue Detected",
                f"{status.message}\n\nTechnical details:\n{status.details}",
            )

    def _update_status(self, state: SessionState) -> None:
        mapping = {
            SessionState.ACTIVE:    ("active",    "Sharing Active ✓ — Playing on multiple outputs"),
            SessionState.REPAIRING: ("repairing", "Reconnecting... Waiting for output device(s)"),
            SessionState.STARTING:  ("repairing", "Reconnecting... Waiting for output device(s)"),
            SessionState.ERROR:     ("error",     "Error — Reset Audio recommended"),
        }
        state_str, text = mapping.get(state, ("idle", "Idle — Ready to share"))
        self._status_label.setText(text)
        _set_qss_property(self._status_frame, "state", state_str)
        _set_qss_property(self._status_label, "state", state_str)

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _clear_layout(self, layout: QVBoxLayout) -> None:
        """Remove all widgets from a layout, keeping the trailing stretch."""
        while layout.count() > 1:
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _error(self, title: str, message: str) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(message)
        box.setIcon(QMessageBox.Icon.Critical)
        box.exec()

    def closeEvent(self, event) -> None:
        """Ensure the split-refresh timer is stopped on close."""
        self._split_timer.stop()
        super().closeEvent(event)
