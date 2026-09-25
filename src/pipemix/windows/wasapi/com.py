"""The WASAPI pieces `pycaw` does not declare, plus the constants we need.

Everything else — `IMMDeviceEnumerator`, `IMMDevice`, `IAudioClient`,
`IPolicyConfig`, the session and endpoint-volume interfaces — comes from
pycaw, which declares them statically with plain `comtypes`. Only the
streaming interfaces are missing, because pycaw never reads or writes audio.

Declared by hand rather than through `comtypes.client.GetModule` for the same
reason pycaw does: `GetModule` writes generated wrappers to a cache at import
time, which does not survive being frozen by PyInstaller.
"""

from __future__ import annotations

from ctypes import HRESULT, POINTER, c_byte, c_uint32, c_uint64
from ctypes.wintypes import DWORD

from comtypes import COMMETHOD, GUID, IUnknown

# IAudioClient::Initialize
AUDCLNT_SHAREMODE_SHARED = 0

AUDCLNT_STREAMFLAGS_LOOPBACK            = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK       = 0x00040000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
# Lets a leg run at a rate and channel count that differ from what we feed it,
# which is the whole reason "Bluetooth at 48k, speakers at 44.1k" is a non-problem.
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM      = 0x80000000

# IAudioCaptureClient::GetBuffer flags
AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
AUDCLNT_BUFFERFLAGS_SILENT             = 0x2

REFTIMES_PER_SEC = 10_000_000  # a REFERENCE_TIME is 100ns


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
