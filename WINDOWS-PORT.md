# PipeMix on Windows — port plan

Target: Windows 10 1809+ and Windows 11.

## Decisions taken

| Area | Choice |
|---|---|
| Audio engine | Both, auto-detected: VB-CABLE as hub when present, leader+mirror when not |
| Code layout | `src/pipemix/windows/` in this repo; Linux tree untouched |
| Audio stack | Raw WASAPI through `comtypes`, hand-declared vtables |
| Per-app routing | Full parity via the undocumented `IAudioPolicyConfigFactory` |
| Leader policy | Auto-elect; re-elect a survivor and rebuild if the leader drops |
| Bluetooth endpoints | Shown as MMDevice enumerates them — no grouping, no filtering |
| Packaging | PyInstaller one-dir inside Inno Setup; detect VB-CABLE and guide |

## What Windows changes, and what it doesn't

The Controller's state machine survives intact. What changes underneath it:

- **No `pactl`.** Every backend method gets a WASAPI/COM equivalent with the same
  signature, so `Controller` keeps calling the same eight or so methods.
- **No BlueZ.** `IMMNotificationClient` reports every endpoint arriving and
  leaving, Bluetooth or not. The whole MAC layer goes away: `sink_to_mac`,
  `path_to_mac`, `resolve_bt_sinks`, and the `SINK_TRIES`/`SINK_WAIT_MS` retry
  chain all exist because a PipeWire sink name changes per session. A Windows
  endpoint ID is stable across reconnects, so `AudioDevice.id` *is*
  `AudioDevice.sink` and the two spellings collapse into one.
- **No GLib.** No `GObject` signals, no `GLib.timeout_add`, no `GLib.MainLoop`.
- **No kernel modules to leak.** `clean_orphans` has nothing to find. It is
  replaced by a different recovery problem — see Phase 6.
- **Clock drift is now ours.** `module-loopback` resampled adaptively to keep
  each leg in step. Raw WASAPI does not; every endpoint runs on its own clock.
  This is the one genuinely new piece of engineering. See Phase 2.

## Code layout

Shared verbatim, no changes beyond one field and one path:

```
src/pipemix/models.py                        AudioDevice, VirtualSink, SharingSession
src/pipemix/ui/api.py                        pure Python, no GLib — shared as-is
src/pipemix/ui/bridge.py                     queue + worker, already thread-safe
src/pipemix/services/backend/__init__.py     + an `engine` field on BackendStatus
src/pipemix/services/config/config_manager.py  + platform-aware default path
frontend/                                    one build, both platforms
```

Forked, because each one imports `gi`:

```
controller.py  →  windows/controller.py
app.py         →  windows/app.py
main.py        →  windows/main.py
```

New:

```
src/pipemix/windows/
  backend.py              WasapiBackend — same surface as PactlBackend
  controller.py           the fork: Signal instead of GObject, threading.Timer instead of GLib
  app.py                  pywebview window, EdgeChromium
  main.py                 CLI args, logging, GUI/console dispatch
  signal.py               ~20 lines: connect() / emit()
  wasapi/
    com.py                GUIDs, PROPERTYKEYs, hand-declared vtables
    devices.py            endpoint enumeration → AudioDevice
    notify.py             IMMNotificationClient → connect/disconnect callbacks
    engine.py             the fan-out: one capture source, N render legs
    policy.py             IPolicyConfig (default device) + IAudioPolicyConfigFactory (per-app)
    sessions.py           IAudioSessionManager2 → the Apps tab
    volume.py             IAudioEndpointVolume per endpoint
```

`src/pipemix/__main__.py` gains a three-line dispatch on `sys.platform` so
`python -m pipemix` works either side. `pyproject.toml` keeps
`pipemix = "pipemix.main:main"` and the PyInstaller spec targets
`pipemix.windows.main:main` directly.

---

## Phase 0 — scaffolding

