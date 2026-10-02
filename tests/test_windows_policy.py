from __future__ import annotations

import ctypes

import pytest

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
    # E_INVALIDARG here is about the pid, not the device id; the log must say so.
    assert "no audio session" in str(e.value)


def test_clear_passes_a_null_device(monkeypatch, router):
    fake, log = calls_returning(0, 0)
    monkeypatch.setattr(policy, "_vtable_fn", fake)
    router.route(1234, None)
    # A c_void_p whose value is None marshals as the NULL HSTRING that means
    # "clear"; what matters is that no string was ever created for it.
    assert log and all(entry[4].value is None for entry in log)
