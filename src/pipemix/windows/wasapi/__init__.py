"""Raw WASAPI plumbing: device enumeration, the fan-out engine, and per-app
routing, all through hand-declared `comtypes` interfaces (`com.py`) rather
than `comtypes.client.GetModule`, which would break under PyInstaller.

Not imported eagerly by `pipemix.windows` — every module in this package
needs `comtypes`, which is only importable on Windows.
"""

from __future__ import annotations
