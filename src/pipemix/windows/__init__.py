"""PipeMix — Windows implementation.

Mirrors the Linux package one level down: `windows/controller.py`,
`windows/app.py`, `windows/main.py` and `windows/wasapi/` replace
`linux/controller.py`, `linux/app.py`, `linux/main.py` and the PulseAudio/
BlueZ backends respectively. `models.py`, `ui/api.py` and `ui/bridge.py` are
shared as-is from the Linux tree.

This module deliberately does not import any of its submodules at import
time: `wasapi/com.py` needs `comtypes`, which only exists on Windows, and the
rest of the codebase (including every Linux import) must be able to import
`pipemix` without pulling that in.
"""

from __future__ import annotations
