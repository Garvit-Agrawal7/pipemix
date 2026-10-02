from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from pipemix.windows.wasapi.engine import Engine


# -- Engine constructor: source_id xor pid ------------------------------

def test_engine_requires_one_source():
    with pytest.raises(ValueError):
        Engine()


def test_engine_rejects_both_source_and_pid():
    with pytest.raises(ValueError):
        Engine("x", pid=1)


def test_engine_pid_source():
    e = Engine(pid=42)
    assert e.pid == 42
    assert e.source_id is None


def test_engine_source_id_source():
    e = Engine("id")
    assert e.source_id == "id"
    assert e.pid is None


# -- com.py: process-loopback declarations ------------------------------

from pipemix.windows.wasapi import com


def test_loopback_constants():
    assert com.AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK == 1
    assert com.PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE == 0
    assert com.VT_BLOB == 65
    assert com.VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK == r"VAD\Process_Loopback"


def test_activation_params_struct():
    assert ctypes.sizeof(com.AUDIOCLIENT_ACTIVATION_PARAMS) == 12


def test_loopback_params():
    params, blob = com.loopback_params(1234)
    assert params.ActivationType == 1
    assert params.ProcessLoopbackParams.TargetProcessId == 1234
    assert params.ProcessLoopbackParams.ProcessLoopbackMode == 0
    assert ctypes.sizeof(params) == 12

    assert blob.vt == com.VT_BLOB
    assert blob.cbSize == ctypes.sizeof(params)
    assert blob.pBlobData == ctypes.addressof(params)


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="offsets are for 64-bit Python")
def test_propvariant_blob_offsets():
    blob = com.PROPVARIANT_BLOB
    assert blob.vt.offset == 0
    assert blob.cbSize.offset == 8
    assert blob.pBlobData.offset == 16


def test_loopback_format():
    fmt = com.loopback_format()
    assert fmt.wFormatTag == 3
    assert fmt.nChannels == 2
    assert fmt.nSamplesPerSec == 48000
    assert fmt.wBitsPerSample == 32
    assert fmt.nBlockAlign == 8
    assert fmt.nAvgBytesPerSec == 384000
    assert fmt.cbSize == 0


def test_com_interfaces_declared():
    # Presence only — these are comtypes interface classes, not callable here.
    assert com.IActivateAudioInterfaceAsyncOperation is not None
    assert com.IActivateAudioInterfaceCompletionHandler is not None
    assert com.IAgileObject is not None


# -- A dead leg must not starve the others --------------------------------

class _NoCapture:
    def GetNextPacketSize(self):
        return 0


class _FakeLeg:
    def __init__(self, device_id, fail=False):
        self.id, self.fail, self.writes = device_id, fail, 0
        self.client = self

    def push(self, data):
        pass

    def write(self):
        if self.fail:
            # AUDCLNT_E_DEVICE_INVALIDATED, as on a Bluetooth profile switch
            raise OSError(-2004287484, "device invalidated")
        self.writes += 1

    def Stop(self):
        pass


@pytest.fixture
def opener(monkeypatch):
    """Start an engine's opener thread without COM; stops it after the test."""
    import comtypes

    monkeypatch.setattr(comtypes, "CoInitializeEx", lambda *a: None)
    monkeypatch.setattr(comtypes, "CoUninitialize", lambda: None)
    started = []

    def start(e):
        e._opener = threading.Thread(target=e._open_legs, daemon=True)
        e._opener.start()
        started.append(e)
        return e

    yield start
    for e in started:
        e._stop.set()
        e._close()


def _settle(e, timeout=2.0):
    """Reconcile, wait for the opener to hand back every open, reconcile again to adopt them."""
    e._reconcile()
    deadline = time.perf_counter() + timeout  # monotonic is patched in some tests
    while e._results.qsize() < len(e._opening):
        assert time.perf_counter() < deadline, "opener never answered"
        time.sleep(0.001)
    e._reconcile()


def test_invalidated_leg_is_dropped_and_reopened_without_starving_the_rest(monkeypatch, opener):
    import pipemix.windows.wasapi.engine as engine_mod

    clock = [100.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])
    e = opener(Engine("src"))
    e._capture = _NoCapture()
    dead, alive = _FakeLeg("dead", fail=True), _FakeLeg("alive")
    e._legs = {"dead": dead, "alive": alive}  # the dead one first, so it would block the other
    e.set_legs(["dead", "alive"])

    e._pump()

    assert alive.writes == 1
    assert "dead" not in e._legs

    fresh = _FakeLeg("dead")
    monkeypatch.setattr(e, "_open_leg", lambda device_id: fresh)
    _settle(e)  # a first write failure reopens at once
    assert e._legs["dead"] is fresh


