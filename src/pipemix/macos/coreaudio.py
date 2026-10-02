"""Core Audio over ctypes — the only module in the macOS tree that touches C.

PipeWire's hub sink and `module-loopback` legs have a native macOS
counterpart: a *stacked* aggregate device, which is exactly what Audio MIDI
Setup calls a Multi-Output Device. The HAL fans the one stream out to every
subdevice and drift-corrects each against the clock device, so there is no
capture/render pump to write, unlike `windows/wasapi/engine.py`.

Everything here is plain `ctypes` against CoreAudio and CoreFoundation — no
pyobjc — so it imports the same in a checkout and in a frozen .app, and it
stays importable (if not callable) on any platform, which keeps the tests
headless.

Runnable on its own, before any of the app exists:

    python -m pipemix.macos.coreaudio            # list output devices
"""

from __future__ import annotations

import ctypes
import logging
import struct
from ctypes import POINTER, byref, c_char, c_char_p, c_int32, c_uint32, c_void_p, sizeof
from typing import Callable

log = logging.getLogger(__name__)


def fourcc(code: str) -> int:
    return struct.unpack(">I", code.encode("ascii"))[0]


# ---------- Selectors, scopes, classes ----------

SYSTEM_OBJECT = 1
ELEMENT_MAIN  = 0

SCOPE_GLOBAL = fourcc("glob")
SCOPE_OUTPUT = fourcc("outp")

HW_DEVICES             = fourcc("dev#")
HW_DEFAULT_OUTPUT      = fourcc("dOut")
HW_DEFAULT_SYSTEM_OUT  = fourcc("sOut")
HW_TRANSLATE_UID       = fourcc("uidd")
HW_RUN_LOOP            = fourcc("rnlp")

OBJ_NAME          = fourcc("lnam")
OBJ_OWNED_OBJECTS = fourcc("ownd")

DEV_UID           = fourcc("uid ")
DEV_TRANSPORT     = fourcc("tran")
DEV_STREAMS       = fourcc("stm#")
DEV_IS_HIDDEN     = fourcc("hidn")
DEV_IS_ALIVE      = fourcc("livn")
DEV_RUNNING_SOMEWHERE = fourcc("gone")
DEV_NOMINAL_RATE  = fourcc("nsrt")
DEV_VOLUME_SCALAR = fourcc("volm")
DEV_MUTE          = fourcc("mute")
DEV_STEREO_CHANNELS = fourcc("dch2")

AGG_FULL_SUBDEVICES = fourcc("grup")
AGG_MAIN_SUBDEVICE  = fourcc("amst")
SUB_DRIFT           = fourcc("drft")
CLASS_SUBDEVICE     = fourcc("asub")

DEV_IOPROC_STREAM_USAGE = fourcc("suse")

# Processes (macOS 14.2+)
HW_PROCESS_LIST       = fourcc("prs#")
HW_PID_TO_PROCESS     = fourcc("id2p")
PROC_PID              = fourcc("ppid")
PROC_BUNDLE_ID        = fourcc("pbid")
PROC_RUNNING_OUTPUT   = fourcc("piro")
PROC_DEVICES          = fourcc("pdv#")

# CATapDescription.muteBehavior
TAP_UNMUTED           = 0
TAP_MUTED             = 1
TAP_MUTED_WHEN_TAPPED = 2

TRANSPORT_BUILTIN     = fourcc("bltn")
TRANSPORT_AGGREGATE   = fourcc("grup")
TRANSPORT_VIRTUAL     = fourcc("virt")
TRANSPORT_USB         = fourcc("usb ")
TRANSPORT_BLUETOOTH   = fourcc("blue")
TRANSPORT_BLUETOOTHLE = fourcc("blea")
TRANSPORT_HDMI        = fourcc("hdmi")
TRANSPORT_DISPLAYPORT = fourcc("dprt")
TRANSPORT_AIRPLAY     = fourcc("airp")
TRANSPORT_THUNDERBOLT = fourcc("thun")


