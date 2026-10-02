from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time

log = logging.getLogger(__name__)

POLL_MS       = 5    # how often the pump looks at the source and the legs
BUFFER_MS     = 200  # endpoint buffer we ask Windows for, source and legs alike
TARGET_MS     = 30   # how much we keep queued in each endpoint
DRIFT_MS      = 20   # how far a leg may fall behind before we drop frames
DRIFT_KEEP_MS = 5    # what we leave queued after dropping
LEG_RETRY_S   = 1.0  # how long a leg that failed to open (or failed again right after opening) waits
OPENER_JOIN_S = 2.0  # how long _close waits for a busy opener before abandoning it


class _Leg:
    """One output endpoint, and the frames queued for it.

    The queue is the drift signal: a leg slower than the source grows it; a faster
    one drains it and gets one clean primed gap of silence rather than a stall.

    A dry endpoint with data waiting is primed with silence up to two device
    periods plus POLL_MS, against pump jitter. Queued audio counts towards that,
    so a stall backlog adds no silence.
    """

    def __init__(self, device_id: str, client, render, frames: int, bpf: int, rate: int,
                 period_ms: float, src_period_ms: float) -> None:
        self.id     = device_id
        self.client = client
        self.render = render
        self.frames = frames  # the endpoint's buffer size, in frames
        self.bpf    = bpf     # bytes per frame
        self.prime  = int(rate * (period_ms + POLL_MS) / 1000)  # silence cushion for a dry endpoint
        self.period = int(rate * period_ms / 1000)
        # Padding we aim for, in frames. Data arrives a source packet at a time:
        # leave room for it after the cushion.
        self.target = max(int(rate * TARGET_MS / 1000),
                          self.prime + max(self.period, int(rate * src_period_ms / 1000)))
        self.fifo   = bytearray()
        self.high   = int(rate * DRIFT_MS / 1000) * bpf
        self.keep   = int(rate * DRIFT_KEEP_MS / 1000) * bpf

    def write(self) -> None:
        if not self.fifo:  # nothing to prime, top up or trim: skip the COM call
            return
        padding = self.client.GetCurrentPadding()
        if padding == 0:
            from pipemix.windows.wasapi.com import AUDCLNT_BUFFERFLAGS_SILENT

            # Only what queued audio can't cover: padding a stall's backlog with
            # silence would make the drift trim delete real audio.
            silence = max(0, self.prime + self.period - len(self.fifo) // self.bpf)
            if silence:
                self.render.GetBuffer(silence)
                self.render.ReleaseBuffer(silence, AUDCLNT_BUFFERFLAGS_SILENT)
                log.debug("[leg %s] dry: primed %d frames", self.id, silence)
            padding = silence
        n = min(min(self.frames, self.target) - padding, len(self.fifo) // self.bpf)
        if n > 0:
            nbytes = n * self.bpf
            ctypes.memmove(self.render.GetBuffer(n), bytes(self.fifo[:nbytes]), nbytes)
            self.render.ReleaseBuffer(n, 0)
            del self.fifo[:nbytes]
        # Drift is what's left once the endpoint is topped up, so a pump stall's
        # backlog refills the endpoint first instead of being dropped.
        if len(self.fifo) > self.high:
            # ponytail: drops a block when a leg drifts (<=15 ms skip, off-clock legs only);
            # upgrade to a resampler on IAudioClock::GetPosition, as module-loopback does.
            dropped = len(self.fifo) - self.keep
            del self.fifo[:dropped]
            log.debug("[leg %s] drift: dropped %d frames", self.id, dropped // self.bpf)


def _period_ms(client) -> float:
    """The endpoint's default device period in ms; 10 if it can't be read."""
    try:
        return client.GetDevicePeriod()[0] / 10_000 or 10.0  # (default, minimum), 100 ns
    except Exception as e:
        log.debug("GetDevicePeriod failed (%s), assuming 10 ms", e)
        return 10.0


def _mmcss_enter():
    """Register the calling thread as "Pro Audio" with MMCSS. None if unavailable."""
    try:
        from ctypes import wintypes

        avrt = ctypes.windll.avrt
        avrt.AvSetMmThreadCharacteristicsW.restype = ctypes.c_void_p  # 64-bit handle
        avrt.AvSetMmThreadCharacteristicsW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(wintypes.DWORD)]
        handle = avrt.AvSetMmThreadCharacteristicsW("Pro Audio", ctypes.byref(wintypes.DWORD(0)))
        if handle:
            return handle
        log.debug("MMCSS registration returned no handle")
    except (AttributeError, OSError) as e:
        log.debug("MMCSS unavailable: %s", e)
    return None


def _mmcss_leave(handle) -> None:
    if handle:
        ctypes.windll.avrt.AvRevertMmThreadCharacteristics(ctypes.c_void_p(handle))


class Engine:
    """Mirrors `source_id`, or app `pid`, onto whatever legs are set, on its own pump thread.

    `set_legs` only records what is wanted, so any thread may call it. An opener
    thread does the slow opens (Bluetooth: ~0.5 s) so the pump never stalls; the
    pump adopts and closes legs. Both are in the MTA, so COM pointers work on either.
    """

    def __init__(self, source_id: str | None = None, *, pid: int | None = None) -> None:
        self.source_id = source_id
        self.pid = pid
        self.error: Exception | None = None

        self._wanted: set[str] = set()
        self._legs: dict[str, _Leg] = {}
        self._retry_at: dict[str, float] = {}  # monotonic time a failed leg may be tried again
        self._adopted_at: dict[str, float] = {}  # monotonic time each leg was last adopted
        self._opening: set[str] = set()        # legs handed to the opener, not back yet
        self._requests: queue.Queue = queue.Queue()  # device ids to open; None stops the opener
        self._results: queue.Queue = queue.Queue()   # (device_id, _Leg or Exception)
        self._opener: threading.Thread | None = None
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
        self._src_period_ms = 10.0  # capture packet period; legs size their target to it

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

    def stop(self, wait: bool = True) -> None:
        """Stop the pump. wait=False only signals it: within one POLL_MS tick the pump
        stops feeding the legs and closes them and the source on its own thread."""
        self._stop.set()
        if not wait:
            return
        if self._thread:
            self._thread.join(timeout=5)  # covers the opener's join in _close
            if self._thread.is_alive():
                log.warning("Engine pump %s still running after 5 s, abandoning it", self._thread.name)
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
        mmcss = _mmcss_enter()
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

            # Only now: the opener reads _fmt/_bpf/_rate, and reuses the
            # enumerator _open_source made (process loopback never makes one,
            # so the opener is then its only user).
            self._opener = threading.Thread(
                target=self._open_legs, name=f"{threading.current_thread().name}-opener", daemon=True)
            self._opener.start()

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
            _mmcss_leave(mmcss)
            comtypes.CoUninitialize()

    def _open_legs(self) -> None:
        """Opener thread: open whatever the pump asks for, hand back the leg or the error."""
        import comtypes

        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            while True:
                device_id = self._requests.get()
                if device_id is None or self._stop.is_set():
                    return
                try:
                    result = self._open_leg(device_id)
                except Exception as e:
                    result = e
                if self._stop.is_set():
                    if not isinstance(result, Exception):
                        self._close_leg(result)  # the pump may be past its drain in _close
                    return
                self._results.put((device_id, result))
                result = None  # don't keep a COM object or traceback alive until the next open
        finally:
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
        self._src_period_ms = _period_ms(self._client)  # process loopback: falls back to 10
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
        period_ms = _period_ms(client)
        log.info("Engine leg open: %s", device_id)
        return _Leg(device_id, client, render, client.GetBufferSize(), self._bpf, self._rate,
                    period_ms=period_ms, src_period_ms=self._src_period_ms)

    def _reconcile(self) -> None:
        with self._lock:
            wanted = set(self._wanted)

        now = time.monotonic()
        while True:
            try:
                device_id, result = self._results.get_nowait()
            except queue.Empty:
                break
            self._opening.discard(device_id)
            if isinstance(result, Exception):
                # A dead endpoint must not take the others down or be retried every
                # 5 ms. It stays wanted: exclusive mode or a profile switch can end.
                (log.debug if device_id in self._retry_at else log.error)(
                    "Engine could not open leg %s: %s", device_id, result)
                self._retry_at[device_id] = now + LEG_RETRY_S
            elif device_id in wanted and device_id not in self._legs:
                self._legs[device_id] = result
                self._adopted_at[device_id] = now
                self._retry_at.pop(device_id, None)
            else:
                self._close_leg(result)  # unwanted while it was opening

        for device_id in list(self._retry_at):
            if device_id not in wanted:
                del self._retry_at[device_id]

        for device_id in wanted - set(self._legs) - self._opening:
            if now < self._retry_at.get(device_id, now):
                continue
            self._opening.add(device_id)
            self._requests.put(device_id)

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
                    leg.fifo += data
            self._capture.ReleaseBuffer(frames)

        for device_id, leg in list(self._legs.items()):
            try:
                leg.write()
            except Exception as e:
                # Invalidated without leaving (format change, Bluetooth profile switch):
                # drop the leg; it stays wanted, so reconcile reopens it at once, or
                # after LEG_RETRY_S if it failed straight after its last reopen.
                log.warning("Engine leg %s failed, reopening: %s", device_id, e)
                self._close_leg(self._legs.pop(device_id))
                now = time.monotonic()
                if now - self._adopted_at.get(device_id, -LEG_RETRY_S) < LEG_RETRY_S:
                    self._retry_at[device_id] = now + LEG_RETRY_S

    def _close(self) -> None:
        if self._opener is not None:
            self._requests.put(None)
            # ponytail: a wedged driver call can't be cancelled; past this we
            # abandon the opener (daemon) and leak whatever it opens.
            self._opener.join(timeout=OPENER_JOIN_S)
            if self._opener.is_alive():
                log.warning("Engine opener still busy after %.1f s, abandoning it", OPENER_JOIN_S)
            self._opener = None
        while True:
            try:
                _device_id, result = self._results.get_nowait()
            except queue.Empty:
                break
            if not isinstance(result, Exception):
                self._close_leg(result)
        self._opening.clear()
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

    import sys

    sys.setswitchinterval(0.001)  # as pipemix.windows.main does, so probes measure what the app gets
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
