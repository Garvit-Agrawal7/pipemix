# PipeMix: redesign the UI, then move it from GTK4 to React + TypeScript on pywebview

## Context

PipeMix routes audio to multiple outputs on Linux (PipeWire/`pactl` + BlueZ) and ships
as a `.deb`. Two decisions are already made — implement them, don't re-litigate:

- **Stack: pywebview 5.x** (`python3-webview`, Ubuntu noble/universe). It renders in
  WebKitGTK and runs the GTK/GLib main loop, so the existing `GObject` signals,
  `GLib.timeout_add`, and `Gio` D-Bus BlueZ monitoring keep working untouched. Tauri
  was evaluated and rejected: same renderer, its cross-platform advantage is moot for a
  `pactl` app, and it would force the Python backend into a Rust rewrite or a sidecar.
- **Target: Ubuntu 24.04+ only.** 22.04 and Debian 12 carry pywebview 3.3.5 (two majors
  behind) and Python 3.10, against this project's declared `>=3.12`. Write no
  compatibility shims for them.

The work is two phases with a **hard stop** between. Do not begin Phase 2 until the user
has approved a design.

---

## Delegation via `omp`

`omp` (v18.2.0, on PATH) is a separate agent CLI with its own model access. Use it to
fan work out. Verified invocation:

```
omp -p --model claude-sonnet-5 --no-session --auto-approve "<full task>"
omp -p --model claude-opus-5   --no-session --auto-approve --thinking high "<full task>"
```

Mechanics that matter:

- `-p` is non-interactive and exits when done. Nobody is there to answer an approval
  prompt, so `--auto-approve` is required for any task that writes files.
- The spawned agent inherits **none** of your context. Every prompt must be
  self-contained: name the files, state the constraints, say what "done" looks like.
- stdout opens with a `Working...` line before the real output. Strip it if parsing.
- Use exact model IDs. `--model opus` fuzzy-matches and may land on an older Opus.
- Bound long tasks with `--max-time 15m`. It runs in the current directory; `--cwd` overrides.

### Which model gets what

**Opus 5** — judgement, taste, and anywhere being wrong is expensive:
- the entire Phase 1 design pass
- `api.py` and `bridge.py`, where the selection-state semantics are subtle
- diagnosing the `webview.start(gui='gtk')` GLib-loop question
- any point where you would otherwise guess

**Sonnet 5** — well-specified mechanical work, once the target is unambiguous:
- React components implementing an already-approved design
- `types.ts`
- Vite config and the `build_deb.py` edits
- porting the stream-poll dedupe

Do not delegate what is faster done directly — a one-line `Depends:` change costs more
as a subprocess than as an edit. Fan out when work is genuinely parallel or genuinely
large, not to look busy.

**Commit before any fan-out.** `--auto-approve` means agents write without asking, and a
clean tree is what makes that recoverable.

---

# Phase 1 — design

## The brief

The current UI is not broken; it is unmemorable. The user's words: *"it feels too okay
and not okay at the same time."* Treat that as the actual problem. A competent reskin of
the same layout is a failure condition here.

Constraints, and they are the only ones:

- **Dark.** It sits alongside system mixers. A new palette is welcome and encouraged —
  the current Catppuccin Mocha is a borrowed default, not a decision. Typography,
  density, accent colour and motion are all open.
- **Structure is fluid.** The sidebar with "Combine Sinks" and "Split Audio" as separate
  pages is *not* a constraint — it may well be why the app feels flat, since it hides
  half the state at all times. Merging onto one surface is explicitly on the table.
  Anything is, provided every capability below stays reachable.
- **Native titlebar.** The window keeps OS decorations; design no custom frameless
  chrome. The logo and shutdown button now in the GTK `HeaderBar` need a home in-page.
- **800x600 default**, usable at that size, resizable larger.

## Before designing: read the code

Do not design from this document alone. Read `src/pipemix/ui/main_window.py`,
`device_row.py`, `controller.py` and `models.py` first, so the design is grounded in the
real state machine. Note that `SessionState` has six states (`idle`, `starting`,
`active`, `repairing`, `stopping`, `error`) and `BackendHealth` three (`ok`,
`unavailable`, `degraded`). The current UI collapses most of this into one status line —
an opportunity, not a requirement.

## Capability inventory — nothing here may become unreachable

**Outputs**: devices with name, kind (bluetooth/usb/hdmi/builtin), connected state,
battery percentage when reported, per-device volume slider, enable toggle. Plus master volume.

**Sharing**: start/stop action routing enabled devices into one combined sink. Session
state and backend health both visible.

**Presets**: select a saved preset (sets device selection), save current selection under
a name, delete a preset.

**Per-app routing**: per playing application — name, stream ID, dropdown to send it to a
specific output, mute toggle. Plus an empty state and an error state.

**Recovery**: reset audio, and shut down cleanly.

## Behaviours the design must account for

The current code gets these right; they shape the interaction:

- Toggling a device mid-session re-routes **immediately**; toggling while idle only
  stages the selection until the user hits share.
- Hand-toggling any device clears the active preset to "none", since the selection no
  longer matches it.
- The stream list polls every 3 seconds and only re-renders when streams or available
  sinks actually changed — re-rendering every tick fought the user's open dropdowns.
  Whatever the design does here must survive a 3s refresh without flicker or stolen focus.
- Devices appear and disappear live as Bluetooth connects and disconnects.

## Deliverable

Use the `/design` skill to produce a design canvas with **at least two genuinely
different directions** — not one design in two palettes. Each needs:

- the main window at 800x600, idle, with devices listed
- the same window with a session active and apps playing
- at least one unhappy state (backend unavailable, or no devices connected)

Then **stop and ask which direction to build.** Write no Python, no React, no config in
this phase.

---

