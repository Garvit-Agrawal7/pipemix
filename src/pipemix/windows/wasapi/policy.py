from __future__ import annotations

import ctypes
import logging
from functools import lru_cache

log = logging.getLogger(__name__)


def set_default(device_id: str) -> None:
    """Set `device_id` as the default for all three roles, so system sounds
    (eConsole) and calls (eCommunications) move too, not just eMultimedia."""
    from pycaw.constants import ERole
    from pycaw.utils import AudioUtilities

    AudioUtilities.SetDefaultDevice(
        device_id, [ERole.eConsole, ERole.eMultimedia, ERole.eCommunications]
    )
    log.info("Default output set to %s (all roles).", device_id)


# -- per-app routing: the undocumented half ---------------------------------

_CLASS_ID = "Windows.Media.Internal.AudioPolicyConfig"

# The factory's WinRT IID changed in Win11 21H2 and only one answers per build
# (26200 takes the first), so probing in order also tells us the layout.
_IID_WIN11 = "{ab3d4648-e242-459f-b02f-541c70306324}"
_IID_WIN10 = "{2a59116d-6c4f-45e0-a74f-707e3fef9258}"

# IUnknown (0-2) + IInspectable (3-5) + undocumented slots; Win11 adds two
# (add/remove _ChatContextChanged), so the slot moves. Verified on Win11 26200:
# slot 25 returns S_OK and writes HKCU\...\Audio\PolicyConfig\PropertyStore.
_SET_PERSISTED_DEFAULT_ENDPOINT = {_IID_WIN11: 25, _IID_WIN10: 23}

# Wrapping form the policy config factory demands — the raw endpoint id is
# rejected outright.
_MMDEVAPI_TOKEN = r"\\?\SWD#MMDEVAPI#"
_DEVINTERFACE_AUDIO_RENDER = "#{e6327cad-dcec-4949-ae8a-991e976a79d2}"


def _pack_render_id(device_id: str) -> str:
    return f"{_MMDEVAPI_TOKEN}{device_id}{_DEVINTERFACE_AUDIO_RENDER}"


@lru_cache(maxsize=1)
def _combase():
    dll = ctypes.WinDLL("combase")
    dll.WindowsCreateString.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
    ]
    dll.WindowsCreateString.restype = ctypes.c_long
    dll.WindowsDeleteString.argtypes = [ctypes.c_void_p]
    dll.WindowsDeleteString.restype = ctypes.c_long
    return dll


def _get_factory(class_id: str, iid: str) -> ctypes.c_void_p | None:
    import comtypes

    dll = _combase()
    ro_get = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(comtypes.GUID), ctypes.POINTER(ctypes.c_void_p)
    )(("RoGetActivationFactory", dll))

    class_id_h = ctypes.c_void_p()
    if dll.WindowsCreateString(class_id, len(class_id), ctypes.byref(class_id_h)) < 0:
        return None
    try:
        factory = ctypes.c_void_p()
        hr = ro_get(class_id_h, ctypes.byref(comtypes.GUID(iid)), ctypes.byref(factory))
        return factory if hr >= 0 and factory else None
    finally:
        dll.WindowsDeleteString(class_id_h)


def _vtable_fn(ptr: ctypes.c_void_p, index: int, functype):
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return functype(vtable[index])


class AppRouter:
    """Per-app default output, via the undocumented AudioPolicyConfig factory.

    `available` is False when no known vtable layout answers (unsupported or newer
    Windows); callers must then hide per-app routing. Nothing else depends on it.
    """

    def __init__(self) -> None:
        self._ptr: ctypes.c_void_p | None = None
        self._slot = 0
        self.available = False
        for iid in (_IID_WIN11, _IID_WIN10):
            try:
                ptr = _get_factory(_CLASS_ID, iid)
            except OSError:
                ptr = None
            if ptr:
                self._ptr = ptr
                self._slot = _SET_PERSISTED_DEFAULT_ENDPOINT[iid]
                self.available = True
                break
        if not self.available:
            log.warning(
                "AudioPolicyConfig factory unavailable on this Windows build — "
                "per-app output routing disabled; volume and mute still work."
            )

    def route(self, pid: int, device_id: str | None) -> None:
        """Persist `device_id` as pid's render default, or clear it if None.

        A preference the app picks up when it next opens a stream, not a live move.
        None returns it to the machine default; every app we moved needs that when
        a session stops. Fails for a pid with no audio session (not in the Apps tab).
        """
        if not self.available:
            log.warning("Per-app routing unavailable; ignoring route(%d, %s)", pid, device_id)
            return

        from pycaw.constants import EDataFlow, ERole

        dll = _combase()
        device_h = ctypes.c_void_p()
        if device_id is not None:
            packed = _pack_render_id(device_id)
            if dll.WindowsCreateString(packed, len(packed), ctypes.byref(device_h)) < 0:
                raise OSError("WindowsCreateString failed for device id")
        try:
            set_fn = _vtable_fn(
                self._ptr, self._slot,
                ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.c_uint32,
                    ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                ),
            )
            failures = []
            for role in (ERole.eConsole.value, ERole.eMultimedia.value):
                hr = set_fn(self._ptr, pid, EDataFlow.eRender.value, role, device_h)
                if hr < 0:
                    failures.append((role, hr))
            # Roles are set independently and one can be refused while another
            # is accepted, so only a clean sweep of failures is an error.
            if len(failures) == 2:
                hr = failures[0][1]
                if hr & 0xFFFFFFFF == 0x80070057:
                    # E_INVALIDARG here means the pid, not the device id — the
                    # factory refuses a process that owns no audio session.
                    raise OSError(
                        f"Windows will not route pid {pid}: it has no audio session to move."
                    )
                raise OSError(f"SetPersistedDefaultAudioEndpoint failed: 0x{hr & 0xFFFFFFFF:08x}")
        finally:
            dll.WindowsDeleteString(device_h)
        if device_id is None:
            log.info("Cleared per-app output for pid %d (back to machine default).", pid)
        else:
            log.info("Routed pid %d to %s (persisted, not live).", pid, device_id)