def test_leg_that_fails_right_after_reopening_backs_off(monkeypatch, opener):
    import pipemix.windows.wasapi.engine as engine_mod

    clock = [100.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])
    e = opener(Engine("src"))
    e._capture = _NoCapture()
    opened = []
    monkeypatch.setattr(e, "_open_leg", lambda d: opened.append(clock[0]) or _FakeLeg(d, fail=True))
    e.set_legs(["bad"])

    _settle(e)                          # opened, then won't take writes
    clock[0] += 0.1
    e._pump()
    _settle(e)
    assert opened == [100.0]            # failed 0.1 s after opening: backing off
    clock[0] += engine_mod.LEG_RETRY_S
    _settle(e)
    assert opened == [100.0, 101.1]

    clock[0] += engine_mod.LEG_RETRY_S + 0.1
    e._legs["bad"].fail = False
    e._pump()                           # worked for a while, then fails: reopen at once
    e._legs["bad"].fail = True
    e._pump()
    _settle(e)
    assert len(opened) == 3


def test_leg_that_fails_to_open_is_retried_once_a_second(monkeypatch, opener):
    import pipemix.windows.wasapi.engine as engine_mod

    clock = [100.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])
    e = opener(Engine("src"))
    e.set_legs(["busy"])
    attempts = []

    def _open(device_id):
        attempts.append(clock[0])
        if len(attempts) == 1:
            raise OSError(-2004287478, "device in use")  # AUDCLNT_E_DEVICE_IN_USE
        return _FakeLeg(device_id)
    monkeypatch.setattr(e, "_open_leg", _open)

    _settle(e)                         # fails
    clock[0] += 0.5
    _settle(e)                         # too soon: not tried again
    assert attempts == [100.0]
    assert "busy" not in e._legs

    clock[0] += 0.6
    _settle(e)                         # a second on: tried and opened
    assert len(attempts) == 2
    assert "busy" in e._legs


def test_retry_is_forgotten_when_the_leg_is_no_longer_wanted(monkeypatch, opener):
    e = opener(Engine("src"))
    e.set_legs(["gone"])
    monkeypatch.setattr(e, "_open_leg", lambda d: (_ for _ in ()).throw(OSError("nope")))
    _settle(e)
    assert "gone" in e._retry_at
    e.set_legs([])
    e._reconcile()
    assert not e._retry_at


def test_failed_open_logs_error_once_then_debug(monkeypatch, opener, caplog):
    import pipemix.windows.wasapi.engine as engine_mod

    clock = [100.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])
    e = opener(Engine("src"))
    e.set_legs(["busy"])
    attempts = []
    monkeypatch.setattr(e, "_open_leg", lambda d: attempts.append(d) or (_ for _ in ()).throw(OSError("in use")))

    with caplog.at_level(logging.DEBUG, logger=engine_mod.log.name):
        _settle(e)
        clock[0] += engine_mod.LEG_RETRY_S - 0.01
        _settle(e)                     # too soon
        clock[0] += 0.02
        _settle(e)                     # retried, still failing
    assert attempts == ["busy", "busy"]
    levels = [r.levelno for r in caplog.records if "could not open leg" in r.message]
    assert levels == [logging.ERROR, logging.DEBUG]
    assert "busy" not in e._legs and "busy" in e._retry_at


# -- Legs open off the pump thread ----------------------------------------

class _SlowOpen:
    """An `_open_leg` that blocks "slow" until released, like a Bluetooth open."""

    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = []

    def __call__(self, device_id):
        self.calls.append(device_id)
        if device_id == "slow":
            self.entered.set()
            assert self.release.wait(5)
        return _FakeLeg(device_id)


def _closes(monkeypatch, e):
    closed = []
    monkeypatch.setattr(e, "_close_leg", closed.append)
    return closed


