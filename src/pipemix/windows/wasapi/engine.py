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
LEG_RETRY_S   = 1.0  # how long a leg that failed to open waits before the next try


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
    """Mirrors `source_id`, or app `pid`, onto whatever legs are set, on its own pump thread.

    `set_legs` is safe to call from any thread and any apartment: it only
    records what is wanted. The pump thread opens and closes the endpoints
    itself, so every COM pointer stays in the apartment that created it.
    """

    def __init__(self, source_id: str | None = None, *, pid: int | None = None) -> None:
        if (source_id is None) == (pid is None):
            raise ValueError("Engine needs exactly one of source_id or pid")
        self.source_id = source_id
        self.pid = pid
        self.error: Exception | None = None

        self._wanted: set[str] = set()
        self._legs: dict[str, _Leg] = {}
        self._retry_at: dict[str, float] = {}  # monotonic time a failed leg may be tried again
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
        name = "wasapi-engine" if self.pid is None else f"wasapi-engine-pid{self.pid}"
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()
        # Longer than the process-loopback activation wait, so its timeout surfaces here.
        self._ready.wait(timeout=10)
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
                log.error(
                    "Engine failed to open source %s: %s",
                    self.source_id if self.pid is None else f"pid {self.pid}", e,
                )
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
        if self.pid is not None:
            return self._open_process()
        import comtypes
        from pycaw.api.audioclient import IAudioClient
        from pycaw.api.mmdeviceapi import IMMEndpoint
        from pycaw.constants import EDataFlow

        from pipemix.windows.wasapi.com import AUDCLNT_STREAMFLAGS_LOOPBACK

        dev = self._device(self.source_id)
        # A render endpoint has to be mirrored; a capture endpoint already is
        # the hub's output and is read directly.
        is_render = dev.QueryInterface(IMMEndpoint).GetDataFlow() == EDataFlow.eRender.value
        flags = AUDCLNT_STREAMFLAGS_LOOPBACK if is_render else 0

        self._client = dev.Activate(
            IAudioClient._iid_, comtypes.CLSCTX_ALL, None
        ).QueryInterface(IAudioClient)
        self._fmt = self._client.GetMixFormat()
        self._start_capture(flags)
        log.info(
            "Engine source open: %s (%s, %d Hz, %d ch)",
            self.source_id, "loopback" if is_render else "capture",
            self._rate, self._fmt.contents.nChannels,
        )

    def _start_capture(self, flags) -> None:
        """Shared Initialize -> GetService -> Start tail for `self._client`/`self._fmt`."""
        from pipemix.windows.wasapi.com import (
            AUDCLNT_SHAREMODE_SHARED,
            REFTIMES_PER_SEC,
            IAudioCaptureClient,
        )

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

    def _open_process(self) -> None:
        """Process loopback of `self.pid`, polled like any other source."""
        from comtypes import COMObject
        from pycaw.api.audioclient import IAudioClient

        from pipemix.windows.wasapi.com import (
            AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
            AUDCLNT_STREAMFLAGS_LOOPBACK,
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
            IActivateAudioInterfaceAsyncOperation,
            IActivateAudioInterfaceCompletionHandler,
            IAgileObject,
            PROPVARIANT_BLOB,
            loopback_format,
            loopback_params,
        )

        class Handler(COMObject):
            _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

            def __init__(self) -> None:
                super().__init__()
                self.done = threading.Event()

            def IActivateAudioInterfaceCompletionHandler_ActivateCompleted(self, this, op):
                self.done.set()
                return 0

        activate = ctypes.windll.Mmdevapi.ActivateAudioInterfaceAsync
        activate.restype = ctypes.HRESULT
        activate.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(type(IAudioClient._iid_)),
            ctypes.POINTER(PROPVARIANT_BLOB),
            ctypes.POINTER(IActivateAudioInterfaceCompletionHandler),
            ctypes.POINTER(ctypes.POINTER(IActivateAudioInterfaceAsyncOperation)),
        ]
        params, blob = loopback_params(self.pid)  # params must outlive the call
        handler = Handler()
        op = ctypes.POINTER(IActivateAudioInterfaceAsyncOperation)()
        activate(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, ctypes.byref(IAudioClient._iid_),
            ctypes.byref(blob), handler, ctypes.byref(op),
        )
        if not handler.done.wait(5):
            raise TimeoutError(f"process loopback activation for pid {self.pid} timed out")
        hr, unk = op.GetActivateResult()
        if hr < 0:
            raise OSError(f"process loopback activation for pid {self.pid} failed: 0x{hr & 0xFFFFFFFF:08X}")

        self._client = unk.QueryInterface(IAudioClient)
        # GetMixFormat is not supported on a process-loopback client, so we
        # pick the format; a pointer, like GetMixFormat's, so legs use it as is.
        self._fmt = ctypes.pointer(loopback_format())
        self._start_capture(AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM)
        log.info(
            "Engine source open: pid %d (process loopback, %d Hz, %d ch)",
            self.pid, self._rate, self._fmt.contents.nChannels,
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

        now = time.monotonic()
        for device_id in list(self._retry_at):
            if device_id not in wanted:
                del self._retry_at[device_id]

        for device_id in wanted - set(self._legs):
            if now < self._retry_at.get(device_id, now):
                continue
            try:
                self._legs[device_id] = self._open_leg(device_id)
                self._retry_at.pop(device_id, None)
            except Exception as e:
                # One dead endpoint must not take the others down, and must not
                # be retried every 5 ms. It stays wanted, though: an endpoint
                # held in exclusive mode, or mid profile switch, comes back.
                (log.debug if device_id in self._retry_at else log.error)(
                    "Engine could not open leg %s: %s", device_id, e)
                self._retry_at[device_id] = now + LEG_RETRY_S

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

        for device_id, leg in list(self._legs.items()):
            try:
                leg.write()
            except Exception as e:
                # An endpoint can be invalidated without going away (a format
                # change, a Bluetooth profile switch). Drop that leg so it
                # can't starve the others; it stays wanted, so the next
                # reconcile reopens it, retrying once a second if that fails.
                log.warning("Engine leg %s failed, reopening: %s", device_id, e)
                self._close_leg(self._legs.pop(device_id))

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
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--from", dest="source", help="source endpoint id (default: current output)")
    src.add_argument("--pid", type=int, help="capture this app (and its children) instead")
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

    if args.pid is not None:
        source, engine = f"pid {args.pid}", Engine(pid=args.pid)
    else:
        source = args.source or default_output_id()
        engine = Engine(source)
    engine.start()
    engine.set_legs([i for i in args.to.split(",") if i])
    print(f"Mirroring {source}\n  -> {args.to}\nCtrl-C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        engine.stop()