Create the package, add the platform dispatch, make `ConfigManager` resolve
`%APPDATA%\PipeMix\config.json` on Windows and logs to
`%LOCALAPPDATA%\PipeMix\logs\`. Add `engine: str = "native"` to `BackendStatus`
so the UI can later tell hub mode from leader mode.

**Done when:** `python3 -m pytest tests/ -q` still passes on Linux, unchanged.

## Phase 1 — enumeration and hotplug

`wasapi/devices.py`: `IMMDeviceEnumerator::EnumAudioEndpoints(eRender,
DEVICE_STATE_ACTIVE)`, then per device `GetId()` and `IPropertyStore` for
`PKEY_Device_FriendlyName`. `DeviceKind` comes from
`DEVPKEY_Device_EnumeratorName` — `BTHENUM`/`BTHHFENUM` → BLUETOOTH, `USB` →
USB, `HDAUDIO` + `PKEY_AudioEndpoint_FormFactor` of
`DigitalAudioDisplayDevice` → HDMI, else BUILTIN.

A Bluetooth headset therefore appears twice, as "Headphones (Stereo)" and
"Headset (Hands-Free)", both kind BLUETOOTH. That is deliberate — both rows are
shown exactly as Windows reports them.

`wasapi/notify.py`: register an `IMMNotificationClient` and translate
`OnDeviceAdded` / `OnDeviceRemoved` / `OnDeviceStateChanged` into the
`on_connect(id)` / `on_disconnect(id)` callbacks the Controller already expects
from `DeviceMonitor`. Callbacks arrive on an arbitrary MTA thread and must not
block, so they push onto a queue that a worker drains.

An endpoint goes ACTIVE slightly before `IAudioClient::Initialize` will succeed
on it, so keep a short bounded retry there — a much smaller version of the
`_resolve_retry` chain it replaces.

**Done when:** `pipemix --list` on Windows prints every output, and plugging a
headset in or out logs a connect/disconnect within a second.

## Phase 2 — the fan-out engine

The core, and the only part with no Linux equivalent to copy.

`wasapi/engine.py` runs one worker thread:

- **Capture source.** Either `IAudioClient` on the CABLE Output *capture*
  endpoint (hub mode), or on a *render* endpoint with
  `AUDCLNT_STREAMFLAGS_LOOPBACK` (leader mode). Event-driven, via
  `AUDCLNT_STREAMFLAGS_EVENTCALLBACK` and `SetEventHandle`.
- **Legs.** One `IAudioRenderClient` per selected output, each opened shared-mode
  with `AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY`
  so Windows handles the rate and channel conversion. That is what makes
  "Bluetooth at 48k, speakers at 44.1k" a non-problem.
- **The loop.** Wait on the capture event → `GetBuffer` → for each leg,
  `GetCurrentPadding`, `GetBuffer`, memcpy with the leg's gain applied,
  `ReleaseBuffer`. No allocation inside the loop: preallocated numpy views only.
- **Adding and dropping a leg** mutates the leg list under a lock; the loop picks
  it up on the next cycle. This is `set_legs`, and like the Linux version it
  touches only what changed.

**Clock drift.** Each endpoint free-runs on its own clock, so a leg slowly
starves or backs up. Each leg gets a ring buffer with a target fill level; when
fill drifts past roughly ±20 ms, drop or duplicate a few milliseconds. Inaudible
on music, and about thirty lines. Marked with a `ponytail:` comment naming the
ceiling — the upgrade path is an adaptive resampler driven by
`IAudioClock::GetPosition`, which is what `module-loopback` does.

The engine is runnable on its own, before any of the app exists:
`python -m pipemix.windows.wasapi.engine --from <id> --to <id>,<id>`.

**Done when:** audio playing to one device comes out of three, stays in step over
a twenty-minute run, and toggling a leg does not interrupt the others.

## Phase 3 — backend and controller

`windows/backend.py` implements `PactlBackend`'s surface: `health`,
`list_outputs`, `list_streams`, `move_streams`, `move_stream`,
`set_stream_mute`, `create_sink`, `set_legs`, `destroy_sink`, `find_orphans`,
`get_volume`, `set_volume`, `set_mute`, `get_default`, `set_default`.

- `health()` probes for a VB-CABLE pair and reports `engine="hub"` or
  `engine="leader"`, with a message the UI surfaces.
- `create_sink()` starts the engine. In hub mode it sets CABLE Input as the
  default via `IPolicyConfig::SetDefaultEndpoint` for all three roles and
  captures CABLE Output. In leader mode it elects a leader — the current Windows
  default if it is among the selected outputs, else the first connected one —
  makes it the default, and loopback-captures it.
- `VirtualSink.module` becomes an engine handle rather than a module id; `legs`
  stays a dict keyed by endpoint id. The dataclass does not change.
- `set_default` wraps `IPolicyConfig`. Undocumented but stable since Vista, and
  it is what every volume utility on Windows uses.

Which engine is in use is decided at session start and re-evaluated at the next
start. A cable appearing mid-session does not migrate a running session.

`windows/controller.py` is `controller.py` with three substitutions —
`GObject.Object`/`emit` → the `Signal` class, `GLib.timeout_add` →
`threading.Timer`, `DeviceMonitor` → the notification client — plus one addition:
leader re-election. When `_on_disconnect` fires for the current leader in leader
mode, elect a survivor, set it default, rebuild the legs. Playback gaps for
roughly 200 ms. In hub mode this path is untouched, because the hub cannot
disconnect.

The `_lock` RLock, `@locked`, `_retarget`, `_prepare`, `_adopt`, `_hub_level`
and the solo-device volume rule all carry over unchanged.

**Done when:** `pipemix --cli` on Windows shares, stops, survives a headset
disconnect and reconnect, in both engines.

## Phase 4 — GUI

`windows/app.py` is `app.py` minus `gui="gtk"` — pywebview picks EdgeChromium.
`Bridge` needs no change; its queue-and-worker design is not GLib-specific and
stays the safe choice given notification callbacks arrive on COM threads.

Frontend additions, small:

- A banner when `health.engine == "leader"`: "Running in mirror mode — outputs
  may drift up to 50 ms apart. Install VB-CABLE for synced output." with a link.
- A "Primary" marker on the elected leader's row in leader mode.

**Done when:** `PIPEMIX_DEV=1 python -m pipemix` opens the window on Windows and
every control works.

## Phase 5 — volume and the Apps tab

**Volume.** `IAudioEndpointVolume::SetMasterVolumeLevelScalar` and `SetMute` per
endpoint, matching `pactl set-sink-volume` and moving the Windows slider as
users expect.

Master volume is the open one. In leader mode it has to be the leader's endpoint
volume, since the leader plays through the OS path and no software gain of ours
can reach it. In hub mode, whether CABLE Input's endpoint volume affects what we
capture from CABLE Output is driver behaviour worth measuring rather than
assuming. Plan: endpoint volume first, and if it turns out not to pass through,
fall back to a software gain in the fan-out. The engine carries a gain knob
either way, so this is a one-line switch, not a redesign.

**Apps tab.** `IAudioSessionManager2::GetSessionEnumerator` per active render
endpoint, deduped by PID — sessions are per-endpoint, so enumerating only the
default device misses apps playing elsewhere. Display name from
`IAudioSessionControl2::GetDisplayName`, falling back to the process image name.
`ISimpleAudioVolume` gives per-app volume and mute.

Moving an app uses `IAudioPolicyConfigFactory`, obtained through
`RoGetActivationFactory("Windows.Media.Internal.AudioPolicyConfig")`, then
`SetPersistedDefaultAudioEndpoint(pid, eRender, eMultimedia, deviceId)`. The id
must be in the `\\?\SWD#MMDEVAPI#<endpoint id>#{e6327cad-...}` form, not the raw
endpoint id.

