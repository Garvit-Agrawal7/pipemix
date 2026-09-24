# PipeMix on Windows — port plan

Target: Windows 10 1809+ and Windows 11.

## Decisions taken

| Area | Choice |
|---|---|
| Audio engine | Both, auto-detected: VB-CABLE as hub when present, leader+mirror when not |
| Code layout | `src/pipemix/windows/` in this repo; Linux tree untouched |
| Audio stack | Raw WASAPI through `comtypes`; `pycaw`'s interface declarations, ours only where it has none |
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
    com.py                the four interfaces pycaw does not declare
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

**Done.** `models.py` moved up to `src/pipemix/models.py`, shared by both trees.

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

**Virtual endpoints are not shown.** `PactlBackend.list_outputs` drops sinks
marked `node.virtual` or `device.bus = virtual`, and the first cut of this port
lost that rule, so VB-CABLE's "CABLE Input" and "CABLE In 16ch" turned up as
things a user could share to. Neither is somewhere anyone can hear anything,
and in hub mode CABLE Input is PipeMix's own plumbing: selecting it as an
output would have the engine write into the very cable it captures from, a
feedback loop that builds on itself. Windows has no "is virtual" flag, but a
driver with no hardware bus enumerates under `ROOT`, which real sound cards
never do — that is `is_virtual()`. `list_outputs(include_virtual=True)` exists
for one caller, the VB-CABLE probe, which has to see the cable precisely
because it is one.

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

**Done**, via `python -m pipemix.windows.wasapi.devices` and
`python -m pipemix.windows.wasapi.notify` — `pipemix --list` itself waits for
`windows/main.py` in Phase 3.

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

**Done, verified on hardware** (Win11 26200, VB-CABLE installed, five render
endpoints):

- *hub mode* — source CABLE Output, legs HDMI + two Bluetooth headsets: all
  three received audio, peak 0.366 on each, no queue backlog.
- *leader mode* — source the HDMI monitor by loopback, legs both headsets:
  both received audio, peak 0.037 on each.
- *drift* — a 12-minute three-leg soak held every queue at 0 ms. Nine drops
  per leg, all of them in the first seconds while the legs were opening, and
  none afterwards. Three endpoints on three different clocks, one of them
  HDMI and two Bluetooth, stayed in step for the whole run without the
  correction ever firing in steady state.

Software gain is still not implemented, and Phase 5 did not force the issue —
endpoint volume works per endpoint, so the gain knob stays unbuilt until
something needs it.

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

**Written**, and `pipemix --list` works, which also closes Phase 1's outstanding
acceptance check.

Driving the real stack (Controller + Api + `WasapiBackend`, no window) found a
bug that every unit test had missed, because they all fake the backend:
`create_sink` named the `VirtualSink` `pipemix_<uuid>` the way Linux does. On
Linux that name *is* a real PulseAudio sink, and `Controller.active_sink()`
hands it straight back to `set_default`, `set_volume` and `move_streams`. On
Windows it names nothing, so starting a session died on `E_INVALIDARG` at the
first of those calls. The sink is now named after the endpoint everything plays
into — CABLE Input in hub mode, the leader in leader mode — which is the true
analogue of the Linux null sink, and all three call sites became correct at
once. Pinned by three tests, since nothing else would catch it returning.

**Done, verified on hardware, both engines**, driven through `Api` — the same
entry points the page calls:

- *hub* — five endpoints enumerated, shared to three, all three received audio,
  stop returned to idle and restored the default.
- *leader* (with the cable hidden, which is what a machine without VB-CABLE
  gets) — elected the current default as leader, made it the Windows default,
  loopback-captured it, kept it out of the legs, marked exactly it primary, and
  all three outputs received audio.
- *re-election* — with a real Bluetooth headset switched off mid-session, the
  survivor was elected, made the default, and the legs rebuilt around it, with
  the new leader correctly excluded from them.

That last one first failed, and found the worst bug of the port. See below.

### The apartment bug

