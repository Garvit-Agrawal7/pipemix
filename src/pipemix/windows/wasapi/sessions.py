"""The Apps tab — `IAudioSessionManager2` per active render endpoint.

Sessions are per-endpoint, not global: an app playing to a Bluetooth headset
does not show up when you only enumerate the default device, so every active
render endpoint is enumerated and the results are deduped by PID. On Windows
the PID *is* the stream identity — there is no sink-input index to replace it
with, unlike the Linux backend this mirrors
(`src/pipemix/linux/services/backend/pactl_backend.py`).
"""

from __future__ import annotations

import logging
import os

from pipemix.linux.services.backend import BackendError

log = logging.getLogger(__name__)


def _stream_name(display_name: str | None, process_name: str | None, pid: int) -> str:
    """Prefer the app's own display name; most apps never set one — that is
    the common case — so the process image name is the real fallback, and a
    bare PID is the last resort if even `psutil` couldn't find the process.
    """
    if display_name:
        return display_name
    if process_name:
        return process_name
    return f"pid {pid}"


def _dedupe_sessions(records: list[dict], own_pid: int) -> list[dict]:
    """First endpoint wins; the system-sounds session (pid 0) and our own
    process are filtered out, the way the Linux backend filters its own
    combine-sink plumbing out of `list_streams`.

    `records` are plain dicts with at least a "pid" key, in enumeration
    order — endpoint by endpoint, session by session within each endpoint.
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
    """One record per (endpoint, session) pair, across every active render
    endpoint — the raw material `list_streams` dedupes.
    """
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
            })
    return records


def list_streams() -> list[dict]:
    """Application streams now playing: {"id", "name", "sink", "mute"}."""
    records = _dedupe_sessions(_session_records(), os.getpid())
    streams = []
    for r in records:
        session = r["session"]
        process = session.Process
        name = _stream_name(session.DisplayName, process.name() if process else None, r["pid"])
        streams.append({
            "id":   r["pid"],
            "name": name,
            "sink": r["sink"],
            "mute": bool(session.SimpleAudioVolume.GetMute()),
        })
    return streams


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


def set_stream_volume(pid: int, volume: int) -> None:
    from pycaw.constants import IID_Empty

    session = _find_session(pid)
    if session is None:
        raise BackendError(f"No audio session found for pid {pid}")
    vol = max(0, min(100, volume))
    try:
        session.SimpleAudioVolume.SetMasterVolume(vol / 100, IID_Empty)
    except Exception as e:
        raise BackendError(f"Failed to set volume of stream {pid}: {e}") from e
