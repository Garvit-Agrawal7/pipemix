"""The fan-out: one capture source, N render legs.

This is the part with no Linux counterpart. PipeWire gave us a null sink and a
`module-loopback` per output, and resampled each leg adaptively to keep it in
step. Raw WASAPI gives us none of that: we capture from one endpoint and write
the same frames to every other one, and every endpoint runs on its own clock.

Two capture sources, one code path:

* **hub mode** — the source is a real capture endpoint (VB-CABLE's "CABLE
  Output"), fed by everything Windows plays into "CABLE Input".
* **leader mode** — the source is a *render* endpoint opened with
  `AUDCLNT_STREAMFLAGS_LOOPBACK`, so we mirror whatever it plays.

Which one applies is decided by the endpoint's data flow, not by a flag the
caller has to get right.

Every leg is opened with the *source's* mix format plus `AUTOCONVERTPCM`, so
Windows resamples and remixes into whatever each endpoint actually wants. That
is what makes "Bluetooth at 48k, speakers at 44.1k" a non-problem, and it is
why the pump can memcpy rather than convert.

Runnable on its own, before any of the app exists:

    python -m pipemix.windows.wasapi.engine --from <id> --to <id>,<id>
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time

log = logging.getLogger(__name__)

POLL_MS       = 5    # how often the pump looks at the source and the legs
BUFFER_MS     = 200  # endpoint buffer we ask Windows for, source and legs alike
DRIFT_MS      = 20   # how far a leg may fall behind before we drop frames
DRIFT_KEEP_MS = 5    # what we leave queued after dropping


class _Leg:
    """One output endpoint, and the frames queued for it.

    The queue is the drift signal. A leg whose clock runs slower than the
    source's accepts fewer frames per cycle than arrive, so its queue grows; a
    leg running faster drains its queue and `write` simply has nothing to give
    it, which costs a gap of silence rather than a stall.
    """

    def __init__(self, device_id: str, client, render, frames: int, bpf: int, rate: int) -> None:
        self.id     = device_id
        self.client = client
        self.render = render
        self.frames = frames  # the endpoint's buffer size, in frames
        self.bpf    = bpf     # bytes per frame
        self.fifo   = bytearray()
        self.high   = int(rate * DRIFT_MS / 1000) * bpf
        self.keep   = int(rate * DRIFT_KEEP_MS / 1000) * bpf

    def push(self, data: bytes) -> None:
        self.fifo += data
        if len(self.fifo) > self.high:
            # ponytail: drops a block outright when a leg drifts behind. Costs
            # at most a 15 ms skip, and only on a leg whose clock is off. The
            # upgrade path is an adaptive resampler driven by
            # IAudioClock::GetPosition, which is what module-loopback does.
            dropped = len(self.fifo) - self.keep
            del self.fifo[:dropped]
            log.debug("[leg %s] drift: dropped %d frames", self.id, dropped // self.bpf)

    def write(self) -> None:
        free = self.frames - self.client.GetCurrentPadding()
        n = min(free, len(self.fifo) // self.bpf)
        if n <= 0:
            return
        nbytes = n * self.bpf
        ctypes.memmove(self.render.GetBuffer(n), bytes(self.fifo[:nbytes]), nbytes)
        self.render.ReleaseBuffer(n, 0)
        del self.fifo[:nbytes]


class Engine:
    """Mirrors `source_id` onto whatever legs are set, on its own pump thread.

    `set_legs` is safe to call from any thread and any apartment: it only
    records what is wanted. The pump thread opens and closes the endpoints
    itself, so every COM pointer stays in the apartment that created it.
    """

    def __init__(self, source_id: str) -> None:
        self.source_id = source_id
        self.error: Exception | None = None

        self._wanted: set[str] = set()
        self._legs: dict[str, _Leg] = {}
        self._lock   = threading.Lock()
        self._stop   = threading.Event()
        self._ready  = threading.Event()
        self._thread: threading.Thread | None = None

        self._enumerator = None
        self._client  = None
        self._capture = None
        self._fmt     = None
        self._bpf     = 0
        self._rate    = 0

    # -- public ---------------------------------------------------------

    def start(self) -> None:
        """Blocks until the source is open, and raises if it could not be."""
        self._thread = threading.Thread(target=self._run, name="wasapi-engine", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)
        if self.error:
            raise self.error

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None

    def set_legs(self, device_ids) -> None:
        """Replace the set of outputs. Only the difference is opened or closed."""
        with self._lock:
            self._wanted = set(device_ids)

    @property
    def legs(self) -> list[str]:
        return sorted(self._legs)

    # -- pump thread ----------------------------------------------------

    def _run(self) -> None:
        import comtypes

        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            try:
                self._open_source()
            except Exception as e:
                self.error = e
                log.error("Engine failed to open source %s: %s", self.source_id, e)
                return
            finally:
                self._ready.set()

            poll = POLL_MS / 1000
            while not self._stop.is_set():
                try:
                    self._reconcile()
                    self._pump()
                except Exception:
                    log.exception("Engine pump error")
                time.sleep(poll)
        finally:
            self._close()
            comtypes.CoUninitialize()

    def _device(self, device_id: str):
        from pycaw.utils import AudioUtilities

        if self._enumerator is None:
            self._enumerator = AudioUtilities.GetDeviceEnumerator()
        return self._enumerator.GetDevice(device_id)

    def _open_source(self) -> None:
        import comtypes
        from pycaw.api.audioclient import IAudioClient
        from pycaw.api.mmdeviceapi import IMMEndpoint
        from pycaw.constants import EDataFlow

        from pipemix.windows.wasapi.com import (
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK,
            REFTIMES_PER_SEC,
            IAudioCaptureClient,
        )

        dev = self._device(self.source_id)
        # A render endpoint has to be mirrored; a capture endpoint already is
        # the hub's output and is read directly.
        is_render = dev.QueryInterface(IMMEndpoint).GetDataFlow() == EDataFlow.eRender.value
        flags = AUDCLNT_STREAMFLAGS_LOOPBACK if is_render else 0

        self._client = dev.Activate(
            IAudioClient._iid_, comtypes.CLSCTX_ALL, None
        ).QueryInterface(IAudioClient)
        self._fmt  = self._client.GetMixFormat()
        self._bpf  = self._fmt.contents.nBlockAlign
        self._rate = self._fmt.contents.nSamplesPerSec
        self._client.Initialize(
            AUDCLNT_SHAREMODE_SHARED, flags,
            BUFFER_MS * REFTIMES_PER_SEC // 1000, 0, self._fmt, None,
        )
        self._capture = self._client.GetService(
            IAudioCaptureClient._iid_
        ).QueryInterface(IAudioCaptureClient)
        self._client.Start()
        log.info(
            "Engine source open: %s (%s, %d Hz, %d ch)",
            self.source_id, "loopback" if is_render else "capture",
            self._rate, self._fmt.contents.nChannels,
        )

    def _open_leg(self, device_id: str) -> _Leg:
        import comtypes
        from pycaw.api.audioclient import IAudioClient

        from pipemix.windows.wasapi.com import (
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
            AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
            REFTIMES_PER_SEC,
            IAudioRenderClient,
        )

        client = self._device(device_id).Activate(
            IAudioClient._iid_, comtypes.CLSCTX_ALL, None
        ).QueryInterface(IAudioClient)
        client.Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
            BUFFER_MS * REFTIMES_PER_SEC // 1000, 0, self._fmt, None,
        )
        render = client.GetService(IAudioRenderClient._iid_).QueryInterface(IAudioRenderClient)
        client.Start()
        log.info("Engine leg open: %s", device_id)
        return _Leg(device_id, client, render, client.GetBufferSize(), self._bpf, self._rate)

    def _reconcile(self) -> None:
        with self._lock:
            wanted = set(self._wanted)

        for device_id in wanted - set(self._legs):
            try:
                self._legs[device_id] = self._open_leg(device_id)
            except Exception as e:
                # One dead endpoint must not take the others down, and must not
                # be retried every 5 ms.
                log.error("Engine could not open leg %s: %s", device_id, e)
                with self._lock:
                    self._wanted.discard(device_id)

        for device_id in set(self._legs) - wanted:
            self._close_leg(self._legs.pop(device_id))

    def _close_leg(self, leg: _Leg) -> None:
        try:
            leg.client.Stop()
        except Exception:
            pass
        log.info("Engine leg closed: %s", leg.id)

    def _pump(self) -> None:
        from pipemix.windows.wasapi.com import AUDCLNT_BUFFERFLAGS_SILENT

        while self._capture.GetNextPacketSize():
            ptr, frames, flags, _pos, _qpc = self._capture.GetBuffer()
            if frames:
                nbytes = frames * self._bpf
                data = (
                    bytes(nbytes) if flags & AUDCLNT_BUFFERFLAGS_SILENT
                    else ctypes.string_at(ptr, nbytes)
                )
                for leg in self._legs.values():
                    leg.push(data)
            self._capture.ReleaseBuffer(frames)

        for leg in self._legs.values():
            leg.write()

    def _close(self) -> None:
        for leg in list(self._legs.values()):
            self._close_leg(leg)
        self._legs.clear()
        if self._client is not None:
            try:
                self._client.Stop()
            except Exception:
                pass
        self._capture = None
        self._client = None
        log.info("Engine stopped.")


if __name__ == "__main__":
    import argparse

    import comtypes

    from pipemix.windows.wasapi.devices import default_output_id, list_outputs

    ap = argparse.ArgumentParser(description="Mirror one endpoint onto several.")
    ap.add_argument("--from", dest="source", help="source endpoint id (default: current output)")
    ap.add_argument("--to", help="comma-separated destination endpoint ids")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s"
    )
    comtypes.CoInitialize()

    if not args.to:
        print("Pick destinations with --to:")
        for d in list_outputs():
            print(f"  {d.name}\n    {d.id}")
        raise SystemExit(0)

    source = args.source or default_output_id()
    engine = Engine(source)
    engine.start()
    engine.set_legs([i for i in args.to.split(",") if i])
    print(f"Mirroring {source}\n  -> {args.to}\nCtrl-C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        engine.stop()