`DeviceMonitor` registered its `IMMNotificationClient` on whichever thread
called `start()`. `comtypes.CoInitialize()` puts that thread in a
single-threaded apartment, and COM marshals calls to an object registered from
an STA back onto that one thread, delivering them **only while it pumps a
Windows message loop**. `main.py --cli` blocks on `threading.Event().wait()`
and never pumps. Every hotplug notification therefore queued up and was never
delivered: a headset could disconnect mid-session and nothing noticed, so no
re-election, no leg teardown, nothing.

Confirmed rather than guessed — `CoGetApartmentType` reports the caller as
`MAINSTA` and the monitor's worker as `MTA`. Registration (and unregistration,
which COM requires on the same thread) now happens on that MTA worker, which
removes the message-pump requirement altogether.

Worth noting how nearly this shipped: pywebview pumps messages, so the GUI
would have worked and only `--cli` would have been silently deaf. This is
risk #4 on the list below, and it was real.

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

**Written.** `frontend/` builds clean and `pipemix.windows.app` imports with
pywebview installed. The banner renders from `health.engine`, which already
crossed the bridge for free — `to_json` uses `asdict`, so the `engine` field on
`BackendStatus` needed no plumbing. The Primary marker needed one line in
`api.py`: `getattr(backend, "leader", None)`, so the shared file keeps working
on Linux where no backend has a leader. The banner's green comes from a
`.banner.info` class rather than an inline hex, matching how the rest of the
stylesheet works.

**Done — the window opens and the app works from it.** A real session, driven
by hand: two outputs selected, hub mode engaged through VB-CABLE, both legs
opened, stopped again, and the default restored. The monitor also slept and
woke mid-session, and the hotplug fix below reported both, live, in the GUI.

Getting there took two bugs, both invisible to every test.

### The blank window

`_entry()` resolved the built frontend from `parents[2]`, which is `src/`, not
the repo root. pywebview 6 serves local files through an internal localhost
HTTP server, so a path that does not exist renders as a bare "URL not found"
against localhost — which reads like a dev-server problem and is not one.

**This was not a Windows bug.** `app.py` used to live at `src/pipemix/app.py`,
where `parents[2]` really was the repo root; the structure refactor moved it
down into `linux/` and quietly repointed it at `src/`. The Linux build has the
same break and nobody noticed, because the dev flow sets `PIPEMIX_DEV=1` and
loads vite, so the packaged-frontend path is never exercised. Both files fixed.

Windows now tries `parents[3]` (checkout) then `parents[2]` (a frozen bundle,
where PyInstaller has flattened `src/` away), so one function covers both and
`pipemix.spec`'s layout stays valid. And it raises naming every path it tried
rather than handing pywebview something that does not exist.

### The silent-UI bug

`SignalEmitter` exists for one reason: to be a drop-in for `GObject.Object`, so
`windows/controller.py` can be a substitution of the Linux controller rather
than a rewrite. It was not one. GObject passes the emitting object as the first
argument to every handler — which is why the *shared* `ui/bridge.py` declares
`_on_health(self, _controller, status)` — and `emit` was calling handlers with
the payload alone. Every signal raised:

```
TypeError: Bridge._on_health() missing 1 required positional argument: 'status'
```

The failure mode is the dangerous kind. `emit` catches per-handler exceptions
so one dead listener cannot take a routing operation down, so nothing crashed:
the app started, the window opened, and the **first paint was correct**,
because the page fetches `snapshot()` itself. Every update after that was lost.
A UI that looks right and silently never changes again.

Fixed in `emit`, which repairs all three signals at once, and pinned by tests —
including one shaped exactly like the real `Bridge` handler, since a test that
only checks the emitter's own convention would have passed before the fix too.

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

**Written, and the undocumented half is verified on Win11 26200** — with three
corrections to what this plan assumed:

- The two vtable layouts are *not* interchangeable. Only one IID activates on a
  given build (26200 answers the Win11 IID and refuses the Win10 one), and the
  Win11 interface carries two extra slots — it gained `add`/`remove`
  `_ChatContextChanged` — so `SetPersistedDefaultAudioEndpoint` sits at slot 25
  there and 23 on Win10. Indexing both the same way silently calls the wrong
  method. The slot is now chosen by which IID answered.
- The factory refuses a pid that owns no audio session, with `E_INVALIDARG`.
  That is not a bad device id and must not be reported as one: only processes
  the Apps tab can actually list are routable.
