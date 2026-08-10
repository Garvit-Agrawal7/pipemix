# PipeMix — Development Log

## Session 1 — 2026-07-05

### What was built
- **Architecture finalized** over 4 plan revisions.
  - Name chosen: `PipeMix` (AudioFusion/AudioBridge/PipeShare all conflicted)
  - Backend abstraction locked: `AudioBackend` ABC → `PactlBackend` (pactl subprocess)
  - Tech stack locked: Python 3.12+, GTK4+PyGObject, BlueZ D-Bus, TOML, rotating logs
  - 10-step development order locked and architecture frozen after Step 5

- **`pyproject.toml`** — project metadata, hatchling build, `pipemix` entry point
- **`src/pipemix/__init__.py`** — package marker, version 0.1.0
- **`src/pipemix/models.py`** — all core data structures:
  - `DeviceKind`, `SessionState` (enums)
  - `DeviceCapabilities` (with `for_bluetooth/hdmi/usb/builtin()` factories)
  - `AudioDevice` (MAC-stable identity, runtime sink_name, capability-aware)
  - `AudioStream` (active PipeWire sink-input)
  - `VirtualSink` (UUID-named: `pipemix_xxxxxxxx`, stores module_id for cleanup)
  - `SharingSession` (first-class state object owned by Controller)
- **`src/pipemix/services/backend/__init__.py`** — `AudioBackend` ABC + `BackendStatus` + `BackendHealth` + exceptions
- **`src/pipemix/services/backend/pactl_backend.py`** — `PactlBackend`:
  - `health_check()` — checks pactl + PipeWire reachable, never raises
  - `list_outputs()` — parses `pactl list sinks`, gets Description for display names, excludes pipemix_* sinks
  - `create_virtual_output()` — loads `module-combine-sink` with UUID sink name
  - `move_streams()` — moves all sink-inputs to virtual sink
  - `destroy_virtual_output()` — safe, idempotent (does not raise if already gone)
  - `find_orphaned_virtual_sinks()` — detects pipemix_* sinks for crash recovery
  - `set_default_sink()` — sets PipeWire default output
- **`tests/unit/test_pactl_backend.py`** — 6-step test script (no Bluetooth required)

### Environment verified on target machine
- Linux Mint, PipeWire 1.0.5, PulseAudio compat active
- Python 3.12.3, GTK4 OK, dbus OK, tomllib OK

### What was tested
- ✅ `tests/unit/test_pactl_backend.py` — PASSED (run twice, 2026-07-05)

### What passed
- health_check(): PipeWire reachable
- list_outputs(): Built-in audio found with PipeWire description ("Family 17h/19h HD Audio Controller Analog Stereo")
- create_virtual_output(): UUID sink created and confirmed in PipeWire (module_id=536870913)
- move_streams(): Moveable streams routed; unmoveable streams (DONT_MOVE flag) skipped gracefully with WARNING
- find_orphaned_virtual_sinks(): Orphan detected correctly
- destroy_virtual_output(): Sink removed cleanly
- Double-destroy idempotency: No raise on already-gone module ("No such entity" logged at DEBUG only)

### Known benign behavior
- `Failed to move stream N: Failure: Invalid argument` — PipeWire DONT_MOVE flag on certain system/monitor streams. Correct behaviour: skip and warn.

- ✅ `tests/unit/test_controller.py` — PASSED (2026-07-05)

### What passed
- Controller start: initialized PactlBackend, ran startup crash recovery check, started DeviceMonitor, and refreshed device list.
- Device discovery: detected built-in and both Bluetooth earbuds cleanly.
- Start sharing: successfully saved original default sink (`bluez_output.E3_ED_0F_56_16_7D.1`), created virtual sink (`pipemix_cda6618f`), and routed streams.
- Stop sharing: successfully restored original default sink and destroyed virtual sink.
- Reset audio: successfully entered repairing state, checked/cleaned orphans, and reset controller to idle cleanly.

- ✅ `main.py` interactive CLI test — PASSED (2026-07-05)

### What passed
- Executed `python3 src/pipemix/main.py`.
- Correctly read keyboard shortcuts (`s`, `t`, `r`, `q`) asynchronously via GLib's IO channels.
- Successfully verified full integration: creating combined sinks, setting system default output, moving active streams, handling Bluetooth device drop/auto-reconnect, and teardown.

### Phase 1 (Core Audio Engine) Status
- **COMPLETED** 2026-07-05. All core backend, device monitoring, and controller logic is fully tested and verified working.

- ✅ `main_window.py` dynamic toggle rebuild and master volume control — PASSED
- ✅ Brave profile mapping default fallback — PASSED
- ✅ Direct routing optimization for single-device active sessions — PASSED
- ✅ HeaderBar power shutdown icon exit button — PASSED
- ✅ Individual device volume sliders in DeviceRow — PASSED
- ✅ Named presets CRUD and dropdown selection bar — PASSED
- ✅ Premium UI/UX redesign (floating cards, blue sliders, minimal muted hover indicators) — PASSED
- ✅ Native Debian package builder and verification — PASSED
- ✅ Virtual combined sink exclusion (filters out non-physical devices like dual_bt) — PASSED
- ✅ Real-time volume synchronization (enforces master and hardware device volumes during rebuilds) — PASSED

### What passed
- Master volume control slider.
- Brave default profile mapping.
- Single-device direct routing optimization.
- HeaderBar power shutdown cleanup hook.
- Individual device volume sliders.
- Grouped presets selection panel.
- Softer rounded corners (16px) and desaturated rose red minimal hovers.
- Packaging compiler script (`build_deb.py`) generating `build/pipemix.deb`.
- Exclude virtual outputs and system virtual helper loopback streams from the app.
- Cache and synchronize volumes automatically on device connection/disconnection.
- Preserve custom slider volumes locally during device lists re-enumeration.

**All project milestones are fully completed and verified!**
