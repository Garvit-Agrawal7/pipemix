# PipeMix

Play the same audio through several outputs at once on Linux — laptop speakers
and a Bluetooth headset together, two Bluetooth speakers in different rooms, or
whatever combination you like — with a volume fader for each one.

<img width="880" height="660" alt="outputs" src="https://github.com/user-attachments/assets/b513daf2-a827-416c-a508-74c7d9da718b" />

PipeWire can do this on its own, but only through config files and `pactl`
incantations. PipeMix gives it a window: tick the outputs you want, drag a row
to set its level, and save the combination as a preset.

## What it does

- **Share to any number of outputs at once.** Bluetooth, USB, HDMI, built-in —
  anything PipeWire exposes as a sink.
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

## Requirements

- **PipeWire** with its PulseAudio compatibility layer (`pactl`)
- **BlueZ**, for Bluetooth device and battery information
- Python 3.12+, PyGObject, and pywebview

Plain PulseAudio is not supported — PipeMix checks for PipeWire at startup and
will tell you if it isn't there.

## Install

Download the `.deb` from the [latest release][releases] and install it:

```sh
sudo dpkg -i pipemix.deb
sudo apt-get install -f          # if any dependencies are missing
```

Then launch **PipeMix** from your applications menu, or run `pipemix`.

[releases]: https://github.com/gaurav-066/pipemix/releases

### Building the package yourself

```sh
git clone https://github.com/gaurav-066/pipemix.git
cd pipemix
npm --prefix frontend install && npm --prefix frontend run build
python3 build_deb.py
sudo dpkg -i build/pipemix.deb
```

The frontend has to be built first — `build_deb.py` refuses to package without
it.

## Command line

The GUI is the default, but everything is reachable from a terminal:

```sh
pipemix                 # launch the window
pipemix --cli           # interactive text dashboard
pipemix --list          # list detected outputs and their IDs
pipemix --share IDS     # share to a comma-separated list of device IDs
pipemix --refresh       # re-scan outputs
pipemix --debug         # verbose logging
```

Presets and device names live in `~/.config/pipemix/config.json`. Logs go to
`~/.local/share/pipemix/pipemix.log`.

## How it works

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
| `controller.py` | The state machine. Owns the session, reacts to Bluetooth events, drives the backend. |
| `services/backend/pactl_backend.py` | Every `pactl` call in the app. Nothing else shells out. |
| `services/bluetooth/device_monitor.py` | BlueZ over D-Bus — connect, disconnect, battery. |
| `ui/api.py`, `ui/bridge.py` | The JS-callable surface, and Controller signals pushed to the page. |
| `frontend/` | React + TypeScript, served to a pywebview window. |

## Development

```sh
python3 -m venv .venv --system-site-packages   # PyGObject comes from the system
source .venv/bin/activate
pip install -e .
npm --prefix frontend install

python3 -m pytest tests/ -q          # the suite runs headless, no PipeWire needed

# Hot-reloading frontend against the real backend:
npm --prefix frontend run dev        # in one terminal
PIPEMIX_DEV=1 python3 -m pipemix.main
```

Two constraints worth knowing before you change anything:

- pywebview's GTK backend pins **GTK 3 and WebKit2 4.1**. Nothing may
  `gi.require_version('Gtk', '4.0')`.
- `evaluate_js` blocks on a semaphore after queueing work on the GLib main
  loop, so calling it *from* the main thread deadlocks the app permanently.
  `Bridge` hands every push to a worker thread for exactly this reason, and
  BlueZ handlers run on that main thread.

## License

GPL-3.0-or-later.
