from __future__ import annotations

import pytest

from pipemix.windows.wasapi.sessions import _dedupe_sessions


@pytest.mark.parametrize("records, own_pid, expected", [
    # system sounds (pid 0) is skipped
    ([(0, "Speakers"), (111, "Speakers")], 999, [(111, "Speakers")]),
    # our own process is skipped
    ([(42, "Speakers"), (111, "Speakers")], 42, [(111, "Speakers")]),
    # the same app on two endpoints (e.g. a Bluetooth headset's two devices) appears once, at the first seen
    ([(111, "Speakers"), (111, "Headphones (Stereo)"), (222, "Headphones (Stereo)")], 999,
     [(111, "Speakers"), (222, "Headphones (Stereo)")]),
])
def test_dedupe_sessions(records, own_pid, expected):
    deduped = _dedupe_sessions([{"pid": p, "sink": s} for p, s in records], own_pid=own_pid)
    assert [(r["pid"], r["sink"]) for r in deduped] == expected