def test_slow_open_does_not_stall_the_pump(monkeypatch, opener):
    e = opener(Engine("src"))
    e._capture = _NoCapture()
    alive = _FakeLeg("alive")
    e._legs = {"alive": alive}
    slow = _SlowOpen()
    monkeypatch.setattr(e, "_open_leg", slow)
    e.set_legs(["alive", "slow"])

    e._reconcile()
    assert slow.entered.wait(2)
    worst = 0.0
    for _ in range(50):
        t = time.perf_counter()
        e._reconcile()
        e._pump()
        worst = max(worst, time.perf_counter() - t)
    print(f"worst reconcile+pump tick while a leg opens: {worst * 1000:.3f} ms")
    assert worst < 0.020
    assert alive.writes == 50
    assert "slow" not in e._legs

    slow.release.set()
    _settle(e)
    assert e.legs == ["alive", "slow"]


def test_leg_is_opened_once_while_in_flight(monkeypatch, opener):
    e = opener(Engine("src"))
    slow = _SlowOpen()
    monkeypatch.setattr(e, "_open_leg", slow)
    e.set_legs(["slow"])
    e._reconcile()
    assert slow.entered.wait(2)
    for _ in range(100):
        e._reconcile()
    slow.release.set()
    _settle(e)
    for _ in range(10):
        e._reconcile()
    assert slow.calls == ["slow"]
    assert e.legs == ["slow"]


def test_leg_unwanted_by_the_time_it_opens_is_closed(monkeypatch, opener):
    e = opener(Engine("src"))
    slow = _SlowOpen()
    monkeypatch.setattr(e, "_open_leg", slow)
    closed = _closes(monkeypatch, e)
    e.set_legs(["slow"])
    e._reconcile()
    assert slow.entered.wait(2)
    e.set_legs([])
    slow.release.set()
    _settle(e)
    assert [leg.id for leg in closed] == ["slow"]
    assert e.legs == [] and not e._opening


def test_stop_joins_the_opener_and_closes_an_unadopted_leg(monkeypatch):
    import comtypes

    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(avrt=_Avrt()), raising=False)
    monkeypatch.setattr(comtypes, "CoInitializeEx", lambda *a: None)
    monkeypatch.setattr(comtypes, "CoUninitialize", lambda: None)
    e = Engine("src")
    monkeypatch.setattr(e, "_open_source", lambda: None)
    monkeypatch.setattr(e, "_pump", lambda: None)
    slow = _SlowOpen()
    monkeypatch.setattr(e, "_open_leg", slow)
    closed = _closes(monkeypatch, e)

    e.start()
    e.set_legs(["slow"])
    assert slow.entered.wait(2)
    opener = e._opener
    threading.Timer(0.1, slow.release.set).start()  # finishes after stop() is underway
    e.stop()

    assert not opener.is_alive()
    assert [leg.id for leg in closed] == ["slow"]
    assert e.legs == []


# -- Endpoint padding is capped at TARGET_MS ------------------------------

class _PadClient:
    def __init__(self, padding=0):
        self.padding = padding

    def GetCurrentPadding(self):
        return self.padding


class _PadRender:
    def __init__(self):
        self.bufs, self.released, self.writes = [], [], []

    def GetBuffer(self, n):
        buf = ctypes.create_string_buffer(n * 4)
        self.bufs.append(buf)
        return ctypes.addressof(buf)

    def ReleaseBuffer(self, n, flags):
        self.released.append(n)
        self.writes.append((n, flags))


def _pad_leg(padding=0, **kw):
    from pipemix.windows.wasapi.engine import _Leg
    client, render = _PadClient(padding), _PadRender()
    return _Leg("d", client, render, 9600, 4, 48000, **kw), client, render  # target = 1440 frames


def test_write_never_pushes_padding_above_target():
    leg, client, render = _pad_leg()
    assert leg.target == 1440
    for padding in (1, 500, 1439):
        leg.fifo = bytearray(4 * 5000)
        client.padding, render.released = padding, []
        leg.write()
        assert render.released == [leg.target - padding]


def test_write_does_nothing_at_or_over_target():
    leg, client, render = _pad_leg()
    leg.fifo = bytearray(4 * 900)  # under the drift threshold
    for padding in (1440, 5000):
        client.padding = padding
        leg.write()
    assert render.released == []
    assert len(leg.fifo) == 4 * 900


def test_surplus_stays_in_the_fifo():
    leg, client, render = _pad_leg(padding=1000)
    leg.fifo = bytearray(4 * 600)
    leg.write()
    assert render.released == [440]
    assert len(leg.fifo) == 4 * 160