Two caveats worth knowing before building the UI around it. The vtable layout
differs between Windows 10 and Windows 11, so probe at runtime and degrade to
list-and-volume-only if neither layout answers. And unlike
`pactl move-sink-input`, which moves a live stream, this sets a persisted
preference that some applications only pick up when they next open an audio
stream — so the tab should say so rather than appear broken.

**Done when:** the Apps tab lists, mutes, sets volume, and routes on both Win10
and Win11.

## Phase 6 — recovery and packaging

**Recovery.** Nothing leaks, but a crash leaves the default output pointing at
CABLE Input, and the user hears nothing until they change it back by hand. So
`prev_default` is persisted to config at session start and cleared on a clean
stop. On startup, a leftover `prev_default` means the last run died — restore it.
That is what replaces `clean_orphans`.

**Packaging.** PyInstaller one-dir, wrapped by Inno Setup, with the WebView2
evergreen bootstrapper chained for the Win10 machines that lack it. Start Menu
entry and a proper uninstaller.

Hand-declaring the COM vtables rather than using `comtypes.client.GetModule`
pays off here: `GetModule` generates wrappers into a cache at runtime, which is
the classic way comtypes breaks inside a frozen app.

VB-CABLE is detected, not bundled — VB-Audio's licence forbids redistribution
without a written agreement. No cable means leader mode plus the banner, so the
app is useful on first launch either way. If that agreement is ever obtained,
chaining their installer is a change to the Inno script alone.

`build_win.py` mirrors `build_deb.py`: read the version from `pyproject.toml`,
refuse to build without `frontend/dist`, emit `build/pipemix-setup.exe`.

## Tests

Logic that does not touch COM runs on the existing Linux CI — leader election,
enumerator-name → `DeviceKind` mapping, the ring-buffer drift arithmetic,
`WasapiBackend` against a faked engine. Everything COM-shaped needs a
`windows-latest` job. Follow the stub pattern in `test_routing.py`: the Windows
controller tests stub the notification client the same way the Linux ones stub
GObject.

## Risks, ranked

1. **Clock drift** (Phase 2). The only part with no reference implementation to
   copy. Mitigated by the ring buffer, upgradeable to adaptive SRC.
2. **`IAudioPolicyConfigFactory` vtable variance** (Phase 5). Undocumented, two
   known layouts, could change on a Windows update. Probe and degrade.
3. **Persisted-not-live app routing** (Phase 5). A behavioural difference from
   Linux that the UI has to be honest about.
4. **COM apartments** (Phase 1). Notification callbacks land on MTA threads.
   `CoInitializeEx` per thread, queue everything, never call back into an
   object across apartments.
5. **`IPolicyConfig`** (Phase 3). Undocumented but stable for fifteen years and
   used by every Windows volume utility. Low.

## Defaulted without asking — say if any is wrong

- Presets and device names do not port between Linux and Windows configs;
  device IDs are a different namespace.
- `--cli`, `--list`, `--share` and `--refresh` are all kept on Windows.
- Drift correction drops and duplicates blocks rather than resampling.
- Bluetooth battery is shown only where Windows exposes it as a device
  property, and the row is hidden otherwise.
