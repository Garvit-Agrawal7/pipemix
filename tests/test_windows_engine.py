from __future__ import annotations

import ctypes
import logging
import threading
import time
import types
from unittest.mock import MagicMock, Mock

import comtypes
import pytest

import pipemix.windows.wasapi.engine as engine_mod
from pipemix.windows.wasapi import com
from pipemix.windows.wasapi.com import AUDCLNT_BUFFERFLAGS_SILENT
from pipemix.windows.wasapi.engine import DRIFT_KEEP_MS, Engine, _Leg, _period_ms


# -- com.py: process-loopback declarations ------------------------------

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


# -- A dead leg must not starve the others --------------------------------

class _NoCapture:
    def GetNextPacketSize(self):
        return 0


class _FakeLeg:
    def __init__(self, device_id, fail=False):
        self.id, self.fail, self.writes, self.adopted = device_id, fail, 0, 0.0
        self.client = self

    def write(self):
        if self.fail:
            # AUDCLNT_E_DEVICE_INVALIDATED, as on a Bluetooth profile switch
            raise OSError(-2004287484, "device invalidated")
        self.writes += 1

    def Stop(self):
        pass


def _patch_com(monkeypatch, avrt=None):
    """Stub COM init (and MMCSS, if `avrt` is given) so threads run without COM."""
    if avrt is not None:
        monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(avrt=avrt), raising=False)
    monkeypatch.setattr(comtypes, "CoInitializeEx", lambda *a: None)
    monkeypatch.setattr(comtypes, "CoUninitialize", lambda: None)


def _stub_engine(monkeypatch, *names, avrt=None):
    """An Engine whose pump-thread internals named in `names` do nothing, on stubbed COM."""
    _patch_com(monkeypatch, avrt or _Avrt())
    e = Engine("src")
    for name in names:
        monkeypatch.setattr(e, name, lambda: None)
    return e


@pytest.fixture
def clock(monkeypatch):
    t = [100.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: t[0])
    return t


@pytest.fixture
def opener(monkeypatch):
    """Start an engine's opener thread without COM; stops it after the test."""
    _patch_com(monkeypatch)
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
    while len(e._results) < len(e._opening):
        assert time.perf_counter() < deadline, "opener never answered"
        time.sleep(0.001)
    e._reconcile()


def test_invalidated_leg_is_dropped_and_reopened_without_starving_the_rest(monkeypatch, opener, clock):
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


def test_leg_that_fails_right_after_reopening_backs_off(monkeypatch, opener, clock):
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


def test_leg_that_fails_to_open_is_retried_once_a_second(monkeypatch, opener, clock):
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
    monkeypatch.setattr(e, "_open_leg", Mock(side_effect=OSError("nope")))
    _settle(e)
    assert "gone" in e._retry_at
    e.set_legs([])
    e._reconcile()
    assert not e._retry_at


def test_failed_open_logs_error_once_then_debug(monkeypatch, opener, clock, caplog):
    e = opener(Engine("src"))
    e.set_legs(["busy"])
    open_leg = Mock(side_effect=OSError("in use"))
    monkeypatch.setattr(e, "_open_leg", open_leg)

    with caplog.at_level(logging.DEBUG, logger=engine_mod.log.name):
        _settle(e)
        clock[0] += engine_mod.LEG_RETRY_S - 0.01
        _settle(e)                     # too soon
        clock[0] += 0.02
        _settle(e)                     # retried, still failing
    assert open_leg.call_count == 2
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
    e = _stub_engine(monkeypatch, "_open_source", "_pump")
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
        self.bufs, self.writes = [], []

    @property
    def released(self) -> list[int]:
        return [n for n, _ in self.writes]

    def GetBuffer(self, n):
        buf = ctypes.create_string_buffer(n * 4)
        self.bufs.append(buf)
        return ctypes.addressof(buf)

    def ReleaseBuffer(self, n, flags):
        self.writes.append((n, flags))


def _pad_leg(padding=0, period_ms=10.0, src_period_ms=10.0):
    client, render = _PadClient(padding), _PadRender()
    return _Leg("d", client, render, 9600, 4, 48000, period_ms, src_period_ms), client, render  # target = 1440 frames


def test_write_never_pushes_padding_above_target():
    leg, client, render = _pad_leg()
    assert leg.target == 1440
    for padding in (1, 500, 1439):
        leg.fifo = bytearray(4 * 5000)
        client.padding, render.writes = padding, []
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


def test_sustained_surplus_triggers_drift_drop():
    leg, client, render = _pad_leg(padding=1440)  # endpoint never drains
    for _ in range(10):
        leg.fifo += bytes(4 * 240)  # 5 ms per push
        leg.write()
    assert render.released == []
    assert len(leg.fifo) <= leg.high
    leg.fifo += bytes(leg.high)
    leg.write()
    assert len(leg.fifo) == int(48000 * DRIFT_KEEP_MS / 1000) * 4