class CoreAudioError(Exception):
    """A HAL call returned a non-zero OSStatus."""

    def __init__(self, what: str, status: int) -> None:
        code = struct.pack(">i", status)
        shown = code.decode("ascii") if all(32 <= b < 127 for b in code) else str(status)
        super().__init__(f"{what} failed (OSStatus {shown})")
        self.status = status


class PropertyAddress(ctypes.Structure):
    _fields_ = [("selector", c_uint32), ("scope", c_uint32), ("element", c_uint32)]


def _addr(selector: int, scope: int = SCOPE_GLOBAL, element: int = ELEMENT_MAIN) -> PropertyAddress:
    return PropertyAddress(selector, scope, element)


# AudioObjectPropertyListenerProc
ListenerProc = ctypes.CFUNCTYPE(c_int32, c_uint32, c_uint32, POINTER(PropertyAddress), c_void_p)


# ---------- Library loading ----------

_ca = None
_cf = None


def _libs():
    """Load the frameworks on first use, so importing this module never fails."""
    global _ca, _cf
    if _ca is not None:
        return _ca, _cf

    ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

    ca.AudioObjectGetPropertyDataSize.argtypes = [
        c_uint32, POINTER(PropertyAddress), c_uint32, c_void_p, POINTER(c_uint32)]
    ca.AudioObjectGetPropertyData.argtypes = [
        c_uint32, POINTER(PropertyAddress), c_uint32, c_void_p, POINTER(c_uint32), c_void_p]
    ca.AudioObjectSetPropertyData.argtypes = [
        c_uint32, POINTER(PropertyAddress), c_uint32, c_void_p, c_uint32, c_void_p]
    ca.AudioObjectHasProperty.argtypes = [c_uint32, POINTER(PropertyAddress)]
    ca.AudioObjectHasProperty.restype = ctypes.c_ubyte
    ca.AudioObjectIsPropertySettable.argtypes = [
        c_uint32, POINTER(PropertyAddress), POINTER(ctypes.c_ubyte)]
    ca.AudioObjectAddPropertyListener.argtypes = [
        c_uint32, POINTER(PropertyAddress), ListenerProc, c_void_p]
    ca.AudioObjectRemovePropertyListener.argtypes = [
        c_uint32, POINTER(PropertyAddress), ListenerProc, c_void_p]
    ca.AudioHardwareCreateAggregateDevice.argtypes = [c_void_p, POINTER(c_uint32)]
    ca.AudioHardwareDestroyAggregateDevice.argtypes = [c_uint32]
    ca.AudioDeviceCreateIOProcID.argtypes = [c_uint32, c_void_p, c_void_p, POINTER(c_void_p)]
    ca.AudioDeviceDestroyIOProcID.argtypes = [c_uint32, c_void_p]
    ca.AudioDeviceStart.argtypes = [c_uint32, c_void_p]
    ca.AudioDeviceStop.argtypes = [c_uint32, c_void_p]
    names = ["AudioObjectGetPropertyDataSize", "AudioObjectGetPropertyData",
             "AudioObjectSetPropertyData", "AudioObjectIsPropertySettable",
             "AudioObjectAddPropertyListener", "AudioObjectRemovePropertyListener",
             "AudioHardwareCreateAggregateDevice", "AudioHardwareDestroyAggregateDevice",
             "AudioDeviceCreateIOProcID", "AudioDeviceDestroyIOProcID",
             "AudioDeviceStart", "AudioDeviceStop"]
    # Process taps arrived in macOS 14.2; older systems simply lack the symbols.
    if hasattr(ca, "AudioHardwareCreateProcessTap"):
        ca.AudioHardwareCreateProcessTap.argtypes = [c_void_p, POINTER(c_uint32)]
        ca.AudioHardwareDestroyProcessTap.argtypes = [c_uint32]
        names += ["AudioHardwareCreateProcessTap", "AudioHardwareDestroyProcessTap"]
    for fn in names:
        getattr(ca, fn).restype = c_int32

    cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
    cf.CFStringCreateWithCString.restype = c_void_p
    cf.CFStringGetLength.argtypes = [c_void_p]
    cf.CFStringGetLength.restype = ctypes.c_long
    cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, ctypes.c_long, c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_ubyte
    cf.CFNumberCreate.argtypes = [c_void_p, ctypes.c_long, c_void_p]
    cf.CFNumberCreate.restype = c_void_p
    cf.CFArrayCreate.argtypes = [c_void_p, POINTER(c_void_p), ctypes.c_long, c_void_p]
    cf.CFArrayCreate.restype = c_void_p
    cf.CFDictionaryCreate.argtypes = [
        c_void_p, POINTER(c_void_p), POINTER(c_void_p), ctypes.c_long, c_void_p, c_void_p]
    cf.CFDictionaryCreate.restype = c_void_p
    cf.CFRelease.argtypes = [c_void_p]
    cf.CFRelease.restype = None

    _ca, _cf = ca, cf

    # The HAL delivers its notifications on the main thread's run loop unless
    # told otherwise. Nothing in a CLI run spins that loop, and pywebview's
    # Cocoa loop is busy with the window, so AudioHardwareCreateAggregateDevice
    # — which waits for the HAL to announce the new device — would block
    # forever. NULL hands notifications to the HAL's own thread.
    no_loop = c_void_p(None)
    status = ca.AudioObjectSetPropertyData(
        SYSTEM_OBJECT, byref(_addr(HW_RUN_LOOP)), 0, None, sizeof(no_loop), byref(no_loop))
    if status:
        log.warning("Could not detach the HAL from the main run loop (OSStatus %d)", status)
    return ca, cf