def test_sustained_surplus_triggers_drift_drop():
    from pipemix.windows.wasapi.engine import DRIFT_KEEP_MS
    leg, client, render = _pad_leg(padding=1440)  # endpoint never drains
    for _ in range(10):
        leg.push(bytes(4 * 240))  # 5 ms per push
        leg.write()
    assert render.released == []
    assert len(leg.fifo) <= leg.high
    leg.push(bytes(leg.high))
    leg.write()
    assert len(leg.fifo) == int(48000 * DRIFT_KEEP_MS / 1000) * 4


def test_stall_backlog_refills_the_endpoint_before_any_drift_drop():
    # 27.5 ms queued, a 25 ms pump stall drains it to 7.5 ms while three 10 ms
    # packets pile up: the endpoint is topped back to 30 ms, nothing lost.
    leg, client, render = _pad_leg(padding=360)  # 7.5 ms left in the endpoint
    for _ in range(3):
        leg.push(bytes(4 * 480))
    leg.write()
    assert render.released == [leg.target - 360]  # 22.5 ms written
    assert len(leg.fifo) == 4 * (1440 - 1080)     # the 7.5 ms remainder is kept


# -- Silence priming of dry endpoints -------------------------------------

def test_dry_endpoint_with_data_is_primed_then_filled_to_target():
    from pipemix.windows.wasapi.com import AUDCLNT_BUFFERFLAGS_SILENT
    leg, client, render = _pad_leg()
    leg.fifo = bytearray(4 * 480)  # one packet: tops up to prime + period
    leg.write()
    assert render.writes == [(720, AUDCLNT_BUFFERFLAGS_SILENT), (480, 0)]
    assert leg.fifo == bytearray()


def test_dry_endpoint_with_a_partial_packet_is_primed_to_the_same_level():
    from pipemix.windows.wasapi.com import AUDCLNT_BUFFERFLAGS_SILENT
    leg, client, render = _pad_leg()
    leg.fifo = bytearray(4 * 100)
    leg.write()
    assert render.writes == [(1100, AUDCLNT_BUFFERFLAGS_SILENT), (100, 0)]


def test_stall_backlog_on_a_dry_endpoint_adds_no_silence_and_drops_nothing():
    # 40 ms pump stall: endpoint ran dry, 1920 frames piled up in the queue.
    leg, client, render = _pad_leg()
    leg.fifo = bytearray(4 * 1920)
    leg.write()
    assert render.writes == [(leg.target, 0)]
    assert len(leg.fifo) == 4 * (1920 - leg.target)  # the rest kept, not trimmed


def test_non_empty_endpoint_is_never_primed():
    leg, client, render = _pad_leg(padding=1)
    leg.fifo = bytearray(4 * 5000)
    leg.write()
    assert render.writes == [(leg.target - 1, 0)]


def test_dry_endpoint_with_empty_fifo_writes_nothing():
    leg, client, render = _pad_leg()
    leg.write()
    assert render.writes == []


def test_primes_once_then_steady_state_never_runs_dry():
    leg, client, render = _pad_leg()
    queued = 0  # frames in the endpoint, mirrored from what the leg wrote
    for _ in range(200):
        leg.push(bytes(4 * 480))                # source: one 10 ms period per tick
        n0 = len(render.writes)
        client.padding = queued
        leg.write()
        queued += sum(n for n, _ in render.writes[n0:])
        queued = max(queued - 480, 0)           # endpoint: consumes one period per tick
        client.padding = queued
        assert queued > 0
    assert sum(1 for _, f in render.writes if f) == 1


def test_prime_size_follows_device_period_and_falls_back_to_10ms():
    from pipemix.windows.wasapi.engine import _period_ms
    assert _pad_leg(period_ms=10)[0].prime == 720
    assert _pad_leg()[0].prime == 720
    assert _period_ms(type("C", (), {"GetDevicePeriod": lambda s: (100000, 30000)})()) == 10.0
    assert _period_ms(type("C", (), {"GetDevicePeriod": lambda s: (250000, 30000)})()) == 25.0
    assert _period_ms(type("C", (), {"GetDevicePeriod": lambda s: 100000})()) == 10.0

    def boom(s):
        raise OSError("no")
    assert _period_ms(type("C", (), {"GetDevicePeriod": boom})()) == 10.0
    assert _period_ms(object()) == 10.0


