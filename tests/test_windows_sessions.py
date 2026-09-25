"""Windows session PID dedupe and display-name fallback.

Pure logic — no COM, so this runs on Linux CI too. Everything else in
`sessions.py` needs a live `IAudioSessionManager2` and is left untested here,
per the brief.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipemix.windows.wasapi.sessions import _dedupe_sessions, _stream_name


def test_display_name_wins_when_present():
    assert _stream_name("Spotify", "Spotify.exe", 123) == "Spotify"


def test_falls_back_to_process_name():
    # The common case: almost nothing sets a session display name.
    assert _stream_name("", "chrome.exe", 456) == "chrome.exe"
    assert _stream_name(None, "chrome.exe", 456) == "chrome.exe"


def test_falls_back_to_pid_when_nothing_else_is_known():
    assert _stream_name("", None, 789) == "pid 789"


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
