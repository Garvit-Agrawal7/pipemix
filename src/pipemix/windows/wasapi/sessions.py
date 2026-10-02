from __future__ import annotations

import ctypes
import logging
import os
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from xml.etree import ElementTree

from pipemix.linux.services.backend import BackendError

log = logging.getLogger(__name__)


def _stream_name(display_name: str | None, process_name: str | None, pid: int) -> str:
    """The app's display name, else its process name, else "pid {pid}"."""
    return display_name or process_name or f"pid {pid}"


@lru_cache(maxsize=64)
def _package_name(exe: str) -> str | None:
    """The Store package's display name for an exe inside one, else None.

    Packaged apps often play through a helper (Apple Music via AMPLibraryAgent.exe),
    so the package name means more than the process name. The WindowsApps folder is
    the package full name; DisplayName is usually an ms-resource for SHLoadIndirectString.
    """
    try:
        path = PureWindowsPath(exe)
        i = [p.lower() for p in path.parts].index("windowsapps")
        full = path.parts[i + 1]
        root = ElementTree.parse(Path(*path.parts[:i + 2], "AppxManifest.xml")).getroot()
        props = next(e for e in root if e.tag.endswith("}Properties"))
        name = next(e.text for e in props if e.tag.endswith("}DisplayName"))
        if not name.startswith("ms-resource:"):
            return name
        key = name[len("ms-resource:"):]
        pkg = full.split("_")[0]
        uri = f"ms-resource://{pkg}/{key}" if "/" in key else f"ms-resource://{pkg}/resources/{key}"
        buf = ctypes.create_unicode_buffer(512)
        if ctypes.windll.shlwapi.SHLoadIndirectString(f"@{{{full}?{uri}}}", buf, 512, None) == 0:
            return buf.value or None
    except Exception as e:
        log.debug("No package name for %s: %s", exe, e)
    return None


def _process_exe(process) -> str | None:
    if process is None:
        return None
    try:
        return process.exe()
    except Exception:
        return None


def _process_label(process, exe: str | None) -> str | None:
    if process is None:
        return None
    if exe is None:
        return process.name()
    return _package_name(exe) or process.name()


def _dedupe_sessions(records: list[dict], own_pid: int) -> list[dict]:
    """First endpoint wins; system sounds (pid 0) and our own process are dropped.

    `records` are dicts with at least "pid", in enumeration order.
    """
    seen: set[int] = set()
    out = []
    for r in records:
        pid = r["pid"]
        if pid == 0 or pid == own_pid or pid in seen:
            continue
        seen.add(pid)
        out.append(r)
    return out


def _session_records() -> list[dict]:
    """One record per (endpoint, session) across active render endpoints, for
    `list_streams` to dedupe."""
    import comtypes
    from pycaw.api.audiopolicy import IAudioSessionControl2, IAudioSessionManager2
    from pycaw.constants import DEVICE_STATE, EDataFlow
    from pycaw.utils import AudioSession, AudioUtilities

    records: list[dict] = []
    for device in AudioUtilities.GetAllDevices(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value):
        try:
            iface = device._dev.Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None)
            manager = iface.QueryInterface(IAudioSessionManager2)
            enumerator = manager.GetSessionEnumerator()
        except Exception as e:
            log.debug("No session manager on %s: %s", device.FriendlyName, e)
            continue

        for i in range(enumerator.GetCount()):
            ctl = enumerator.GetSession(i)
            if ctl is None:
                continue
            session = AudioSession(ctl.QueryInterface(IAudioSessionControl2))
            records.append({
                "pid": session.ProcessId,
                "session": session,
                "sink": device.FriendlyName,
                "endpoint": device.id,
                "active": ctl.GetState() == 1,  # AudioSessionStateActive
            })
    # An app keeps idle sessions on old endpoints; the playing one must win the
    # dedupe, or its label and mute come from a stale session.
    records.sort(key=lambda r: not r["active"])
    return records


def list_streams() -> list[dict]:
    """Application streams now playing: {"id", "name", "sink", "endpoint", "active", "mute", "exe"}."""
    records = _dedupe_sessions(_session_records(), os.getpid())
    streams = []
    for r in records:
        session = r["session"]
        process = session.Process
        exe = _process_exe(process)
        name = _stream_name(session.DisplayName, _process_label(process, exe), r["pid"])
        streams.append({
            "id":       r["pid"],
            "name":     name,
            "sink":     r["sink"],
            "endpoint": r["endpoint"],
            "active":   r["active"],
            "mute":     bool(session.SimpleAudioVolume.GetMute()),
            "exe":      exe,
        })
    # A packaged app's window and its audio helper get the same package name;
    # an idle twin of a playing app is noise, and routing it does nothing.
    playing = {s["name"] for s in streams if s["active"]}
    return [s for s in streams if s["active"] or s["name"] not in playing]


def _find_session(pid: int):
    for record in _session_records():
        if record["pid"] == pid:
            return record["session"]
    return None


def set_stream_mute(pid: int, mute: bool) -> None:
    from pycaw.constants import IID_Empty

    session = _find_session(pid)
    if session is None:
        raise BackendError(f"No audio session found for pid {pid}")
    try:
        session.SimpleAudioVolume.SetMute(mute, IID_Empty)
    except Exception as e:
        raise BackendError(f"Failed to mute stream {pid}: {e}") from e
