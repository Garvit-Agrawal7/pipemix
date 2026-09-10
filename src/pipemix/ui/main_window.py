"""
PipeMix — MainWindow

The main desktop user interface. Built purely in GTK4.
Follows the "dumb UI" principle:
  - Only displays state pushed from the Controller via signals.
  - Sends user actions (button clicks, toggle switch states) to the Controller.
  - Contains no business logic, no subprocess calls, no direct audio controls.
  - Uses CSS for a premium, dark-mode, custom Libadwaita-like aesthetic.
"""

from __future__ import annotations

import logging
from pathlib import Path

from gi.repository import Gdk, Gtk, GLib

from pipemix.models import AudioDevice, SessionState
from pipemix.ui.device_row import DeviceRow
from pipemix.services.backend import BackendHealth

log = logging.getLogger(__name__)

CSS_STYLES = """
window {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: 'Outfit', 'Inter', sans-serif;
}

headerbar {
    background-color: #181825;
    color: #cdd6f4;
    border-bottom: 1px solid #313244;
    padding: 6px;
}

.sidebar {
    background-color: #181825;
    border-right: 1px solid #313244;
    padding-top: 12px;
}

.sidebar-row {
    padding: 12px 18px;
    border-radius: 12px;
    margin: 4px 12px;
    color: #a6adc8;
    font-weight: 600;
    transition: all 0.2s ease;
}

.sidebar-row:hover {
    background-color: #313244;
    color: #cdd6f4;
}

.sidebar-row:selected {
    background-color: #313244;
    color: #89b4fa;
    font-weight: 700;
}

.main-content {
    padding: 24px;
}

.tab-title {
    font-size: 1.8em;
    font-weight: 800;
    color: #cdd6f4;
    margin-bottom: 4px;
}

.tab-subtitle {
    font-size: 0.95em;
    color: #a6adc8;
    margin-bottom: 24px;
}

.device-list-frame {
    background-color: transparent;
    border: none;
}

.device-list {
    background-color: transparent;
}

/* Floating Card style for Device Rows */
.device-row {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 16px;
    margin: 0px 4px 12px 4px;
    box-shadow: 0 4px 10px rgba(0, 0, 0, 0.15);
    transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}

.device-row:hover {
    background-color: #252636;
    border-color: #45475a;
    box-shadow: 0 6px 16px rgba(0, 0, 0, 0.25);
}

.device-row-title {
    font-weight: 600;
    font-size: 1.1em;
    color: #cdd6f4;
}

.device-row-subtitle {
    font-size: 0.85em;
    color: #bac2de;
}

.device-row-battery {
    font-size: 0.9em;
    font-weight: bold;
    color: #a6e3a1;
}

.device-row-offline {
    opacity: 0.45;
}

/* Audio Volume Scale customized to Royal Blue */
scale trough {
    background-color: #313244;
    border-radius: 8px;
    min-height: 8px;
}
scale highlight {
    background-color: #89b4fa;
    border-radius: 8px;
}
scale slider {
    background-color: #cdd6f4;
    border: 1px solid #11111b;
    border-radius: 50%;
    min-width: 14px;
    min-height: 14px;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.4);
    margin: -3px 0;
    transition: background-color 0.2s;
}
scale slider:hover {
    background-color: #89b4fa;
}

switch:checked {
    background-color: #89b4fa;
}
switch:checked > slider {
    background-color: #eff1f5;
}

/* Presets panel styling */
.presets-panel {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 16px;
    padding: 14px 16px;
    margin-bottom: 20px;
    box-shadow: 0 4px 10px rgba(0, 0, 0, 0.15);
}

.presets-panel button {
    padding: 6px 16px;
    border-radius: 10px;
    font-weight: 600;
}

/* Share Button with Solid Accent Orange/Peach */
.btn-share {
    background-color: #fab387;
    color: #11111b;
    border: none;
    border-radius: 14px;
    font-weight: 700;
    font-size: 1.1em;
    padding: 14px;
    margin-top: 16px;
    box-shadow: none;
    transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}

.btn-share:hover {
    background-color: #f9e2af;
    box-shadow: none;
    transform: translateY(-1px);
}

.btn-share:active {
    background-color: #fab387;
    transform: translateY(0);
}

/* Active Sharing Button - Royal Blue (No Red bleeding) */
.btn-share.active {
    background-color: #89b4fa;
    color: #11111b;
    box-shadow: none;
}

.btn-share.active:hover {
    background-color: #b4befe;
    box-shadow: none;
    transform: translateY(-1px);
}

.btn-share.active:active {
    background-color: #89b4fa;
    transform: translateY(0);
}

.btn-reset {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 14px;
    padding: 14px;
    margin-top: 16px;
    font-weight: 600;
    transition: all 0.2s ease;
}

.btn-reset:hover {
    background-color: #45475a;
}

/* Delete Preset Button with custom Red hover highlight only */
.btn-delete {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 10px;
    transition: all 0.2s ease;
}

.btn-delete:hover {
    background-color: rgba(212, 125, 133, 0.08);
    color: #d47d85;
    border-color: #d47d85;
}

.status-panel {
    border-radius: 24px;
    padding: 10px 18px;
    font-weight: 700;
    margin-top: 16px;
    letter-spacing: 0.2px;
}

.status-idle {
    background-color: rgba(69, 71, 90, 0.2);
    color: #bac2de;
}

.status-active {
    background-color: rgba(137, 180, 250, 0.15);
    color: #89b4fa;
    border: 1px solid rgba(137, 180, 250, 0.3);
}

.status-repairing {
    background-color: rgba(249, 226, 175, 0.15);
    color: #f9e2af;
    border: 1px solid rgba(249, 226, 175, 0.3);
}

.status-error {
    background-color: rgba(212, 125, 133, 0.08);
    color: #d47d85;
    border: 1px solid rgba(212, 125, 133, 0.15);
}

/* Split Routing Tab Styles */
.app-row {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 16px;
    margin: 0px 4px 12px 4px;
    padding: 16px;
    box-shadow: 0 4px 10px rgba(0, 0, 0, 0.15);
    transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}

.app-row:last-child {
    border-bottom: none;
}

.app-row:hover {
    background-color: #252636;
    border-color: #45475a;
    box-shadow: 0 6px 16px rgba(0, 0, 0, 0.25);
}

.app-name {
    font-weight: bold;
    font-size: 1.05em;
    color: #cdd6f4;
}

.app-stream-id {
    font-size: 0.8em;
    color: #a6adc8;
}

.device-dropdown {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 10px;
    padding: 6px 12px;
    font-weight: 500;
}

.device-dropdown:hover {
    background-color: #45475a;
}

/* Shutdown Button Styles (subtext color on idle, red only on hover) */
.btn-shutdown {
    color: #a6adc8;
    background-color: transparent;
    border: none;
    border-radius: 50%;
    padding: 6px;
    transition: all 0.2s ease;
}
.btn-shutdown:hover {
    background-color: rgba(212, 125, 133, 0.08);
    color: #d47d85;
}
"""