- The same call with a **null** device id clears an app's preference, which is
  how a stopped session puts every app it moved back on the machine default.
  Without it, apps stay pinned to an endpoint the user never chose.

Endpoint volume reads and writes real per-endpoint levels (verified across all
five endpoints, including the clamp at 100).

Session enumeration was checked against sessions deliberately opened on
*non-default* endpoints, which is the case the plan warns about, and finds
them. One consequence of deduping by PID is worth knowing: a process playing
to two endpoints at once — which PipeMix itself does, since the engine holds
the source and every leg — is reported on whichever endpoint enumerates first,
not on all of them. Fine for the Apps tab, where one app means one endpoint,
and PipeMix filters its own pid out anyway.

One trap worth knowing, because it cost real debugging time: a per-app override
changes what `GetDefaultAudioEndpoint` returns **inside that process**. Route
`python.exe` somewhere and every later `default_output_id()` from Python reports
that endpoint, while the machine default is untouched. It reads exactly like
`IPolicyConfig::SetDefaultEndpoint` silently failing, and it is not.

## Phase 6 — recovery and packaging

**Recovery.** Nothing leaks, but a crash leaves the default output pointing at
CABLE Input, and the user hears nothing until they change it back by hand. So
`prev_default` is persisted to config at session start and cleared on a clean
stop. On startup, a leftover `prev_default` means the last run died — restore it.
That is what replaces `clean_orphans`.

**Packaging.** PyInstaller one-dir, wrapped by Inno Setup, with the WebView2
evergreen bootstrapper chained for the Win10 machines that lack it. Start Menu
entry and a proper uninstaller.

Every COM interface here is declared statically, in `pycaw` or in `com.py`,
rather than through `comtypes.client.GetModule` — `GetModule` generates
wrappers into a cache at runtime, which is the classic way comtypes breaks
inside a frozen app.

VB-CABLE is detected, not bundled — VB-Audio's licence forbids redistribution
without a written agreement. No cable means leader mode plus the banner, so the
app is useful on first launch either way. If that agreement is ever obtained,
chaining their installer is a change to the Inno script alone.

`build_win.py` mirrors `build_deb.py`: read the version from `pyproject.toml`,
refuse to build without `frontend/dist`, emit `build/pipemix-setup.exe`.

**Recovery is done and verified on hardware.** Simulating the crash aftermath —
config still naming the pre-session default, Windows default stranded on CABLE
Input — startup restored it and cleared the key; a persisted id for an endpoint
that had since gone was dropped rather than retried on every launch.

**Packaging builds, and the frozen app runs.**

```
build/dist/pipemix/pipemix.exe       windowed — the Start Menu entry
build/dist/pipemix/pipemix-cli.exe   console  — --list / --cli / --share
build/pipemix-setup.exe              the installer
```

Two executables over one shared `COLLECT`: same code, same bundle, only the
Windows subsystem flag differs. That settles the console question — a windowed
exe has nowhere to print, a console exe flashes a black window behind every GUI
launch, and PipeMix needs both, so it ships both rather than picking a side.

`pipemix-cli.exe --list` enumerates real endpoints out of the frozen build,
which is what proves the whole freeze: the lazy `comtypes` imports resolved,
pycaw's submodules came along, and nothing reached for the `GetModule` cache
that would have broken here. The GUI exe runs too — twenty-seven minutes,
through a monitor sleeping and waking, closing clean.

### The logo

`data/icons/pipemix.png` was **a JPEG with a .png extension** (`\xff\xd8\xff\xe0
JFIF`, 1024x1024). Nothing on Windows referenced it at all, and `build_deb.py`
has been installing it into `/usr/share/icons/hicolor/512x512/apps/` — the
wrong format for that tree, at the wrong size for that directory.

Now a real 512x512 PNG, plus a multi-size `pipemix.ico` (16/24/32/48/64/128/
256, so Windows is not left downscaling one 256px image into a mushy 16px
title-bar icon). Converted once and committed, so no build step needs an image
library. Wired into `webview.start(icon=)`, both exes and Inno's
`SetupIconFile`; both built exes carry seven icon images and one group.

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