def test_long_period_device_raises_target_so_data_fits():
    leg, client, render = _pad_leg(period_ms=25)
    assert leg.prime == 1440
    assert leg.target == leg.prime + 1200 > 1440
    leg.fifo = bytearray(4 * 480)
    leg.write()
    assert render.writes[1] == (480, 0)
    assert sum(n for n, _ in render.writes) == leg.target
    assert _pad_leg()[0].target == 1440  # 10 ms device: unchanged


def test_empty_fifo_write_skips_the_padding_call():
    leg, client, render = _pad_leg()
    client.GetCurrentPadding = lambda: pytest.fail("GetCurrentPadding called")
    leg.write()


def test_source_period_raises_target_default_unchanged():
    leg = _pad_leg(period_ms=10, src_period_ms=20)[0]
    assert leg.target == leg.prime + 960 == 1680
    assert _pad_leg(src_period_ms=10)[0].target == 1440


def test_open_leg_sizes_target_to_the_source_period(monkeypatch):
    class Client:
        def __init__(self, period):
            self.period = period
        def GetDevicePeriod(self):
            return (self.period, self.period)
        def Initialize(self, *a):
            pass
        def GetService(self, iid):
            return self
        def QueryInterface(self, iface):
            return self
        def Start(self):
            pass
        def GetBufferSize(self):
            return 9600
        def Activate(self, *a):
            return self

    e = Engine("src")
    e._fmt = type("P", (), {"contents": type("F", (), {"nBlockAlign": 4, "nSamplesPerSec": 48000})()})()
    e._client = Client(200_000)                 # 20 ms source packets
    e._start_capture(0)
    monkeypatch.setattr(e, "_device", lambda d: Client(100_000))  # 10 ms leg
    assert e._open_leg("d").target == 1680


def test_period_of_a_client_without_device_period_is_10ms():
    from pipemix.windows.wasapi.engine import _period_ms
    def nope(s):
        raise OSError("Not implemented")
    assert _period_ms(type("C", (), {"GetDevicePeriod": nope})()) == 10.0


def test_late_leg_after_stop_is_closed_not_queued(monkeypatch, opener):
    import pipemix.windows.wasapi.engine as engine_mod

    monkeypatch.setattr(engine_mod, "OPENER_JOIN_S", 0.05)
    e = opener(Engine("src"))
    slow = _SlowOpen()
    monkeypatch.setattr(e, "_open_leg", slow)
    closed = _closes(monkeypatch, e)
    e.set_legs(["slow"])
    e._reconcile()
    assert slow.entered.wait(2)
    opener_thread = e._opener
    e._stop.set()
    e._close()  # gives up on the busy opener after OPENER_JOIN_S
    slow.release.set()
    opener_thread.join(2)
    assert not opener_thread.is_alive()
    assert [leg.id for leg in closed] == ["slow"]
    assert e._results.empty() and e.legs == []


# -- MMCSS on the pump thread ---------------------------------------------

class _Avrt:
    def __init__(self, handle=0x1234, boom=False):
        self.reverted = []

        def set_(*a):  # plain functions, so the engine can set restype/argtypes
            if boom:
                raise OSError("no avrt")
            return handle

        def revert(h):
            self.reverted.append(h)

        self.AvSetMmThreadCharacteristicsW = set_
        self.AvRevertMmThreadCharacteristics = revert


def _run_engine(monkeypatch, avrt):
    import comtypes

    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(avrt=avrt), raising=False)
    monkeypatch.setattr(comtypes, "CoInitializeEx", lambda *a: None)
    monkeypatch.setattr(comtypes, "CoUninitialize", lambda: None)
    e = Engine("src")
    for name in ("_open_source", "_reconcile", "_pump"):
        monkeypatch.setattr(e, name, lambda: None)
    e.start()
    assert e._ready.is_set() and e.error is None
    e.stop()


def test_mmcss_handle_is_reverted_on_stop(monkeypatch):
    avrt = _Avrt()
    _run_engine(monkeypatch, avrt)
    assert avrt.reverted == [0x1234]


@pytest.mark.parametrize("avrt", [_Avrt(boom=True), _Avrt(handle=0), _Avrt(handle=None)])
def test_mmcss_failure_does_not_stop_the_engine(monkeypatch, avrt):
    _run_engine(monkeypatch, avrt)
    assert avrt.reverted == []
