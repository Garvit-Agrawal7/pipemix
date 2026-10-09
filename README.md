# PipeMix

Play the same audio through as many speakers and headphones as you like, all at
once, on Linux and Windows.

<img width="880" height="660" alt="outputs" src="https://github.com/user-attachments/assets/b513daf2-a827-416c-a508-74c7d9da718b" />

Neither Linux nor Windows lets you do this from its sound settings. PipeMix
gives it a window: tick the outputs you want, drag a row to set its level, and
save the combination as a preset. It builds on each system's own audio layer:
PipeWire on Linux, WASAPI on Windows.

## What it does

- **Share to any number of outputs at once.** Bluetooth, USB, HDMI, built-in —
  anything the system lists as an output.
- **A fader per device.** Each row *is* its own volume control; drag it
  anywhere. A master fader rides on top.
- **Presets.** Save a set of outputs as "Whole House" or "Desk Only" and switch
  between them in a click.
- **Per-app routing.** Send Spotify to the whole house and keep a video call on
  the laptop. Anything you route by hand stays put when the outputs change.
- **Survives Bluetooth dropouts.** When a device wanders out of range the rest
  keep playing, and it rejoins on its own when it comes back.
- **Toggle outputs mid-session.** Adding or removing a device doesn't interrupt
  the ones that are staying.

<img width="880" height="660" alt="apps" src="https://github.com/user-attachments/assets/1f1bbe2f-310d-40e7-81e7-7835064bfb8f" />

## Install

Download the latest build from the [releases page][releases].

**Linux.** Needs PipeWire, which most current distros use by default. Plain
PulseAudio isn't supported; PipeMix will tell you at startup if PipeWire isn't
there.

```sh
sudo apt install ./pipemix-*-linux-x64.deb
```

Then launch **PipeMix** from your applications menu, or run `pipemix`.

**Windows 10 or 11.** Run `pipemix-*-windows-x64.exe`. On the last page, leave
**Install VB virtual audio driver** ticked. That's
[VB-CABLE](https://www.vb-cable.com/), VB-Audio's virtual audio driver, bundled
with the installer; it needs administrator approval and may need a reboot. If
you skip it, there's a Start Menu shortcut to install it later. PipeMix works
without it, but per-app routing needs it. VB-CABLE is donationware, and all
participations are welcome.

[releases]: https://github.com/Garvit-Agrawal7/pipemix/releases

## Technical Details

### How it works

#### Linux

PipeMix creates one **hub sink** when a session starts and makes it the system
default, then runs a **`module-loopback`** from that hub out to each chosen
device. Staging or unstaging an output loads or unloads a single loopback; the
hub and every other output are left alone, which is why toggling a device
doesn't interrupt the rest.

The obvious tool for this is `module-combine-sink`, and PipeMix used it at
first. Its slave list is fixed at load time, so every toggle meant destroying
and rebuilding the whole thing — and under that churn it silently stops
attaching one of its slaves. It reports success, the sink looks healthy, and one
device just goes quiet. Measured against a laptop speaker and a Bluetooth
headset, it attached both outputs once in eight attempts. Independent loopbacks
have no such failure mode.

Everything runs in one process:

| Piece | Job |
| --- | --- |
| `linux/controller.py` | The state machine. Owns the session, reacts to Bluetooth events, drives the backend. |
| `linux/pactl_backend.py` | Every `pactl` call in the app. Nothing else shells out. |
| `linux/bluetooth.py` | BlueZ over D-Bus — connect and disconnect. |
| `api.py`, `bridge.py` | The JS-callable surface, and Controller signals pushed to the page. |
| `frontend/` | React + TypeScript, served to a pywebview window. |

#### Windows

Windows has no PipeWire, so PipeMix does the fan-out itself over WASAPI: one
capture source feeds a shared-mode render stream per output, and Windows
converts the format for each device. With VB-CABLE installed, apps play into
the silent "CABLE Input" and PipeMix captures each playing app on its own; that
is also what makes per-app routing work. Without it, one chosen output is the
**leader**: it plays normally and the rest copy it. If the leader disappears,
a surviving output takes over. Devices drift apart slowly, so each output drops
a few frames when it falls too far behind rather than resampling.

The code lives under `src/pipemix/windows/`; `api.py`, `bridge.py`,
`config.py`, `models.py` and the frontend are shared with Linux.

### Building from source

**Linux** needs PipeWire with its PulseAudio compatibility layer (`pactl`),
BlueZ for Bluetooth device and battery information, Python 3.12+, PyGObject,
pywebview, and npm for the frontend.

```sh
git clone https://github.com/Garvit-Agrawal7/pipemix.git
cd pipemix
npm --prefix frontend install && npm --prefix frontend run build
python3 build_deb.py
sudo apt install ./build/pipemix.deb
```

The frontend has to be built first — `build_deb.py` refuses to package without
it.

**Windows:** build the frontend first, install the Python dependencies and
PyInstaller, install Inno Setup 6, and put `VBCABLE_Driver_Pack45.zip` in the
repo root, then run `python build_win.py`. The installer lands as
`pipemix-setup.exe`.

### Command line

The GUI is the default, but everything is reachable from a terminal:

```sh
pipemix                 # launch the window
pipemix --cli           # interactive text dashboard
pipemix --list          # list detected outputs and their IDs
pipemix --share IDS     # share to a comma-separated list of device IDs
pipemix --debug         # verbose logging
```

From a source checkout, `python -m pipemix.main` takes the same flags on
either platform.

Presets and device names live in `~/.config/pipemix/config.json`
(`%APPDATA%\PipeMix\config.json` on Windows). Logs go to
`~/.local/share/pipemix/pipemix.log` (`%LOCALAPPDATA%\PipeMix\logs\pipemix.log`).

## Contributing

Bug reports and fixes are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) covers
dev setup on both platforms, tests, and the things that will bite you.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).