# Phase 2 — build the approved design

## Hard constraints

1. **Do not modify the backend.** `controller.py`, `services/backend/pactl_backend.py`,
   `services/bluetooth/device_monitor.py`, `services/config/config_manager.py` and
   `models.py` are out of scope. If you think one needs changing, stop and explain why.
2. **`tests/test_core.py` and `tests/test_routing.py` must pass untouched.**
3. **`main.py` keeps its CLI modes.** `--cli`, `--list`, `--share`, `--reset` are
   independent of the GUI. Only the no-args GUI branch changes.
4. The `.deb` stays `Architecture: all` — no compiled artifacts. Frontend builds on the
   dev machine; only `dist/` ships.
5. Everything stays in-process. No REST API, no sidecar.

## Delete

`src/pipemix/ui/main_window.py` (923 lines, ~320 of them a CSS string) and
`src/pipemix/ui/device_row.py`.

## `src/pipemix/ui/api.py` — the JS-callable surface

One `Api` class passed as `js_api=` to `create_window`. Each method returns a
JSON-serializable dict and catches `BackendError`, returning `{"ok": False, "error": ...}`
rather than raising across the bridge.

| JS call | Maps to |
|---|---|
| `toggle_device(id, active)` | see selection note below |
| `set_device_volume(id, vol)` / `set_master_volume(vol)` | same-named `Controller` methods |
| `start_sharing()` / `stop_sharing()` / `reset_audio()` / `clean_orphans()` | same-named |
| `list_streams()` | `controller.backend.list_streams()` |
| `route_stream(id, sink)` | `controller.route_stream` |
| `set_stream_mute(id, mute)` | `controller.backend.set_stream_mute` |
| `presets()` / `save_preset(name, ids)` / `delete_preset(id)` | same-named |
| `last_preset` get/set | the existing property |

**Selection state:** `MainWindow.selected: dict[str, bool]` lives in the view today, and
the view is being deleted. The backend is off-limits, so `Api` owns it now. Port
`_apply_selection`, `_on_device_toggle` and `_clear_preset` faithfully — including "a
live session follows the toggles immediately, an idle one doesn't" and "hand-toggling
clears the preset."

## `src/pipemix/ui/bridge.py` — backend to frontend

Connect the three existing signals and forward each as JSON via `window.evaluate_js(...)`:

- `devices-changed` -> `list[AudioDevice]`
- `state-changed` -> `SessionState`
- `health-changed` -> `BackendStatus`

One `to_json` helper converting dataclasses to dicts and `Enum` members to `.value` (all
three enums are string-valued). Guard `evaluate_js` against the window not being ready.

## `src/pipemix/app.py` — rewrite

Replace `Gtk.Application` with pywebview startup, preserving the ordering guarantee:
build `Controller(PactlBackend(), ConfigManager())`, create the window, connect signals,
and only then call `controller.start()` — it blocks on pactl/BlueZ and its first
`devices-changed` emit needs a listener already attached. Use pywebview's startup
callback rather than `GLib.idle_add`. On teardown call `controller.stop()` so routing is
unwound before exit.

If `PIPEMIX_DEV=1`, point the window at the Vite dev server (`http://localhost:5173`)
instead of the built files, so UI iteration gets hot reload against the real backend.

**Verify before writing the frontend:** start with `webview.start(gui='gtk')` and confirm
`GLib.timeout_add` still fires and BlueZ connect/disconnect still arrives. This is the
one load-bearing assumption in the migration. If it fails, stop and report — the whole
approach needs rethinking.

## `frontend/` — Vite + React + TypeScript

Build the approved design. Hand-write a `types.ts` mirroring `AudioDevice`, `DeviceKind`,
`SessionState`, `BackendHealth`, `BackendStatus` and the `list_streams()` dict shape
(`id`, `name`, `sink`, `mute`). Do not generate types from Python.

Keep the 3-second stream poll and its change-signature dedupe.

## Local HTTP server — decide deliberately

pywebview auto-starts a bottle HTTP server on `127.0.0.1` (random port under the default
`private_mode=True`) whenever the window loads local files, because Vite's default ESM
output is CORS-blocked over `file://`. Its asset route is unauthenticated, though it only
serves frontend files already shipped in the `.deb`.

Default to this — it is loopback-only, and the GTK backend routes `js_api` over the
native WebKit bridge rather than HTTP, so the audio backend is never socket-reachable.

Two rules if you keep it: do not set `private_mode=False` without saying so (it pins the
port to a fixed `42001`), and never pass a `host` that makes it non-loopback.

If the user prefers zero open sockets, `vite-plugin-singlefile` inlines the bundle into
one HTML file that loads over `file://` and no server starts. Don't do both.

## `build_deb.py`

Copy `frontend/dist/` into `/usr/share/pipemix/web/`. Update the control file to
`Depends: python3, python3-gi, python3-webview, pulseaudio-utils` (drop
`gir1.2-gtk-4.0`). Keep `Architecture: all`, the launcher wrapper, the `.desktop` entry
and the icon handling. Resolve the web assets path the way `main_window.py` resolved the
logo: local dev path first, then the installed path.

## Do not build

No state management library — three signals and a poll is `useState` territory. No
component library, no router, no IPC abstraction layer, no frontend test framework, no
light/dark toggle (the app is dark), no i18n, no build watcher in `build_deb.py`.

## Verification

1. `python -m pytest tests/` — unchanged, passing.
2. App launches, lists real devices, and a Bluetooth connect/disconnect updates the UI
   live. This proves the GLib loop survived.
3. Enabling two devices creates a combined sink: `pactl list short modules` shows a
   `pipemix_*` module, and it is gone after stop.
4. `python build_deb.py`, install the `.deb` clean, launch it.

Report anything you could not verify rather than assuming it works.