class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, controller: Controller) -> None:
        super().__init__(application=app)
        self.controller = controller
        self.set_title("PipeMix")
        self.set_default_size(800, 600)

        self.selected: dict[str, bool] = {}

        self._split_sig: tuple | None = None

        self._apply_css()

        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_box.set_valign(Gtk.Align.CENTER)

        # Try local path first, then installed path, fallback to generic icon
        logo_path = Path(__file__).parent.parent.parent / "data" / "icons" / "pipemix.png"
        if not logo_path.exists():
            logo_path = Path("/usr/share/icons/hicolor/512x512/apps/pipemix.png")

        if logo_path.exists():
            logo_icon = Gtk.Image.new_from_file(str(logo_path))
            logo_icon.set_pixel_size(24)
        else:
            logo_icon = Gtk.Image.new_from_icon_name("audio-card")

        logo_label = Gtk.Label(label="PipeMix")
        logo_label.add_css_class("font-bold")
        title_box.append(logo_icon)
        title_box.append(logo_label)
        header.set_title_widget(title_box)

        self.btn_shutdown = Gtk.Button()
        self.btn_shutdown.set_icon_name("system-shutdown")
        self.btn_shutdown.set_tooltip_text("Shutdown PipeMix completely")
        self.btn_shutdown.add_css_class("btn-shutdown")
        self.btn_shutdown.connect("clicked", self._on_shutdown)
        header.pack_end(self.btn_shutdown)

        self.set_titlebar(header)

        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        self.sidebar = Gtk.ListBox()
        self.sidebar.add_css_class("sidebar")
        self.sidebar.set_size_request(200, -1)
        self.sidebar.connect("row-selected", self._on_sidebar)

        self.row_combine = self._sidebar_row("Combine Sinks", "audio-speakers")
        self.row_split = self._sidebar_row("Split Audio", "audio-card")
        self.sidebar.append(self.row_combine)
        self.sidebar.append(self.row_split)
        main_box.append(self.sidebar)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(250)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        main_box.append(self.stack)

        self._build_combine()
        self._build_split()

        self.set_child(main_box)

        self.controller.connect("devices-changed", self._on_devices)
        self.controller.connect("state-changed", self._on_state)
        self.controller.connect("health-changed", self._on_health)

        self.sidebar.select_row(self.row_combine)

        GLib.timeout_add(3000, self._tick)

    def _apply_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS_STYLES.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _sidebar_row(self, text: str, icon_name: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.add_css_class("sidebar-row")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        label = Gtk.Label(label=text)
        box.append(icon)
        box.append(label)
        row.set_child(box)
        return row

    def _on_sidebar(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if not row:
            return
        if row == self.row_combine:
            self.stack.set_visible_child_name("combine")
        elif row == self.row_split:
            self.stack.set_visible_child_name("split")
            self._refresh_split()

    # ---------- Combine Sinks page ----------

    def _build_combine(self) -> None:
        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page_box.add_css_class("main-content")

        title = Gtk.Label(label="Combine Sinks")
        title.set_halign(Gtk.Align.START)
        title.add_css_class("tab-title")
        page_box.append(title)

        subtitle = Gtk.Label(label="Select multiple devices to play audio through them simultaneously.")
        subtitle.set_halign(Gtk.Align.START)
        subtitle.add_css_class("tab-subtitle")
        page_box.append(subtitle)

        preset_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        preset_box.add_css_class("presets-panel")
        preset_box.set_valign(Gtk.Align.CENTER)

        preset_label = Gtk.Label(label="Presets:")
        preset_label.add_css_class("font-bold")
        preset_box.append(preset_label)

        self.preset_combo = Gtk.ComboBoxText()
        self.preset_combo.set_hexpand(True)
        self.preset_combo.add_css_class("device-dropdown")
        self._preset_handler = self.preset_combo.connect("changed", self._on_preset_changed)
        preset_box.append(self.preset_combo)

        self.btn_save = Gtk.Button(label="Add Preset")
        self.btn_save.set_tooltip_text("Save current selection as preset")
        self.btn_save.connect("clicked", self._on_save_preset)
        preset_box.append(self.btn_save)

        self.btn_delete = Gtk.Button(label="Delete")
        self.btn_delete.set_tooltip_text("Delete selected preset")
        self.btn_delete.add_css_class("btn-delete")
        self.btn_delete.connect("clicked", self._on_delete_preset)
        preset_box.append(self.btn_delete)

        page_box.append(preset_box)

        scroll = Gtk.ScrolledWindow()
        scroll.set_size_request(-1, 260)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add_css_class("device-list-frame")

        self.device_list = Gtk.ListBox()
        self.device_list.add_css_class("device-list")
        self.device_list.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll.set_child(self.device_list)
        page_box.append(scroll)

        self.status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.status_box.add_css_class("status-panel")
        self.status_box.add_css_class("status-idle")
        self.status_label = Gtk.Label(label="Idle — Ready to share")
        self.status_box.append(self.status_label)
        page_box.append(self.status_box)

        vol_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        vol_box.set_margin_top(16)
        vol_box.set_margin_bottom(8)

        vol_icon = Gtk.Image.new_from_icon_name("audio-volume-high")
        vol_label = Gtk.Label(label="Master Volume")

        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.volume.set_hexpand(True)
        self.volume.set_draw_value(True)
        self.volume.set_value(50)
        self.volume.connect("value-changed", self._on_master_volume)

        vol_box.append(vol_icon)
        vol_box.append(vol_label)
        vol_box.append(self.volume)
        page_box.append(vol_box)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)

        self.btn_share = Gtk.Button(label="Start Sharing")
        self.btn_share.set_hexpand(True)
        self.btn_share.add_css_class("btn-share")
        self.btn_share.connect("clicked", self._on_share)
        btn_box.append(self.btn_share)

        self.btn_reset = Gtk.Button(label="Reset Audio")
        self.btn_reset.add_css_class("btn-reset")
        self.btn_reset.connect("clicked", self._on_reset)
        btn_box.append(self.btn_reset)

        page_box.append(btn_box)
        self.stack.add_named(page_box, "combine")

        self._refresh_presets(select_id=self.controller.last_preset)

    # ---------- Split Audio page ----------

    def _build_split(self) -> None:
        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page_box.add_css_class("main-content")

        title = Gtk.Label(label="Split Audio")
        title.set_halign(Gtk.Align.START)
        title.add_css_class("tab-title")
        page_box.append(title)

        subtitle = Gtk.Label(label="Route individual application tabs or profiles to separate output devices.")
        subtitle.set_halign(Gtk.Align.START)
        subtitle.add_css_class("tab-subtitle")
        page_box.append(subtitle)

        scroll = Gtk.ScrolledWindow()
        scroll.set_size_request(-1, 350)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add_css_class("device-list-frame")

        self.stream_list = Gtk.ListBox()
        self.stream_list.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll.set_child(self.stream_list)
        page_box.append(scroll)

        self.stack.add_named(page_box, "split")

    def _refresh_split(self) -> None:
        """Rebuild the per-application routing list, but only when something changed."""
        try:
            streams, error = self.controller.backend.list_streams(), None
        except Exception as e:
            log.error("Failed to list audio streams: %s", e)
            streams, error = [], "Could not query audio streams."

        devices = [
            d for d in self.controller.devices.values()
            if d.connected and d.sink
        ]

        # Redrawing on every tick would fight the user's open dropdowns, so only
        # rebuild when the streams or the available outputs actually differ.
        signature = (error, [tuple(s.values()) for s in streams], [d.sink for d in devices])
        if signature == self._split_sig:
            return
        self._split_sig = signature

        while row := self.stream_list.get_row_at_index(0):
            self.stream_list.remove(row)

        if not streams:
            self._show_message(error or "No applications are currently playing audio.")
            return

        for stream in streams:
            self.stream_list.append(self._stream_row(stream, devices))

    def _stream_row(self, stream: dict, devices: list[AudioDevice]) -> Gtk.ListBoxRow:
        """One application stream: name, target-output dropdown and a mute toggle."""
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)

        icon = Gtk.Image.new_from_icon_name("audio-speakers")
        icon.set_icon_size(Gtk.IconSize.LARGE)
        row_box.append(icon)

        details_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        details_box.set_hexpand(True)
        for text, css in ((stream["name"], "app-name"),
                          (f"Stream ID: {stream['id']}", "app-stream-id")):
            label = Gtk.Label(label=text)
            label.set_halign(Gtk.Align.START)
            label.add_css_class(css)
            details_box.append(label)
        row_box.append(details_box)

        dropdown = Gtk.ComboBoxText()
        dropdown.add_css_class("device-dropdown")
        for dev in devices:
            dropdown.append(dev.sink, dev.name)
        dropdown.set_active_id(stream["sink"])
        dropdown.connect("changed", self._on_route, stream["id"])
        row_box.append(dropdown)

        mute_btn = Gtk.ToggleButton(active=stream["mute"])
        mute_btn.set_child(Gtk.Image.new_from_icon_name(
            "audio-volume-muted" if stream["mute"] else "audio-volume-high"
        ))
        mute_btn.connect("toggled", self._on_mute, stream["id"])
        row_box.append(mute_btn)

        row = Gtk.ListBoxRow()
        row.add_css_class("app-row")
        row.set_child(row_box)
        return row

    def _show_message(self, message: str) -> None:
        row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row_box.set_margin_top(48)
        row_box.set_margin_bottom(48)
        row_box.set_halign(Gtk.Align.CENTER)

        icon = Gtk.Image.new_from_icon_name("dialog-information")
        icon.set_pixel_size(48)
        label = Gtk.Label(label=message)
        label.add_css_class("text-muted")

        row_box.append(icon)
        row_box.append(label)

        row = Gtk.ListBoxRow()
        row.set_child(row_box)
        self.stream_list.append(row)

    def _on_mute(self, button: Gtk.ToggleButton, stream_id: int) -> None:
        muted = button.get_active()
        button.get_child().set_from_icon_name(
            "audio-volume-muted" if muted else "audio-volume-high"
        )
        try:
            self.controller.backend.set_stream_mute(stream_id, muted)
        except Exception as e:
            log.error("Failed to mute stream %d: %s", stream_id, e)

    def _on_route(self, combobox: Gtk.ComboBoxText, stream_id: int) -> None:
        sink = combobox.get_active_id()
        if not sink:
            return
        log.info("UI action: route stream %d to %s", stream_id, sink)
        try:
            self.controller.route_stream(stream_id, sink)
        except Exception as e:
            log.error("Failed to route stream %d: %s", stream_id, e)

    def _tick(self) -> bool:
        if self.stack.get_visible_child_name() == "split":
            self._refresh_split()
        return True  # Keep timer running

    # ---------- User actions ----------

    def _devices(self, ids) -> list[AudioDevice]:
        return [self.controller.devices[i] for i in ids if i in self.controller.devices]

    def _apply_selection(self) -> None:
        """Share to whatever is ticked, or stop if nothing is."""
        devices = self._devices(i for i, on in self.selected.items() if on)
        try:
            if devices:
                self.controller.start_sharing(devices)
            else:
                self.controller.stop_sharing()
        except Exception as e:
            self._error("Sharing failed", str(e))

    def _on_device_volume(self, device: AudioDevice, volume: int) -> None:
        try:
            self.controller.set_device_volume(device.id, volume)
        except Exception as e:
            log.error("Failed to set volume on %s: %s", device.name, e)

    def _on_device_toggle(self, device: AudioDevice, active: bool) -> None:
        self.selected[device.id] = active
        # A live session follows the ticks immediately.
        if self.controller.session.is_active:
            self._apply_selection()

    def _on_share(self, button: Gtk.Button) -> None:
        if self.controller.session.is_active:
            try:
                self.controller.stop_sharing()
            except Exception as e:
                self._error("Failed to Stop Sharing", str(e))
        elif not any(self.selected.values()):
            self._error("No Devices Selected", "Please toggle on at least one device before sharing.")
        else:
            self._apply_selection()

    def _on_master_volume(self, scale: Gtk.Scale) -> None:
        self.controller.set_master_volume(int(scale.get_value()))

    def _on_preset_changed(self, combo: Gtk.ComboBoxText) -> None:
        preset = self.controller.presets.get(combo.get_active_id() or "none")
        if not preset:
            return

        wanted = preset.get("devices", [])
        log.info("Loading preset '%s': %s", preset.get("name"), wanted)
        self.controller.last_preset = combo.get_active_id()
        for dev_id in self.selected:
            self.selected[dev_id] = dev_id in wanted

        idx = 0
        while row := self.device_list.get_row_at_index(idx):
            if hasattr(row, "device"):
                row.set_active(row.device.id in wanted)
            idx += 1

        if self.controller.session.is_active:
            self._apply_selection()

    def _on_save_preset(self, button: Gtk.Button) -> None:
        """Asks for a name, then saves the ticked devices under it."""
        selected_ids = [i for i, on in self.selected.items() if on]
        if not selected_ids:
            self._error("No Devices Selected", "Please select at least one device to save as a preset.")
            return

        dialog = Gtk.Dialog(title="Save Preset", transient_for=self)
        dialog.set_modal(True)

        content_area = dialog.get_content_area()
        content_area.set_orientation(Gtk.Orientation.VERTICAL)
        content_area.set_spacing(12)
        content_area.set_margin_top(16)
        content_area.set_margin_bottom(16)
        content_area.set_margin_start(16)
        content_area.set_margin_end(16)

        label = Gtk.Label(label="Enter a name for this preset:")
        label.set_halign(Gtk.Align.START)

        entry = Gtk.Entry()
        entry.set_placeholder_text("e.g. Gym buds, Movie Mode")

        content_area.append(label)
        content_area.append(entry)

        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)

        entry.connect("activate", lambda *args: dialog.response(Gtk.ResponseType.OK))

        def on_response(dialog, response_id):
            if response_id == Gtk.ResponseType.OK:
                name = entry.get_text().strip()
                if name:
                    preset_id = self.controller.save_preset(name, selected_ids)
                    self._refresh_presets(select_id=preset_id)
            dialog.destroy()

        dialog.connect("response", on_response)
        dialog.present()

    def _on_delete_preset(self, button: Gtk.Button) -> None:
        preset_id = self.preset_combo.get_active_id()
        if not preset_id or preset_id == "none":
            self._error("No Preset Selected", "Please select a preset from the dropdown to delete.")
            return

        self.controller.delete_preset(preset_id)
        if self.controller.last_preset == preset_id:
            self.controller.last_preset = None
        self._refresh_presets()

    def _refresh_presets(self, select_id: str | None = None) -> None:
        """Repopulate the dropdown without the refill counting as a user choice."""
        self.preset_combo.handler_block(self._preset_handler)
        try:
            self.preset_combo.remove_all()
            self.preset_combo.append("none", "-- Select Preset --")
            presets = self.controller.presets
            for pid, preset in presets.items():
                self.preset_combo.append(pid, preset.get("name", pid))

            if select_id in presets:
                self.preset_combo.set_active_id(select_id)
            else:
                self.preset_combo.set_active(0)
        finally:
            self.preset_combo.handler_unblock(self._preset_handler)

    def _on_shutdown(self, button: Gtk.Button) -> None:
        self.get_application().quit()

    def _on_reset(self, button: Gtk.Button) -> None:
        self.controller.reset_audio()

    # ---------- Controller signals ----------

    def _on_devices(self, controller: Controller, devices: list[AudioDevice]) -> None:
        preset = self.controller.presets.get(self.preset_combo.get_active_id() or "none", {})
        wanted = preset.get("devices", [])

        while row := self.device_list.get_row_at_index(0):
            self.device_list.remove(row)

        for dev in devices:
            # An offline device cannot be shared to, so it cannot stay ticked.
            if not dev.connected:
                self.selected[dev.id] = False
            self.selected.setdefault(dev.id, dev.id in wanted)

            row = DeviceRow(dev, self._on_device_toggle, self._on_device_volume)
            row.set_active(self.selected[dev.id])
            self.device_list.append(row)

    def _on_state(self, controller: Controller, state: SessionState) -> None:
        self._set_status(state)

        if state == SessionState.ACTIVE:
            self.btn_share.set_label("Stop Sharing")
            self.btn_share.add_css_class("active")
            self.btn_share.set_sensitive(True)
            self.btn_reset.set_sensitive(True)

            # Match the new output to the slider, so sharing does not change volume.
            target = self.controller.active_sink()
            if target:
                try:
                    self.controller.backend.set_volume(target, int(self.volume.get_value()))
                except Exception as e:
                    log.warning("Failed to initialize session volume: %s", e)
        elif state in (SessionState.STARTING, SessionState.STOPPING):
            self.btn_share.set_sensitive(False)
            self.btn_reset.set_sensitive(False)
        else:  # IDLE, ERROR
            self.btn_share.set_label("Start Sharing")
            self.btn_share.remove_css_class("active")
            self.btn_share.set_sensitive(True)
            self.btn_reset.set_sensitive(True)

    def _on_health(self, controller: Controller, status: BackendStatus) -> None:
        if status.health != BackendHealth.OK:
            self._error(
                "Audio Server Issue Detected",
                f"{status.message}\n\nTechnical details:\n{status.details}"
            )

    def _set_status(self, state: SessionState) -> None:
        for c in ["status-idle", "status-active", "status-repairing", "status-error"]:
            self.status_box.remove_css_class(c)

        if state == SessionState.ACTIVE:
            self.status_box.add_css_class("status-active")
            self.status_label.set_text("Sharing Active ✓ — Playing on multiple outputs")
        elif state == SessionState.REPAIRING or state == SessionState.STARTING:
            self.status_box.add_css_class("status-repairing")
            self.status_label.set_text("Reconnecting... Waiting for output device(s)")
        elif state == SessionState.ERROR:
            self.status_box.add_css_class("status-error")
            self.status_label.set_text("Error — Reset Audio recommended")
        else:
            self.status_box.add_css_class("status-idle")
            self.status_label.set_text("Idle — Ready to share")

    # ---------- Dialogs ----------

    def _error(self, title: str, message: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=title,
            secondary_text=message
        )
        dialog.set_modal(True)
        dialog.connect("response", lambda d, r: d.destroy())
        dialog.present()
