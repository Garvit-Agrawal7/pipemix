from __future__ import annotations

import logging

from pipemix.models import BackendError

log = logging.getLogger(__name__)


def _endpoint_volume(device_id: str):
    import comtypes
    from pycaw.api.endpointvolume import IAudioEndpointVolume
    from pycaw.utils import AudioUtilities

    dev = AudioUtilities.GetDeviceEnumerator().GetDevice(device_id)
    iface = dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
    return iface.QueryInterface(IAudioEndpointVolume)


def get_volume(device_id: str) -> int:
    """0-100, or 100 if it cannot be read."""
    try:
        return round(_endpoint_volume(device_id).GetMasterVolumeLevelScalar() * 100)
    except Exception as e:
        log.warning("Failed to get volume for %s: %s", device_id, e)
        return 100


def set_volume(device_id: str, volume: int) -> None:
    from pycaw.constants import IID_Empty

    vol = max(0, min(100, volume))
    try:
        _endpoint_volume(device_id).SetMasterVolumeLevelScalar(vol / 100, IID_Empty)
    except Exception as e:
        raise BackendError(f"Failed to set volume of {device_id} to {vol}%: {e}") from e


def set_mute(device_id: str, mute: bool) -> None:
    from pycaw.constants import IID_Empty

    try:
        _endpoint_volume(device_id).SetMute(mute, IID_Empty)
    except Exception as e:
        raise BackendError(f"Failed to set mute state for {device_id}: {e}") from e
