from __future__ import annotations

from ctypes import (
    HRESULT, POINTER, Structure, addressof, c_byte, c_int, c_uint32, c_uint64,
    c_ulong, c_ushort, c_void_p, sizeof,
)
from ctypes.wintypes import DWORD

from comtypes import COMMETHOD, GUID, IUnknown

# IAudioClient::Initialize
AUDCLNT_SHAREMODE_SHARED = 0

AUDCLNT_STREAMFLAGS_LOOPBACK            = 0x00020000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
# Lets a leg run at a rate and channel count that differ from what we feed it,
# which is the whole reason "Bluetooth at 48k, speakers at 44.1k" is a non-problem.
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM      = 0x80000000

# IAudioCaptureClient::GetBuffer flags
AUDCLNT_BUFFERFLAGS_SILENT = 0x2

REFTIMES_PER_SEC = 10_000_000  # a REFERENCE_TIME is 100ns

# Process loopback (Win10 2004+): ActivateAudioInterfaceAsync on this virtual
# device, with the params below wrapped in a VT_BLOB PROPVARIANT.
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = r"VAD\Process_Loopback"
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK       = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
VT_BLOB = 65


class IAudioRenderClient(IUnknown):
    _iid_ = GUID("{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}")
    _methods_ = (
        COMMETHOD(
            [], HRESULT, "GetBuffer",
            (["in"], c_uint32, "NumFramesRequested"),
            (["out"], POINTER(POINTER(c_byte)), "ppData"),
        ),
        COMMETHOD(
            [], HRESULT, "ReleaseBuffer",
            (["in"], c_uint32, "NumFramesWritten"),
            (["in"], DWORD, "dwFlags"),
        ),
    )


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
    _methods_ = (
        COMMETHOD(
            [], HRESULT, "GetBuffer",
            (["out"], POINTER(POINTER(c_byte)), "ppData"),
            (["out"], POINTER(c_uint32), "pNumFramesToRead"),
            (["out"], POINTER(DWORD), "pdwFlags"),
            (["out"], POINTER(c_uint64), "pu64DevicePosition"),
            (["out"], POINTER(c_uint64), "pu64QPCPosition"),
        ),
        COMMETHOD(
            [], HRESULT, "ReleaseBuffer",
            (["in"], c_uint32, "NumFramesRead"),
        ),
        COMMETHOD(
            [], HRESULT, "GetNextPacketSize",
            (["out"], POINTER(c_uint32), "pNumFramesInNextPacket"),
        ),
    )


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = (
        COMMETHOD(
            [], HRESULT, "GetActivateResult",
            (["out"], POINTER(HRESULT), "hr"),
            (["out"], POINTER(POINTER(IUnknown)), "itf"),
        ),
    )


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = (
        COMMETHOD(
            [], HRESULT, "ActivateCompleted",
            (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "op"),
        ),
    )


class IAgileObject(IUnknown):
    # Marker only: mmdevapi calls the completion handler from its own thread,
    # and refuses a handler that isn't agile.
    _iid_ = GUID("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
    _methods_ = ()


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(Structure):
    _fields_ = [("TargetProcessId", DWORD), ("ProcessLoopbackMode", c_int)]


class AUDIOCLIENT_ACTIVATION_PARAMS(Structure):
    _fields_ = [
        ("ActivationType", c_int),
        ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


class PROPVARIANT_BLOB(Structure):
    """A PROPVARIANT holding only the VT_BLOB arm, which is all we pass."""
    _fields_ = [
        ("vt", c_ushort),
        ("wReserved1", c_ushort),
        ("wReserved2", c_ushort),
        ("wReserved3", c_ushort),
        ("cbSize", c_ulong),
        ("pBlobData", c_void_p),
    ]


def loopback_params(pid: int) -> tuple[AUDIOCLIENT_ACTIVATION_PARAMS, PROPVARIANT_BLOB]:
    """Activation params capturing `pid` and its child processes.

    Returns the params too, because the PROPVARIANT only points at them: the
    caller has to keep both alive until activation completes.
    """
    params = AUDIOCLIENT_ACTIVATION_PARAMS(
        AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(
            pid, PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
        ),
    )
    blob = PROPVARIANT_BLOB(vt=VT_BLOB, cbSize=sizeof(params), pBlobData=addressof(params))
    return params, blob


def loopback_format():
    """The format a process-loopback client is initialized with.

    Its `GetMixFormat` is not supported, so we pick one: 48 kHz stereo float.
    `AUTOCONVERTPCM` on the source and on every leg makes the choice harmless.
    """
    from pycaw.api.audioclient import WAVEFORMATEX

    return WAVEFORMATEX(
        wFormatTag=3,  # WAVE_FORMAT_IEEE_FLOAT
        nChannels=2,
        nSamplesPerSec=48000,
        nAvgBytesPerSec=48000 * 8,
        nBlockAlign=8,
        wBitsPerSample=32,
        cbSize=0,
    )
