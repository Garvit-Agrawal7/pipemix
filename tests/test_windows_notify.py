"""Which thread the endpoint notifications get registered on.

No COM — `pycaw` and `comtypes` are stubbed — because the thing worth pinning
here is not what WASAPI does, it is which apartment we ask it from.

`comtypes.CoInitialize()` puts the calling thread in a single-threaded
apartment, and COM delivers calls to an object registered from an STA only
while that thread pumps a Windows message loop. `main.py --cli` blocks on
`threading.Event().wait()` and never pumps, so registering on the caller's
thread meant hotplug notifications were queued and never delivered: a headset
could disconnect mid-session and nothing noticed. Registration therefore has
to happen on the monitor's own MTA worker, and this test fails if it moves
back.

The stubs go in through `monkeypatch.setitem`, so they are torn down again —
a fake `comtypes` left in `sys.modules` breaks every other test in the run.
"""

from __future__ import annotations

import importlib
import sys
import threading
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

NOTIFY = "pipemix.windows.wasapi.notify"


class _FakeEnumerator:
    def __init__(self, log):
        self._log = log

    def RegisterEndpointNotificationCallback(self, client):
        self._log.append(("register", threading.current_thread().name))

    def UnregisterEndpointNotificationCallback(self, client):
        self._log.append(("unregister", threading.current_thread().name))


def _stub_modules(log) -> dict[str, ModuleType]:
    comtypes = ModuleType("comtypes")
    comtypes.COINIT_MULTITHREADED = 0x0
    comtypes.CoInitializeEx = lambda flags: log.append(
        ("coinit", threading.current_thread().name))
    comtypes.CoUninitialize = lambda: None
    comtypes.CoInitialize = lambda: None
    comtypes.COMObject = type("COMObject", (), {"__init__": lambda self, *a, **k: None})

    mm = ModuleType("pycaw.api.mmdeviceapi")
    mm.IMMNotificationClient = object
    mm.IMMEndpoint = object

    consts = ModuleType("pycaw.constants")
    consts.EDataFlow = type("EDataFlow", (), {"eRender": type("v", (), {"value": 0})()})
    consts.DEVICE_STATE = type("DEVICE_STATE", (), {"ACTIVE": type("v", (), {"value": 1})()})

    utils = ModuleType("pycaw.utils")
    utils.AudioUtilities = type(
        "AudioUtilities", (), {"GetDeviceEnumerator": staticmethod(lambda: _FakeEnumerator(log))}
    )

    return {
        "comtypes": comtypes,
        "pycaw": ModuleType("pycaw"),
        "pycaw.api": ModuleType("pycaw.api"),
        "pycaw.api.mmdeviceapi": mm,
        "pycaw.constants": consts,
        "pycaw.utils": utils,
    }


@pytest.fixture
def monitor_with_log(monkeypatch):
    """A DeviceMonitor built against stubs, plus the call log it writes to."""
    log: list = []
    for name, mod in _stub_modules(log).items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delitem(sys.modules, NOTIFY, raising=False)

    notify = importlib.import_module(NOTIFY)
    monkeypatch.setattr(notify.DeviceMonitor, "_render_endpoints", lambda self: [])
    yield notify.DeviceMonitor(), log

    # The imported module has the stubs bound into it; drop it so anything
    # importing it later rebuilds against the real comtypes.
    sys.modules.pop(NOTIFY, None)


def test_registration_happens_off_the_calling_thread(monitor_with_log):
    monitor, log = monitor_with_log
    caller = threading.current_thread().name

    monitor.start()
    monitor.stop()

    registered_on = [entry[1] for entry in log if entry[0] == "register"]
    entered_apartment_on = [entry[1] for entry in log if entry[0] == "coinit"]

    assert registered_on, f"never registered; log={log}"
    assert caller not in registered_on, (
        f"registered on the calling thread {caller!r} — COM marshals "
        f"notifications back to an STA that never pumps a message loop"
    )
    assert entered_apartment_on, f"worker never entered an apartment; log={log}"
    assert entered_apartment_on[0] == registered_on[0], (
        "the thread that registered is not the one that entered the apartment"
    )


def test_unregisters_on_the_same_thread_it_registered_on(monitor_with_log):
    # COM requires it, and getting this wrong leaks the callback for the life
    # of the process.
    monitor, log = monitor_with_log
    monitor.start()
    monitor.stop()

    registered_on = [entry[1] for entry in log if entry[0] == "register"]
    unregistered_on = [entry[1] for entry in log if entry[0] == "unregister"]
    assert unregistered_on, f"never unregistered; log={log}"
    assert unregistered_on == registered_on
