from __future__ import annotations

from pipemix.windows.wasapi.sessions import _dedupe_sessions


def test_dedupe_skips_system_sounds_session():
    records = [{"pid": 0, "sink": "Speakers"}, {"pid": 111, "sink": "Speakers"}]
    assert [r["pid"] for r in _dedupe_sessions(records, own_pid=999)] == [111]


def test_dedupe_skips_our_own_process():
    records = [{"pid": 42, "sink": "Speakers"}, {"pid": 111, "sink": "Speakers"}]
    assert [r["pid"] for r in _dedupe_sessions(records, own_pid=42)] == [111]


def test_dedupe_first_endpoint_wins():
    # Same app playing on two endpoints (e.g. a Bluetooth headset's two
    # exposed devices) must appear once, keyed to wherever it was seen first.
    records = [
        {"pid": 111, "sink": "Speakers"},
        {"pid": 111, "sink": "Headphones (Stereo)"},
        {"pid": 222, "sink": "Headphones (Stereo)"},
    ]
    deduped = _dedupe_sessions(records, own_pid=999)
    assert [(r["pid"], r["sink"]) for r in deduped] == [
        (111, "Speakers"),
        (222, "Headphones (Stereo)"),
    ]
