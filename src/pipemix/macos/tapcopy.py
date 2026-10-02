"""Find (or build) the compiled IOProc in `_tapcopy.c`.

A frozen .app ships `_tapcopy.dylib` next to this module (build_mac.py
compiles it). A source checkout compiles it on first use into
~/Library/Caches/PipeMix with the Xcode command-line tools' `cc`. If neither
works, per-app routing reports itself unavailable and everything else
carries on.
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "_tapcopy.c"
BUILT = HERE / "_tapcopy.dylib"
CACHE = Path.home() / "Library" / "Caches" / "PipeMix"


class Meter(ctypes.Structure):
    _fields_ = [("peak", ctypes.c_float), ("cycles", ctypes.c_ulonglong)]


_lib: ctypes.CDLL | None = None
_error: str | None = None


def compile_to(out: Path) -> None:
    """Build the dylib — used by build_mac.py and by the source-checkout fallback."""
    cc = shutil.which("cc") or shutil.which("clang")
    if not cc:
        raise RuntimeError("no C compiler (install the Xcode command-line tools)")
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [cc, "-O2", "-dynamiclib", "-arch", "arm64", "-arch", "x86_64",
         "-framework", "CoreAudio", "-o", str(out), str(SOURCE)],
        check=True, capture_output=True, text=True,
    )


def _candidates() -> list[Path]:
    paths = [BUILT]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        paths.insert(0, Path(meipass) / "pipemix" / "macos" / "_tapcopy.dylib")
    return paths


def load() -> ctypes.CDLL:
    """The loaded helper, or raise RuntimeError saying why not."""
    global _lib, _error
    if _lib is not None:
        return _lib
    if _error is not None:
        raise RuntimeError(_error)

    path = next((p for p in _candidates() if p.is_file()), None)
    if path is None and SOURCE.is_file():
        digest = hashlib.sha1(SOURCE.read_bytes()).hexdigest()[:12]
        path = CACHE / f"_tapcopy-{digest}.dylib"
        if not path.is_file():
            try:
                compile_to(path)
                log.info("Compiled the per-app IOProc to %s", path)
            except Exception as e:
                detail = getattr(e, "stderr", "") or str(e)
                _error = f"Could not build the per-app audio helper: {detail.strip()}"
                raise RuntimeError(_error) from e
    if path is None:
        _error = "The per-app audio helper is missing from this build."
        raise RuntimeError(_error)

    lib = ctypes.CDLL(str(path))
    lib.pm_copy_proc.restype = ctypes.c_void_p
    lib.pm_copy_proc.argtypes = []
    _lib = lib
    return lib


def proc_pointer() -> int:
    """The C IOProc's address, for AudioDeviceCreateIOProcID."""
    return load().pm_copy_proc()
