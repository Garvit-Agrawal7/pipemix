"""Per-app routing: the slot choice and the failure handling.

No COM — the vtable call and `combase` are stubbed, so this runs on Linux CI
alongside the rest. What it pins down is the two things that were actually
wrong when this was first written against real Windows.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipemix.windows.wasapi import policy

E_INVALIDARG = -0x7FF8FFA9  # 0x80070057 as a signed HRESULT


class FakeCombase:
    """Enough of combase for route(): strings always succeed, deletes are free."""

    def WindowsCreateString(self, text, length, out):
        out._obj.value = 0xBEEF
        return 0

    def WindowsDeleteString(self, handle):
        return 0


@pytest.fixture
def router(monkeypatch):
    monkeypatch.setattr(policy, "_combase", lambda: FakeCombase())
    r = policy.AppRouter.__new__(policy.AppRouter)   # no real factory activation
    r._ptr = ctypes.c_void_p(1)
    r._slot = policy._SET_PERSISTED_DEFAULT_ENDPOINT[policy._IID_WIN11]
    r.available = True
    return r


def calls_returning(*results):
    """Stub _vtable_fn so each successive call returns the next result."""
    seq = list(results)
    log = []

    def fake_vtable_fn(ptr, index, functype):
        def call(_ptr, pid, flow, role, device):
            log.append((index, pid, flow, role, device))
            return seq.pop(0) if seq else 0
        return call

    return fake_vtable_fn, log


def test_win10_and_win11_use_different_slots():
    # They are not interchangeable: Win11's interface carries two extra slots,
    # so indexing both the same way calls the wrong method entirely.
    win11 = policy._SET_PERSISTED_DEFAULT_ENDPOINT[policy._IID_WIN11]
    win10 = policy._SET_PERSISTED_DEFAULT_ENDPOINT[policy._IID_WIN10]
    assert win11 != win10
    assert win11 - win10 == 2


def test_packed_id_wraps_the_endpoint(monkeypatch):
    packed = policy._pack_render_id("{0.0.0.00000000}.{abc}")
    assert packed.startswith("\\\\?\\SWD#MMDEVAPI#")
    assert packed.endswith("#{e6327cad-dcec-4949-ae8a-991e976a79d2}")
    assert "{0.0.0.00000000}.{abc}" in packed


def test_one_refused_role_is_not_a_failure(monkeypatch, router):
    # Roles are set independently; Windows accepting only one of them still
    # means the app got routed.
    fake, log = calls_returning(E_INVALIDARG, 0)
    monkeypatch.setattr(policy, "_vtable_fn", fake)
    router.route(1234, "{0.0.0.00000000}.{abc}")
    assert len(log) == 2


def test_every_role_refused_raises(monkeypatch, router):
    fake, _ = calls_returning(E_INVALIDARG, E_INVALIDARG)
    monkeypatch.setattr(policy, "_vtable_fn", fake)
    with pytest.raises(OSError) as e:
        router.route(1234, "{0.0.0.00000000}.{abc}")
    # E_INVALIDARG here is about the pid, not the device id — saying otherwise
    # sends whoever reads the log hunting the wrong bug.
    assert "no audio session" in str(e.value)


def test_clear_passes_a_null_device(monkeypatch, router):
    fake, log = calls_returning(0, 0)
    monkeypatch.setattr(policy, "_vtable_fn", fake)
    router.route(1234, None)
    # A c_void_p whose value is None marshals as the NULL HSTRING that means
    # "clear"; what matters is that no string was ever created for it.
    assert log and all(entry[4].value is None for entry in log)


def test_unavailable_router_is_a_no_op(monkeypatch):
    r = policy.AppRouter.__new__(policy.AppRouter)
    r._ptr, r._slot, r.available = None, 0, False
    monkeypatch.setattr(policy, "_vtable_fn", lambda *a: pytest.fail("must not call"))
    r.route(1234, "{0.0.0.00000000}.{abc}")   # degrades, never raises
