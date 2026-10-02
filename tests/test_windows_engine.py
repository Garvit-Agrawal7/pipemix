from __future__ import annotations

import ctypes
import sys
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


def test_invalidated_leg_is_dropped_and_reopened_without_starving_the_rest(monkeypatch):
    e = Engine("src")
    e._capture = _NoCapture()
    dead, alive = _FakeLeg("dead", fail=True), _FakeLeg("alive")
    e._legs = {"dead": dead, "alive": alive}  # the dead one first, so it would block the other
    e.set_legs(["dead", "alive"])

    e._pump()

    assert alive.writes == 1
    assert "dead" not in e._legs

    fresh = _FakeLeg("dead")
    monkeypatch.setattr(e, "_open_leg", lambda device_id: fresh)
    e._reconcile()
    assert e._legs["dead"] is fresh


def test_leg_that_fails_to_open_is_retried_once_a_second(monkeypatch):
    import pipemix.windows.wasapi.engine as engine_mod

    clock = [100.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])
    e = Engine("src")
    e.set_legs(["busy"])
    attempts = []

    def _open(device_id):
        attempts.append(clock[0])
        if len(attempts) == 1:
            raise OSError(-2004287478, "device in use")  # AUDCLNT_E_DEVICE_IN_USE
        return _FakeLeg(device_id)
    monkeypatch.setattr(e, "_open_leg", _open)

    e._reconcile()                     # fails
    clock[0] += 0.5
    e._reconcile()                     # too soon: not tried again
    assert attempts == [100.0]
    assert "busy" not in e._legs

    clock[0] += 0.6
    e._reconcile()                     # a second on: tried and opened
    assert len(attempts) == 2
    assert "busy" in e._legs


def test_retry_is_forgotten_when_the_leg_is_no_longer_wanted(monkeypatch):
    e = Engine("src")
    e.set_legs(["gone"])
    monkeypatch.setattr(e, "_open_leg", lambda d: (_ for _ in ()).throw(OSError("nope")))
    e._reconcile()
    e.set_legs([])
    e._reconcile()
    assert not e._retry_at