# ---------- CoreFoundation helpers ----------

UTF8 = 0x08000100
CF_NUMBER_SINT32 = 3


def _cf_str(s: str) -> int:
    _, cf = _libs()
    return cf.CFStringCreateWithCString(None, s.encode("utf-8"), UTF8)


def _cf_num(n: int) -> int:
    _, cf = _libs()
    v = c_int32(n)
    return cf.CFNumberCreate(None, CF_NUMBER_SINT32, byref(v))


def _cf_array(items: list[int]) -> int:
    _, cf = _libs()
    arr = (c_void_p * len(items))(*items)
    callbacks = ctypes.addressof(c_char.in_dll(cf, "kCFTypeArrayCallBacks"))
    return cf.CFArrayCreate(None, arr, len(items), callbacks)


def _cf_dict(pairs: dict[str, int]) -> int:
    """String keys, CF object values. The dictionary retains both."""
    _, cf = _libs()
    keys = [_cf_str(k) for k in pairs]
    try:
        k = (c_void_p * len(keys))(*keys)
        v = (c_void_p * len(pairs))(*pairs.values())
        key_cb = ctypes.addressof(c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
        val_cb = ctypes.addressof(c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
        return cf.CFDictionaryCreate(None, k, v, len(keys), key_cb, val_cb)
    finally:
        for key in keys:
            cf.CFRelease(key)


def _release(*refs: int | None) -> None:
    _, cf = _libs()
    for r in refs:
        if r:
            cf.CFRelease(r)


def _py_str(ref: int | None) -> str:
    """A CFStringRef to str, releasing it."""
    if not ref:
        return ""
    _, cf = _libs()
    try:
        size = cf.CFStringGetLength(ref) * 4 + 1
        buf = ctypes.create_string_buffer(size)
        if not cf.CFStringGetCString(ref, buf, size, UTF8):
            return ""
        return buf.value.decode("utf-8")
    finally:
        cf.CFRelease(ref)


# ---------- Property access ----------

def has(obj: int, selector: int, scope: int = SCOPE_GLOBAL, element: int = ELEMENT_MAIN) -> bool:
    ca, _ = _libs()
    return bool(ca.AudioObjectHasProperty(obj, byref(_addr(selector, scope, element))))


def settable(obj: int, selector: int, scope: int = SCOPE_GLOBAL, element: int = ELEMENT_MAIN) -> bool:
    ca, _ = _libs()
    out = ctypes.c_ubyte(0)
    a = _addr(selector, scope, element)
    if not ca.AudioObjectHasProperty(obj, byref(a)):
        return False
    return ca.AudioObjectIsPropertySettable(obj, byref(a), byref(out)) == 0 and bool(out.value)


def _get(obj: int, selector: int, ctype, scope: int = SCOPE_GLOBAL,
         element: int = ELEMENT_MAIN, qualifier=None):
    ca, _ = _libs()
    value = ctype()
    size = c_uint32(sizeof(ctype))
    q_size, q_ptr = (sizeof(qualifier), byref(qualifier)) if qualifier is not None else (0, None)
    status = ca.AudioObjectGetPropertyData(
        obj, byref(_addr(selector, scope, element)), q_size, q_ptr, byref(size), byref(value))
    if status:
        raise CoreAudioError(f"get {selector:#x} on {obj}", status)
    return value.value


def _set(obj: int, selector: int, value, scope: int = SCOPE_GLOBAL, element: int = ELEMENT_MAIN) -> None:
    ca, _ = _libs()
    status = ca.AudioObjectSetPropertyData(
        obj, byref(_addr(selector, scope, element)), 0, None, sizeof(value), byref(value))
    if status:
        raise CoreAudioError(f"set {selector:#x} on {obj}", status)


def _get_ids(obj: int, selector: int, scope: int = SCOPE_GLOBAL, qualifier=None) -> list[int]:
    ca, _ = _libs()
    a = _addr(selector, scope)
    q_size, q_ptr = (sizeof(qualifier), byref(qualifier)) if qualifier is not None else (0, None)
    size = c_uint32(0)
    status = ca.AudioObjectGetPropertyDataSize(obj, byref(a), q_size, q_ptr, byref(size))
    if status:
        raise CoreAudioError(f"size of {selector:#x} on {obj}", status)
    count = size.value // sizeof(c_uint32)
    if not count:
        return []
    buf = (c_uint32 * count)()
    status = ca.AudioObjectGetPropertyData(obj, byref(a), q_size, q_ptr, byref(size), buf)
    if status:
        raise CoreAudioError(f"get {selector:#x} on {obj}", status)
    return list(buf[: size.value // sizeof(c_uint32)])


def _get_str(obj: int, selector: int) -> str:
    return _py_str(_get(obj, selector, c_void_p))


# ---------- Devices ----------

def device_ids() -> list[int]:
    return _get_ids(SYSTEM_OBJECT, HW_DEVICES)


def uid(dev: int) -> str:
    return _get_str(dev, DEV_UID)


def name(dev: int) -> str:
    return _get_str(dev, OBJ_NAME)


def transport(dev: int) -> int:
    try:
        return _get(dev, DEV_TRANSPORT, c_uint32)
    except CoreAudioError:
        return 0


def has_output(dev: int) -> bool:
    try:
        return bool(_get_ids(dev, DEV_STREAMS, SCOPE_OUTPUT))
    except CoreAudioError:
        return False


def is_hidden(dev: int) -> bool:
    try:
        return bool(_get(dev, DEV_IS_HIDDEN, c_uint32))
    except CoreAudioError:
        return False


def is_running_somewhere(dev: int) -> bool:
    return bool(_get(dev, DEV_RUNNING_SOMEWHERE, c_uint32))


def nominal_rate(dev: int) -> float:
    return _get(dev, DEV_NOMINAL_RATE, ctypes.c_double)


def device_for_uid(device_uid: str) -> int | None:
    """The AudioObjectID currently carrying `device_uid`, or None if it is not present."""
    ref = _cf_str(device_uid)
    try:
        q = c_void_p(ref)
        dev = _get(SYSTEM_OBJECT, HW_TRANSLATE_UID, c_uint32, qualifier=q)
    except CoreAudioError:
        return None
    finally:
        _release(ref)
    return dev or None


def default_output() -> int:
    return _get(SYSTEM_OBJECT, HW_DEFAULT_OUTPUT, c_uint32)


def set_default_output(dev: int) -> None:
    _set(SYSTEM_OBJECT, HW_DEFAULT_OUTPUT, c_uint32(dev))
    # System sounds (alerts, the volume-key click) follow too, where allowed.
    try:
        _set(SYSTEM_OBJECT, HW_DEFAULT_SYSTEM_OUT, c_uint32(dev))
    except CoreAudioError as e:
        log.debug("Could not move system sounds: %s", e)


# ---------- Volume ----------

def _volume_elements(dev: int) -> list[int]:
    """Where this device keeps its output volume: the main element if it has
    one, else each of its stereo channels."""
    if has(dev, DEV_VOLUME_SCALAR, SCOPE_OUTPUT, ELEMENT_MAIN):
        return [ELEMENT_MAIN]
    channels = [1, 2]
    try:
        buf = (c_uint32 * 2)()
        ca, _ = _libs()
        size = c_uint32(sizeof(buf))
        if ca.AudioObjectGetPropertyData(dev, byref(_addr(DEV_STEREO_CHANNELS, SCOPE_OUTPUT)),
                                         0, None, byref(size), buf) == 0:
            channels = list(buf)
    except Exception:
        pass
    return [c for c in channels if has(dev, DEV_VOLUME_SCALAR, SCOPE_OUTPUT, c)]


def get_volume(dev: int) -> float | None:
    """0.0–1.0, or None for a device with no volume control."""
    elements = _volume_elements(dev)
    if not elements:
        return None
    levels = [_get(dev, DEV_VOLUME_SCALAR, ctypes.c_float, SCOPE_OUTPUT, e) for e in elements]
    return max(levels)


def set_volume(dev: int, level: float) -> bool:
    """False when the device has no settable volume (some HDMI, some virtual)."""
    level = min(max(level, 0.0), 1.0)
    done = False
    for e in _volume_elements(dev):
        if settable(dev, DEV_VOLUME_SCALAR, SCOPE_OUTPUT, e):
            _set(dev, DEV_VOLUME_SCALAR, ctypes.c_float(level), SCOPE_OUTPUT, e)
            done = True
    return done


def set_mute(dev: int, mute: bool) -> bool:
    if not settable(dev, DEV_MUTE, SCOPE_OUTPUT):
        return False
    _set(dev, DEV_MUTE, c_uint32(1 if mute else 0), SCOPE_OUTPUT)
    return True


# ---------- Aggregate devices ----------

def create_aggregate(agg_uid: str, agg_name: str, sub_uids: list[str], main_uid: str,
                     *, private: bool = False, stacked: bool = True,
                     taps: list[str] = ()) -> int:
    """
    An aggregate over `sub_uids`, clocked by `main_uid`, every other
    subdevice drift-corrected against it.

    The session hub is stacked (a Multi-Output Device) and not private: a
    private aggregate is invisible to every other process, so it could not
    be the system default output. A per-app route is the opposite — private,
    unstacked, and fed by `taps` (tap UUIDs) through an IOProc.
    """
    ca, _ = _libs()
    owned: list[int] = []

    def keep(ref: int) -> int:
        owned.append(ref)
        return ref

    try:
        subs = [
            keep(_cf_dict({
                "uid":   keep(_cf_str(u)),
                "drift": keep(_cf_num(0 if u == main_uid else 1)),
            }))
            for u in sub_uids
        ]
        fields = {
            "uid":        keep(_cf_str(agg_uid)),
            "name":       keep(_cf_str(agg_name)),
            "subdevices": keep(_cf_array(subs)),
            "master":     keep(_cf_str(main_uid)),
            "private":    keep(_cf_num(1 if private else 0)),
            "stacked":    keep(_cf_num(1 if stacked else 0)),
        }
        if taps:
            fields["taps"] = keep(_cf_array([
                keep(_cf_dict({"uid": keep(_cf_str(t)), "drift": keep(_cf_num(1))}))
                for t in taps
            ]))
            fields["tapautostart"] = keep(_cf_num(1))
        desc = keep(_cf_dict(fields))
        out = c_uint32(0)
        status = ca.AudioHardwareCreateAggregateDevice(desc, byref(out))
        if status:
            raise CoreAudioError("AudioHardwareCreateAggregateDevice", status)
        return out.value
    finally:
        _release(*owned)


# ---------- Processes and taps (macOS 14.2+) ----------

def taps_supported() -> bool:
    ca, _ = _libs()
    return hasattr(ca, "AudioHardwareCreateProcessTap")


def process_ids() -> list[int]:
    """AudioObjectIDs of every process that has opened audio."""
    return _get_ids(SYSTEM_OBJECT, HW_PROCESS_LIST)


def process_for_pid(pid: int) -> int | None:
    try:
        obj = _get(SYSTEM_OBJECT, HW_PID_TO_PROCESS, c_uint32, qualifier=c_int32(pid))
    except CoreAudioError:
        return None
    return obj or None


def process_pid(proc: int) -> int:
    return _get(proc, PROC_PID, c_int32)


def process_bundle_id(proc: int) -> str:
    try:
        return _get_str(proc, PROC_BUNDLE_ID)
    except CoreAudioError:
        return ""


def process_is_playing(proc: int) -> bool:
    try:
        return bool(_get(proc, PROC_RUNNING_OUTPUT, c_uint32))
    except CoreAudioError:
        return False


def process_output_devices(proc: int) -> list[int]:
    try:
        return _get_ids(proc, PROC_DEVICES, SCOPE_OUTPUT)
    except CoreAudioError:
        return []


def create_process_tap(proc: int, mute: int, name: str) -> tuple[int, str]:
    """A private stereo-mixdown tap of one process. Returns (tap id, tap UUID).

    `mute` is a CATapMuteBehavior: TAP_MUTED silences the app everywhere
    (that is all "mute" needs), TAP_MUTED_WHEN_TAPPED silences it only while
    something reads the tap — so if PipeMix's IOProc stops, the app is heard
    on its normal output again rather than lost.
    """
    import objc
    ca, _ = _libs()
    CATapDescription = objc.lookUpClass("CATapDescription")
    desc = CATapDescription.alloc().initStereoMixdownOfProcesses_([proc])
    desc.setMuteBehavior_(mute)
    desc.setPrivate_(True)
    desc.setName_(name)
    out = c_uint32(0)
    status = ca.AudioHardwareCreateProcessTap(objc.pyobjc_id(desc), byref(out))
    if status:
        raise CoreAudioError("AudioHardwareCreateProcessTap", status)
    return out.value, str(desc.UUID().UUIDString())


def destroy_process_tap(tap: int) -> None:
    ca, _ = _libs()
    status = ca.AudioHardwareDestroyProcessTap(tap)
    if status:
        raise CoreAudioError("AudioHardwareDestroyProcessTap", status)


# ---------- IOProcs ----------

def create_ioproc(dev: int, proc: int, client_data) -> int:
    """Register the C function at address `proc` as an IOProc on `dev`."""
    ca, _ = _libs()
    proc_id = c_void_p(0)
    status = ca.AudioDeviceCreateIOProcID(dev, proc, ctypes.cast(client_data, c_void_p)
                                          if client_data is not None else None, byref(proc_id))
    if status:
        raise CoreAudioError("AudioDeviceCreateIOProcID", status)
    return proc_id.value


def only_last_inputs(dev: int, proc_id: int, keep: int) -> None:
    """Switch off every input stream of `dev` for this IOProc but the last `keep`.

    An aggregate's inputs are its subdevices' inputs followed by its taps.
    Leaving a Bluetooth headset's microphone running would flip it into
    hands-free mode (mono, telephone quality) and light the mic indicator,
    all for audio this IOProc never reads.
    """
    ca, _ = _libs()
    a = _addr(DEV_IOPROC_STREAM_USAGE, fourcc("inpt"))
    size = c_uint32(0)
    if ca.AudioObjectGetPropertyDataSize(dev, byref(a), 0, None, byref(size)):
        return
    buf = ctypes.create_string_buffer(size.value)
    ctypes.memmove(buf, struct.pack("=Q", proc_id), 8)  # mIOProc is in/out
    if ca.AudioObjectGetPropertyData(dev, byref(a), 0, None, byref(size), buf):
        return
    count = struct.unpack_from("=I", buf, 8)[0]
    for i in range(count):
        struct.pack_into("=I", buf, 12 + 4 * i, 1 if i >= count - keep else 0)
    status = ca.AudioObjectSetPropertyData(dev, byref(a), 0, None, size.value, buf)
    if status:
        log.debug("Could not limit input streams on %d: %d", dev, status)


def start_ioproc(dev: int, proc_id: int) -> None:
    ca, _ = _libs()
    status = ca.AudioDeviceStart(dev, proc_id)
    if status:
        raise CoreAudioError("AudioDeviceStart", status)


def destroy_ioproc(dev: int, proc_id: int) -> None:
    ca, _ = _libs()
    ca.AudioDeviceStop(dev, proc_id)
    ca.AudioDeviceDestroyIOProcID(dev, proc_id)


def set_subdevices(agg: int, sub_uids: list[str], main_uid: str) -> None:
    """Change a live aggregate's subdevices and clock, then drift-correct the rest."""
    strs = [_cf_str(u) for u in sub_uids]
    arr = _cf_array(strs)
    main = _cf_str(main_uid)
    try:
        _set(agg, AGG_FULL_SUBDEVICES, c_void_p(arr))
        _set(agg, AGG_MAIN_SUBDEVICE, c_void_p(main))
    finally:
        _release(arr, main, *strs)
    set_drift(agg, main_uid)


def set_drift(agg: int, main_uid: str) -> None:
    """Drift correction on for every subdevice but the clock."""
    try:
        subs = _get_ids(agg, OBJ_OWNED_OBJECTS, qualifier=c_uint32(CLASS_SUBDEVICE))
    except CoreAudioError as e:
        log.debug("Could not list subdevices of %d: %s", agg, e)
        return
    for sub in subs:
        try:
            want = 0 if uid(sub) == main_uid else 1
            if settable(sub, SUB_DRIFT):
                _set(sub, SUB_DRIFT, c_uint32(want))
        except CoreAudioError as e:
            log.debug("Could not set drift on subdevice %d: %s", sub, e)


def destroy_aggregate(agg: int) -> None:
    ca, _ = _libs()
    status = ca.AudioHardwareDestroyAggregateDevice(agg)
    if status:
        raise CoreAudioError("AudioHardwareDestroyAggregateDevice", status)


# ---------- Notifications ----------

class Listener:
    """Calls `fn()` (no arguments, on a HAL thread) whenever `selector` on
    `obj` changes. Keep a reference: the C callback dies with this object."""

    def __init__(self, fn: Callable[[], None], obj: int = SYSTEM_OBJECT,
                 selector: int = HW_DEVICES) -> None:
        self._fn = fn
        self._obj = obj
        self._addr = _addr(selector)
        self._proc = ListenerProc(self._fire)
        self._added = False

    def _fire(self, _obj, _n, _addrs, _data) -> int:
        try:
            self._fn()
        except Exception:
            log.exception("Core Audio listener raised")
        return 0

    def add(self) -> None:
        ca, _ = _libs()
        status = ca.AudioObjectAddPropertyListener(self._obj, byref(self._addr), self._proc, None)
        if status:
            raise CoreAudioError("AudioObjectAddPropertyListener", status)
        self._added = True

    def remove(self) -> None:
        if not self._added:
            return
        ca, _ = _libs()
        ca.AudioObjectRemovePropertyListener(self._obj, byref(self._addr), self._proc, None)
        self._added = False


if __name__ == "__main__":
    default = default_output()
    for d in device_ids():
        if not has_output(d):
            continue
        t = struct.pack(">I", transport(d)).decode("ascii", "replace")
        vol = get_volume(d)
        print(f"{'*' if d == default else ' '} {d:4} [{t}] {name(d):<32} "
              f"vol={'-' if vol is None else f'{vol:.2f}'} uid={uid(d)}")