def test_stall_backlog_refills_the_endpoint_before_any_drift_drop():
    # 27.5 ms queued, a 25 ms pump stall drains it to 7.5 ms while three 10 ms
    # packets pile up: the endpoint is topped back to 30 ms, nothing lost.
    leg, client, render = _pad_leg(padding=360)  # 7.5 ms left in the endpoint
    for _ in range(3):
        leg.fifo += bytes(4 * 480)
    leg.write()
    assert render.released == [leg.target - 360]  # 22.5 ms written
    assert len(leg.fifo) == 4 * (1440 - 1080)     # the 7.5 ms remainder is kept


# -- Silence priming of dry endpoints -------------------------------------

def test_dry_endpoint_with_data_is_primed_then_filled_to_target():
    leg, client, render = _pad_leg()
    leg.fifo = bytearray(4 * 480)  # one packet: tops up to prime + period
    leg.write()
    assert render.writes == [(720, AUDCLNT_BUFFERFLAGS_SILENT), (480, 0)]
    assert leg.fifo == bytearray()


def test_dry_endpoint_with_a_partial_packet_is_primed_to_the_same_level():
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


def test_primes_once_then_steady_state_never_runs_dry():
    leg, client, render = _pad_leg()
    queued = 0  # frames in the endpoint, mirrored from what the leg wrote
    for _ in range(200):
        leg.fifo += bytes(4 * 480)                # source: one 10 ms period per tick
        n0 = len(render.writes)
        client.padding = queued
        leg.write()
        queued += sum(n for n, _ in render.writes[n0:])
        queued = max(queued - 480, 0)           # endpoint: consumes one period per tick
        client.padding = queued
        assert queued > 0
    assert sum(1 for _, f in render.writes if f) == 1


def test_prime_size_follows_device_period_and_falls_back_to_10ms():
    assert _pad_leg(period_ms=10)[0].prime == 720
    assert _period_ms(type("C", (), {"GetDevicePeriod": lambda s: (100000, 30000)})()) == 10.0
    assert _period_ms(type("C", (), {"GetDevicePeriod": lambda s: (250000, 30000)})()) == 25.0

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
    assert render.writes == []


def test_open_leg_sizes_target_to_the_source_period(monkeypatch):
    def client(period):
        c = MagicMock()
        c.GetDevicePeriod.return_value = (period, period)
        c.GetBufferSize.return_value = 9600
        for m in ("Activate", "GetService", "QueryInterface"):
            getattr(c, m).return_value = c
        return c

    e = Engine("src")
    e._fmt = types.SimpleNamespace(contents=types.SimpleNamespace(nBlockAlign=4, nSamplesPerSec=48000))
    e._client = client(200_000)                 # 20 ms source packets
    e._start_capture(0)
    monkeypatch.setattr(e, "_device", lambda d: client(100_000))  # 10 ms leg
    assert e._open_leg("d").target == 1680


def test_late_leg_after_stop_is_closed_not_queued(monkeypatch, opener):
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
    assert not e._results and e.legs == []


# -- MMCSS on the pump thread ---------------------------------------------

class _Avrt:
    def __init__(self, handle=0x1234, boom=False):
        self.reverted = []

        def set_(*a):  # plain functions, so the engine can set restype/argtypes
            if boom:
                raise OSError("no avrt")
            return handle

        def revert(h):
            self.reverted.append(h.value)

        self.AvSetMmThreadCharacteristicsW = set_
        self.AvRevertMmThreadCharacteristics = revert


def _run_engine(monkeypatch, avrt):
    e = _stub_engine(monkeypatch, "_open_source", "_reconcile", "_pump", avrt=avrt)
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


# -- stop(wait=False) only signals; the pump closes itself -----------------

def test_stop_without_wait_returns_at_once_and_the_pump_closes_itself(monkeypatch):
    e = _stub_engine(monkeypatch, "_open_source", "_reconcile", "_pump")
    closing, release, closed_on = threading.Event(), threading.Event(), []

    def slow_close():
        closing.set()
        assert release.wait(1)
        closed_on.append(threading.current_thread())
    monkeypatch.setattr(e, "_close", slow_close)
    e.start()
    pump = e._thread

    t0 = time.perf_counter()
    e.stop(wait=False)
    assert time.perf_counter() - t0 < 0.05
    assert closing.wait(1)              # the pump saw the signal on its own
    assert pump.is_alive()              # and is still closing: nobody waited

    threading.Timer(0.1, release.set).start()
    e.stop()                            # the default still joins
    assert not pump.is_alive()
    assert closed_on == [pump]          # legs and source closed on the pump thread
