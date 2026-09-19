# Contributing to PipeMix

Thanks for taking a look. Bug reports, small fixes and new outputs support are
all welcome.

## Reporting a bug

Audio routing bugs are usually environment-specific, so please include:

- Your distro, and `pipewire --version`
- The output of `pipemix --list`
- The device(s) involved — Bluetooth, USB, HDMI, built-in
- `~/.local/share/pipemix/pipemix.log` after reproducing with `pipemix --debug`

If it involves Bluetooth, say whether the device was connected before or after
PipeMix started, and whether it dropped out mid-session.

## Setting up

```sh
git clone https://github.com/gaurav-066/pipemix.git
cd pipemix
python3 -m venv .venv --system-site-packages   # PyGObject comes from the system
source .venv/bin/activate
pip install -e .
npm --prefix frontend install
```

The venv needs `--system-site-packages`; PyGObject is not installed from pip.

Run it with a hot-reloading frontend:

```sh
npm --prefix frontend run dev        # one terminal
PIPEMIX_DEV=1 python3 -m pipemix.main
```

Build the `.deb`:

```sh
npm --prefix frontend run build
python3 build_deb.py                 # output in build/
```

## Tests

```sh
python3 -m pytest tests/ -q
```

The suite mocks `PactlBackend` and stubs `gi.repository`, so it runs headless
with no PipeWire and no display. Keep it that way — a test that needs real
audio hardware can't run on anyone else's machine. If you add a test file that
imports `controller.py`, copy the GObject stubbing pattern from
`tests/test_routing.py`.

Anything touching routing, presets or the Bluetooth reconnect path should come
with a test. UI-only changes usually don't need one.

## Things that will bite you

- **GTK 3 only.** pywebview's GTK backend pins GTK 3 and WebKit2 4.1. Never
  `gi.require_version('Gtk', '4.0')`.
- **Never call `evaluate_js` from the main thread.** It queues work on the GLib
  main loop and then blocks on a semaphore, so calling it from that same thread
  deadlocks the app permanently. Every push to the page goes through the
  `Bridge` worker queue.
- **Only `PactlBackend` shells out to `pactl`.** If you need a new pactl call,
  add a method there rather than running it from the controller or the UI.
- **Serialize routing changes.** The Controller's `_lock` guards against the
  pywebview thread and the GTK main thread (BlueZ events) racing. Use the
  `@locked` decorator on methods that change session routing.
- **Bluetooth devices are keyed by MAC**, not by sink name — sink names change
  every reconnect. `sink_to_mac` / `path_to_mac` in `models.py` convert.
- **Don't reach for `module-combine-sink`.** It was tried; it has a fixed slave
  list and silently drops outputs when devices come and go. Independent
  loopbacks are deliberate.

## Pull requests

- One change per PR, and a branch off `main`.
- Say what you tested it on — the hardware matters here more than usual.
- Frontend changes: run `npm --prefix frontend run build` to make sure `tsc`
  is happy. Don't commit `frontend/dist/`.
- Match the surrounding style. There's no formatter or linter configured, and
  adding one isn't a small change — raise it as an issue first.

## License

By contributing you agree your work is licensed under GPL-3.0-or-later, the
same as the rest of the project.
