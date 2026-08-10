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
import time
from gi.repository import Gdk, GObject, Gtk, GLib

from pipemix.models import AudioDevice, DeviceKind, SessionState
from pipemix.ui.device_row import DeviceRow
from pipemix.services.backend import BackendHealth

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom CSS Stylesheet
# ---------------------------------------------------------------------------
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
    """
    Main PipeMix Application Window.
    Implements a responsive sidebar + stack design.
    """

    def __init__(self, app: Gtk.Application, controller: Controller) -> None:
        super().__init__(application=app)
        self.controller = controller
        self.set_title("PipeMix")
        self.set_default_size(800, 600)

        # Track checkbox selection states: device_id -> bool
        self.selected_states: dict[str, bool] = {}

        # Set up custom styles
        self._apply_css()

        # Build Header Bar
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)
        # Custom center title with brand logo icon loading
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_box.set_valign(Gtk.Align.CENTER)
        
        # Try local path first, then installed path, fallback to generic icon
        import os
        from pathlib import Path
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

        # Power/Shutdown Button in top-right of HeaderBar
        self.btn_shutdown = Gtk.Button()
        self.btn_shutdown.set_icon_name("system-shutdown")
        self.btn_shutdown.set_tooltip_text("Shutdown PipeMix completely")
        self.btn_shutdown.add_css_class("btn-shutdown")
        self.btn_shutdown.connect("clicked", self._on_shutdown_clicked)
        header.pack_end(self.btn_shutdown)

        self.set_titlebar(header)

        # Main Layout: Sidebar (left) + Content Stack (right)
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        # 1. Left Sidebar
        self.sidebar_list = Gtk.ListBox()
        self.sidebar_list.add_css_class("sidebar")
        self.sidebar_list.set_size_request(200, -1)
        self.sidebar_list.connect("row-selected", self._on_sidebar_row_selected)

        # Sidebar rows
        self.row_combine = self._create_sidebar_row("Combine Sinks", "audio-speakers")
        self.row_split = self._create_sidebar_row("Split Audio", "audio-card")
        self.sidebar_list.append(self.row_combine)
        self.sidebar_list.append(self.row_split)
        main_box.append(self.sidebar_list)

        # 2. Right Content Stack
        self.content_stack = Gtk.Stack()
        self.content_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.content_stack.set_transition_duration(250)
        self.content_stack.set_hexpand(True)
        self.content_stack.set_vexpand(True)
        main_box.append(self.content_stack)

        # Build pages inside stack
        self._build_combine_sinks_page()
        self._build_split_audio_page()

        self.set_child(main_box)

        # Connect controller signals
        self.controller.connect("device-list-updated", self._on_device_list_updated)
        self.controller.connect("session-state-changed", self._on_session_state_changed)
        self.controller.connect("backend-health-changed", self._on_backend_health_changed)

        # Select first sidebar row on start
        self.sidebar_list.select_row(self.row_combine)

        # Periodically refresh the Split Audio stream lists (every 3 seconds)
        GLib.timeout_add(3000, self._periodic_split_refresh)

    def _apply_css(self) -> None:
        """Loads and applies custom CSS styles."""
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS_STYLES.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _create_sidebar_row(self, text: str, icon_name: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.add_css_class("sidebar-row")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        label = Gtk.Label(label=text)
        box.append(icon)
        box.append(label)
        row.set_child(box)
        return row

    def _on_sidebar_row_selected(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        """Toggles the visible page in the stack based on sidebar choice."""
        if not row:
            return
        if row == self.row_combine:
            self.content_stack.set_visible_child_name("combine")
        elif row == self.row_split:
            self.content_stack.set_visible_child_name("split")
            self._refresh_split_streams_list()

    # ------------------------------------------------------------------
    # Page 1: Combine Sinks Layout
    # ------------------------------------------------------------------

    def _build_combine_sinks_page(self) -> None:
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

        # Preset Selection Bar
        preset_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        preset_box.add_css_class("presets-panel")
        preset_box.set_valign(Gtk.Align.CENTER)
        
        preset_label = Gtk.Label(label="Presets:")
        preset_label.add_css_class("font-bold")
        preset_box.append(preset_label)
        
        self.preset_combo = Gtk.ComboBoxText()
        self.preset_combo.set_hexpand(True)
        self.preset_combo.add_css_class("device-dropdown")
        self._preset_combo_handler_id = self.preset_combo.connect("changed", self._on_preset_changed)
        preset_box.append(self.preset_combo)
        
        self.btn_save_preset = Gtk.Button(label="Save")
        self.btn_save_preset.set_tooltip_text("Save current selection as preset")
        self.btn_save_preset.connect("clicked", self._on_save_preset_clicked)
        preset_box.append(self.btn_save_preset)
        
        self.btn_delete_preset = Gtk.Button(label="Delete")
        self.btn_delete_preset.set_tooltip_text("Delete selected preset")
        self.btn_delete_preset.add_css_class("btn-delete")
        self.btn_delete_preset.connect("clicked", self._on_delete_preset_clicked)
        preset_box.append(self.btn_delete_preset)
        
        page_box.append(preset_box)

        # Scrollable list container
        scroll = Gtk.ScrolledWindow()
        scroll.set_size_request(-1, 260)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add_css_class("device-list-frame")

        self.device_listbox = Gtk.ListBox()
        self.device_listbox.add_css_class("device-list")
        self.device_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll.set_child(self.device_listbox)
        page_box.append(scroll)

        # Status Panel
        self.status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.status_box.add_css_class("status-panel")
        self.status_box.add_css_class("status-idle")
        self.status_label = Gtk.Label(label="Idle — Ready to share")
        self.status_box.append(self.status_label)
        page_box.append(self.status_box)

        # 4. Group Volume Slider
        vol_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        vol_box.set_margin_top(16)
        vol_box.set_margin_bottom(8)
        
        vol_icon = Gtk.Image.new_from_icon_name("audio-volume-high")
        vol_label = Gtk.Label(label="Master Volume")
        
        self.volume_slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.volume_slider.set_hexpand(True)
        self.volume_slider.set_draw_value(True)
        self.volume_slider.set_value(50)  # Start at 50% default
        self.volume_slider.connect("value-changed", self._on_volume_slider_changed)
        
        vol_box.append(vol_icon)
        vol_box.append(vol_label)
        vol_box.append(self.volume_slider)
        page_box.append(vol_box)

        # Bottom buttons box
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)

        self.btn_share = Gtk.Button(label="Start Sharing")
        self.btn_share.set_hexpand(True)
        self.btn_share.add_css_class("btn-share")
        self.btn_share.connect("clicked", self._on_share_clicked)
        btn_box.append(self.btn_share)

        self.btn_reset = Gtk.Button(label="Reset Audio")
        self.btn_reset.add_css_class("btn-reset")
        self.btn_reset.connect("clicked", self._on_reset_clicked)
        btn_box.append(self.btn_reset)

        page_box.append(btn_box)
        self.content_stack.add_named(page_box, "combine")

        # Populate presets initially
        self._refresh_presets_dropdown()

    # ------------------------------------------------------------------
    # Page 2: Split Audio Layout (Different tastes/tabs router)
    # ------------------------------------------------------------------

    def _build_split_audio_page(self) -> None:
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

        # Scrolled window for application streams list
        scroll = Gtk.ScrolledWindow()
        scroll.set_size_request(-1, 350)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add_css_class("device-list-frame")

        self.app_listbox = Gtk.ListBox()
        self.app_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll.set_child(self.app_listbox)
        page_box.append(scroll)

        self.content_stack.add_named(page_box, "split")

    def _refresh_split_streams_list(self) -> None:
        """Fetches active sink-inputs from backend and displays/updates them incrementally."""
        try:
            # 1. Fetch active playing streams using pactl wrapper
            import subprocess
            result = subprocess.run(
                ["pactl", "list", "sink-inputs"],
                capture_output=True, text=True, timeout=3
            )
            if result.returncode != 0:
                # Clean up and show error
                self._clear_split_streams_list()
                self._show_no_streams_message("Could not query audio streams.")
                return

            streams = self._parse_sink_inputs_detailed(result.stdout)
            if not streams:
                self._clear_split_streams_list()
                self._show_no_streams_message("No applications are currently playing audio.")
                return

            # Fetch mapping of sink_index -> sink_name to resolve indices
            sink_map = {}
            sinks_result = subprocess.run(
                ["pactl", "list", "short", "sinks"],
                capture_output=True, text=True, timeout=3
            )
            if sinks_result.returncode == 0:
                for line in sinks_result.stdout.strip().splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        s_idx = parts[0].strip()
                        s_name = parts[1].strip()
                        sink_map[s_idx] = s_name

            # 2. Fetch output devices available for routing
            devices = [d for d in self.controller.known_devices.values() if d.is_connected and d.sink_name]
            
            # 3. Get currently displayed rows
            existing_rows = {}
            idx = 0
            while True:
                row = self.app_listbox.get_row_at_index(idx)
                if not row:
                    break
                if hasattr(row, "stream_id"):
                    existing_rows[row.stream_id] = row
                    idx += 1
                else:
                    # Remove placeholder / non-stream messages
                    self.app_listbox.remove(row)

            # Map new streams by ID
            new_streams = {s["id"]: s for s in streams}

            # 4. Remove rows that are no longer active
            for s_id, row in list(existing_rows.items()):
                if s_id not in new_streams:
                    self.app_listbox.remove(row)
                    existing_rows.pop(s_id)

            # 5. Add or Update rows in-place
            for s_id, stream in new_streams.items():
                stream_sink_index = stream.get("sink", "")
                resolved_stream_sink_name = sink_map.get(stream_sink_index, stream_sink_index)

                # Find correct dropdown index matching active device
                active_idx = 0
                for d_idx, dev in enumerate(devices):
                    if dev.sink_name == resolved_stream_sink_name:
                        active_idx = d_idx
                        break

                is_muted = stream.get("mute", False)

                if s_id in existing_rows:
                    # Update existing row in-place
                    row = existing_rows[s_id]
                    row.label.set_text(stream["name"])
                    row.subtitle_label.set_text(f"Stream ID: {s_id} • {stream['media_class']}")

                    # Safely sync dropdown choice without firing change events
                    if row.dropdown.get_active() != active_idx:
                        if hasattr(row, "dropdown_handler_id"):
                            row.dropdown.handler_block(row.dropdown_handler_id)
                        try:
                            row.dropdown.set_active(active_idx)
                        finally:
                            if hasattr(row, "dropdown_handler_id"):
                                row.dropdown.handler_unblock(row.dropdown_handler_id)

                    # Safely sync mute button state without firing toggled events
                    if row.mute_btn.get_active() != is_muted:
                        if hasattr(row, "mute_handler_id"):
                            row.mute_btn.handler_block(row.mute_handler_id)
                        try:
                            row.mute_btn.set_active(is_muted)
                            row.mute_img.set_from_icon_name("audio-volume-muted" if is_muted else "audio-volume-high")
                        finally:
                            if hasattr(row, "mute_handler_id"):
                                row.mute_btn.handler_unblock(row.mute_handler_id)
                else:
                    # Create a new row
                    row = Gtk.ListBoxRow()
                    row.add_css_class("app-row")
                    row.stream_id = s_id
                    
                    row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)

                    # Icon
                    icon = Gtk.Image.new_from_icon_name("audio-speakers")
                    icon.set_icon_size(Gtk.IconSize.LARGE)
                    row_box.append(icon)

                    # App Details Label Box
                    details_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                    details_box.set_hexpand(True)

                    row.label = Gtk.Label(label=stream["name"])
                    row.label.set_halign(Gtk.Align.START)
                    row.label.add_css_class("app-name")

                    row.subtitle_label = Gtk.Label(label=f"Stream ID: {s_id} • {stream['media_class']}")
                    row.subtitle_label.set_halign(Gtk.Align.START)
                    row.subtitle_label.add_css_class("app-stream-id")

                    details_box.append(row.label)
                    details_box.append(row.subtitle_label)
                    row_box.append(details_box)

                    # Dropdown for Target Output Selection
                    row.dropdown = Gtk.ComboBoxText()
                    row.dropdown.add_css_class("device-dropdown")

                    # Populate choices
                    for dev in devices:
                        row.dropdown.append(dev.sink_name, dev.display_name)
                    row.dropdown.set_active(active_idx)
                    
                    # Connect and store handler ID
                    row.dropdown_handler_id = row.dropdown.connect(
                        "changed", 
                        lambda widget, sid=s_id: self._on_app_route_changed(widget, sid)
                    )
                    row_box.append(row.dropdown)

                    # Mute Toggle Button
                    row.mute_btn = Gtk.ToggleButton()
                    row.mute_btn.add_css_class("btn-mute")
                    row.mute_btn.set_active(is_muted)
                    
                    row.mute_img = Gtk.Image.new_from_icon_name("audio-volume-muted" if is_muted else "audio-volume-high")
                    row.mute_btn.set_child(row.mute_img)
                    
                    # Connect and store handler ID
                    row.mute_handler_id = row.mute_btn.connect(
                        "toggled", 
                        lambda btn, sid=s_id: self._on_app_mute_toggled(btn, sid)
                    )
                    row_box.append(row.mute_btn)

                    row.set_child(row_box)
                    self.app_listbox.append(row)

        except Exception as e:
            log.error("Failed to refresh split streams list: %s", e)
            self._show_no_streams_message("Failed to fetch audio routing data.")

    def _clear_split_streams_list(self) -> None:
        """Helper to safely empty all items from the streams ListBox."""
        while True:
            row = self.app_listbox.get_row_at_index(0)
            if not row:
                break
            self.app_listbox.remove(row)

    def _on_app_mute_toggled(self, button: Gtk.ToggleButton, stream_id: int) -> None:
        """Toggles the mute state of a specific application audio stream."""
        muted = button.get_active()
        # Find the specific row to update its child image immediately
        idx = 0
        while True:
            row = self.app_listbox.get_row_at_index(idx)
            if not row:
                break
            if hasattr(row, "stream_id") and row.stream_id == stream_id:
                row.mute_img.set_from_icon_name("audio-volume-muted" if muted else "audio-volume-high")
                break
            idx += 1
        
        try:
            import subprocess
            val = "1" if muted else "0"
            subprocess.run(["pactl", "set-sink-input-mute", str(stream_id), val], check=True)
            log.info("UI action: Stream %d muted=%s", stream_id, muted)
        except Exception as e:
            log.error("Failed to mute stream %d: %s", stream_id, e)

    def _show_no_streams_message(self, message: str) -> None:
        row = Gtk.ListBoxRow()
        row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row_box.set_margin_top(48)
        row_box.set_margin_bottom(48)
        row_box.set_halign(Gtk.Align.CENTER)

        icon = Gtk.Image.new_from_icon_name("dialog-information")
        icon.set_pixel_size(48)
        
        label = Gtk.Label(label=message)
        label.add_css_class("text-muted")
        label.set_halign(Gtk.Align.CENTER)

        row_box.append(icon)
        row_box.append(label)
        row.set_child(row_box)
        self.app_listbox.append(row)

    def _on_app_route_changed(self, combobox: Gtk.ComboBoxText, stream_id: int) -> None:
        """Triggers when user selects a different output device in the app dropdown."""
        sink_name = combobox.get_active_id()
        if not sink_name:
            return
        log.info("UI action: route stream %d to %s", stream_id, sink_name)
        try:
            self.controller.set_stream_routing(stream_id, sink_name)
        except Exception as e:
            log.error("Failed to route stream %d: %s", stream_id, e)

    def _periodic_split_refresh(self) -> bool:
        """GLib timeout callback. Refreshes stream list if Split Audio is visible."""
        if self.content_stack.get_visible_child_name() == "split":
            self._refresh_split_streams_list()
        return True  # Keep timer running

    def _resolve_profile_dir_to_name(self, profile_dir: str) -> str:
        """Helper to resolve a Brave profile directory to its friendly name."""
        import os
        import json
        possible_paths = [
            os.path.expanduser("~/.config/BraveSoftware/Brave-Browser/Local State"),
            os.path.expanduser("~/.config/brave-browser/Local State"),
        ]
        for path in possible_paths:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    profiles_cache = data.get("profile", {}).get("info_cache", {})
                    if profile_dir in profiles_cache:
                        return str(profiles_cache[profile_dir].get("name", profile_dir))
                except Exception:
                    pass
        return profile_dir

    def _get_brave_profile_name(self, pid: str) -> str | None:
        """Finds the friendly Brave profile name by recursively checking parent PIDs."""
        import os
        curr_pid = pid
        
        # Walk up the process tree (max 5 levels) to find the parent browser process
        for _ in range(5):
            try:
                # 1. Check current process command line
                cmdline_path = f"/proc/{curr_pid}/cmdline"
                if not os.path.exists(cmdline_path):
                    break
                with open(cmdline_path, "rb") as f:
                    content = f.read()
                args = [a.decode("utf-8", errors="ignore") for a in content.split(b"\x00") if a]
                
                profile_dir = None
                is_main_browser = True
                for arg in args:
                    if arg.startswith("--type="):
                        is_main_browser = False
                    if arg.startswith("--profile-directory="):
                        profile_dir = arg.split("=", 1)[1]
                        break
                
                if profile_dir:
                    return self._resolve_profile_dir_to_name(profile_dir)
                
                if is_main_browser:
                    # If it's the main browser process and no profile directory was specified,
                    # Chromium defaults to the "Default" profile directory.
                    return self._resolve_profile_dir_to_name("Default")
                
                # 2. Walk up to parent PID
                status_path = f"/proc/{curr_pid}/status"
                if not os.path.exists(status_path):
                    break
                ppid = None
                with open(status_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.startswith("PPid:"):
                            ppid = line.split(":", 1)[1].strip()
                            break
                if not ppid or ppid == curr_pid or ppid == "0":
                    break
                curr_pid = ppid
            except Exception:
                break
        return None

    def _parse_sink_inputs_detailed(self, output: str) -> list[dict]:
        """Parses raw pactl detailed output to identify PIDs/App names."""
        streams = []
        current = {}
        
        for raw_line in output.splitlines():
            line = raw_line.strip()
            
            if raw_line.startswith("Sink Input #"):
                if current:
                    streams.append(current)
                current = {"id": int(raw_line.split("#")[1].strip())}
                
            elif line.startswith("Sink:"):
                # Handle both short formats or detailed index values
                try:
                    current["sink"] = line.split(":", 1)[1].strip()
                except Exception:
                    pass
                    
            elif line.startswith("application.name ="):
                current["app_name"] = line.split("=")[1].replace('"', '').strip()
                
            elif line.startswith("media.name ="):
                current["media_name"] = line.split("=")[1].replace('"', '').strip()

            elif line.startswith("media.class ="):
                current["media_class"] = line.split("=")[1].replace('"', '').strip()

            elif line.startswith("application.process.id ="):
                current["pid"] = line.split("=")[1].replace('"', '').strip()

            elif line.startswith("Mute:"):
                current["mute"] = line.split(":", 1)[1].strip() == "yes"

        if current:
            streams.append(current)

        # Resolve display names from metadata (combining media name and app name)
        resolved_streams = []
        for s in streams:
            # Skip combined virtual outputs streams (PipeMix internals)
            if s.get("app_name") == "pipemix" or "pipemix_" in s.get("media_name", ""):
                continue
                
            media_class = s.get("media_class", "Stream/Output/Audio")
            if media_class != "Stream/Output/Audio":
                continue

            app = s.get("app_name", "Application")
            media = s.get("media_name", "")
            
            # Resolve Brave friendly profile names using process cmdline lookup
            if app.lower() in ("brave", "brave-browser", "brave browser") and "pid" in s:
                profile_name = self._get_brave_profile_name(s["pid"])
                if profile_name:
                    app = f"Brave ({profile_name})"

            # Create a clean display name: e.g. "Brave (Gauravv) (YouTube)" or "Spotify"
            if media and media != "Playback" and media != app:
                name = f"{app} ({media})"
            else:
                name = app
                
            resolved_streams.append({
                "id": s["id"],
                "name": name,
                "sink": s.get("sink", ""),
                "media_class": "Audio Output",
                "mute": s.get("mute", False)
            })
            
        return resolved_streams

    # ------------------------------------------------------------------
    # UI Interaction Handlers
    # ------------------------------------------------------------------

    def _on_device_volume_changed(self, device: AudioDevice, volume: int) -> None:
        """Called when a volume slider on an individual device row is adjusted."""
        try:
            self.controller.set_device_volume(device.device_id, volume)
        except Exception as e:
            log.error("Failed to set volume on %s: %s", device.display_name, e)

    def _on_device_row_toggled(self, device: AudioDevice, active: bool) -> None:
        """Updates internal selected state list when a device toggle changes."""
        self.selected_states[device.device_id] = active

        # If sharing is currently active, automatically rebuild the virtual sink!
        if self.controller.session.state == SessionState.ACTIVE:
            log.info("Sharing is active. Rebuilding virtual sink to apply toggle changes...")
            selected_ids = [dev_id for dev_id, is_active in self.selected_states.items() if is_active]
            selected_devices = [self.controller.known_devices[dev_id] 
                                for dev_id in selected_ids 
                                if dev_id in self.controller.known_devices]
            
            if not selected_devices:
                # If they toggled off all devices, stop sharing
                try:
                    self.controller.stop_sharing()
                except Exception as e:
                    self._show_error_dialog("Failed to stop sharing", str(e))
            else:
                try:
                    self.controller.start_sharing(selected_devices)
                except Exception as e:
                    self._show_error_dialog("Failed to update sharing", str(e))

    def _on_share_clicked(self, button: Gtk.Button) -> None:
        """Triggers Start/Stop action in Controller based on state."""
        state = self.controller.session.state
        
        if state == SessionState.ACTIVE:
            log.info("UI action: Stop Sharing clicked.")
            try:
                self.controller.stop_sharing()
            except Exception as e:
                self._show_error_dialog("Failed to Stop Sharing", str(e))
        else:
            log.info("UI action: Start Sharing clicked.")
            
            # Find selected devices
            selected_ids = [dev_id for dev_id, active in self.selected_states.items() if active]
            selected_devices = [self.controller.known_devices[dev_id] 
                                for dev_id in selected_ids 
                                if dev_id in self.controller.known_devices]
            
            if not selected_devices:
                self._show_error_dialog("No Devices Selected", "Please toggle on at least one device before sharing.")
                return

            try:
                self.controller.start_sharing(selected_devices)
            except Exception as e:
                self._show_error_dialog("Failed to Start Sharing", str(e))

    def _on_volume_slider_changed(self, scale: Gtk.Scale) -> None:
        """Fires when the volume slider is adjusted."""
        val = int(scale.get_value())
        self.controller.set_master_volume(val)

    def _on_preset_changed(self, combo: Gtk.ComboBoxText) -> None:
        """Fires when the selected preset is changed in the dropdown."""
        preset_id = combo.get_active_id()
        if not preset_id or preset_id == "none":
            return
            
        presets = self.controller.get_all_presets()
        preset = presets.get(preset_id)
        if not preset:
            return
            
        preset_devices = preset.get("devices", [])
        log.info("Loading preset '%s': %s", preset.get("name"), preset_devices)
        
        # 1. Update states and active rows
        for dev_id in list(self.selected_states.keys()):
            self.selected_states[dev_id] = dev_id in preset_devices

        # Synchronize rows visually
        idx = 0
        while True:
            row = self.device_listbox.get_row_at_index(idx)
            if not row:
                break
            if hasattr(row, "device"):
                is_active = row.device.device_id in preset_devices
                row.set_active(is_active)
            idx += 1
            
        # 2. If sharing is active, automatically rebuild to match the new preset!
        if self.controller.session.state == SessionState.ACTIVE:
            selected_devices = [self.controller.known_devices[dev_id] 
                                for dev_id in preset_devices 
                                if dev_id in self.controller.known_devices]
            if selected_devices:
                try:
                    self.controller.start_sharing(selected_devices)
                except Exception as e:
                    self._show_error_dialog("Failed to Apply Preset", str(e))
            else:
                try:
                    self.controller.stop_sharing()
                except Exception as e:
                    self._show_error_dialog("Failed to Stop Sharing", str(e))

    def _on_save_preset_clicked(self, button: Gtk.Button) -> None:
        """Prompts for a preset name and saves the currently selected devices."""
        # Check if any devices are actually selected
        selected_ids = [dev_id for dev_id, active in self.selected_states.items() if active]
        if not selected_ids:
            self._show_error_dialog("No Devices Selected", "Please select at least one device to save as a preset.")
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
                    self._refresh_presets_dropdown(select_preset_id=preset_id)
            dialog.destroy()
            
        dialog.connect("response", on_response)
        dialog.present()

    def _on_delete_preset_clicked(self, button: Gtk.Button) -> None:
        """Deletes the currently selected preset."""
        preset_id = self.preset_combo.get_active_id()
        if not preset_id or preset_id == "none":
            self._show_error_dialog("No Preset Selected", "Please select a preset from the dropdown to delete.")
            return
            
        self.controller.delete_preset(preset_id)
        self._refresh_presets_dropdown()

    def _refresh_presets_dropdown(self, select_preset_id: str = None) -> None:
        """Re-populates the preset ComboBoxText and optionally selects one."""
        if not hasattr(self, "preset_combo"):
            return
            
        if hasattr(self, "_preset_combo_handler_id") and self._preset_combo_handler_id:
            self.preset_combo.handler_block(self._preset_combo_handler_id)
            
        try:
            self.preset_combo.remove_all()
            
            # Placeholder item
            self.preset_combo.append("none", "-- Select Preset --")
            
            presets = self.controller.get_all_presets()
            for pid, preset in presets.items():
                self.preset_combo.append(pid, preset.get("name", pid))
                
            if select_preset_id and select_preset_id in presets:
                self.preset_combo.set_active_id(select_preset_id)
            else:
                self.preset_combo.set_active(0)
        finally:
            if hasattr(self, "_preset_combo_handler_id") and self._preset_combo_handler_id:
                self.preset_combo.handler_unblock(self._preset_combo_handler_id)

    def _on_shutdown_clicked(self, button: Gtk.Button) -> None:
        """Called when the top-right shutdown button is clicked."""
        log.info("UI action: Shutdown button clicked. Stopping session and quitting...")
        self.get_application().quit()

    def _on_reset_clicked(self, button: Gtk.Button) -> None:
        """Triggers Reset Audio in Controller."""
        log.info("UI action: Reset Audio clicked.")
        self.controller.reset_audio()

    # ------------------------------------------------------------------
    # Controller Signal Observers (Dumb View Actions)
    # ------------------------------------------------------------------

    def _on_device_list_updated(self, controller: Controller, devices: list[AudioDevice]) -> None:
        """Called when Controller refreshes the device list. Re-populates the list box."""
        # 1. Clean current ListBox items
        while True:
            row = self.device_listbox.get_row_at_index(0)
            if not row:
                break
            self.device_listbox.remove(row)

        # 2. Add new DeviceRows
        for dev in devices:
            # Skip visual virtual outputs
            if dev.device_id.startswith("pipemix_"):
                continue

            # Ensure we track its switch state (keep checked if already checked)
            if dev.device_id not in self.selected_states:
                self.selected_states[dev.device_id] = False
                
            # If the device has gone offline, turn off its checkbox state
            if not dev.is_connected:
                self.selected_states[dev.device_id] = False

            row = DeviceRow(dev, self._on_device_row_toggled, self._on_device_volume_changed)
            row.set_active(self.selected_states[dev.device_id])
            self.device_listbox.append(row)

        log.debug("UI updated with %d device row(s).", len(devices))

    def _on_session_state_changed(self, controller: Controller, state: SessionState) -> None:
        """Reflects the sharing state machine changes in status boxes and buttons."""
        self._update_status_panel(state)

        # Configure sharing button
        if state == SessionState.ACTIVE:
            self.btn_share.set_label("Stop Sharing")
            self.btn_share.add_css_class("active")
            self.btn_share.set_sensitive(True)
            self.btn_reset.set_sensitive(True)
            
            # Instantly sync the active output's volume to the slider value
            target_sink = None
            if self.controller.session.virtual_sink:
                target_sink = self.controller.session.virtual_sink.sink_name
            elif self.controller.session.selected_devices:
                target_sink = self.controller.session.selected_devices[0].sink_name
                
            if target_sink:
                try:
                    vol = int(self.volume_slider.get_value())
                    self.controller.backend.set_sink_volume(target_sink, vol)
                except Exception as e:
                    log.warning("Failed to initialize session volume: %s", e)
        elif state == SessionState.STARTING or state == SessionState.STOPPING:
            self.btn_share.set_sensitive(False)
            self.btn_reset.set_sensitive(False)
        else:  # IDLE, ERROR
            self.btn_share.set_label("Start Sharing")
            self.btn_share.remove_css_class("active")
            self.btn_share.set_sensitive(True)
            self.btn_reset.set_sensitive(True)

    def _on_backend_health_changed(self, controller: Controller, status: BackendStatus) -> None:
        """Notifies the user if PipeWire goes offline."""
        if status.health != BackendHealth.OK:
            self._show_error_dialog(
                "Audio Server Issue Detected",
                f"{status.message}\n\nTechnical details:\n{status.details}"
            )

    def _update_status_panel(self, state: SessionState) -> None:
        """Sets panel CSS classes based on current state."""
        # Clear old styling classes
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

    # ------------------------------------------------------------------
    # Dialogs & Notification Boxes
    # ------------------------------------------------------------------

    def _show_error_dialog(self, title: str, message: str) -> None:
        """Shows a standard GTK4 error alert dialog."""
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
