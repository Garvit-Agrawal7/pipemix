"""Entry-point dispatch: pick the platform implementation at call time.

`pyproject.toml` points its console script at `pipemix.main:main` on both
platforms; this module is the only thing that has to know which OS it is
running on. The actual implementations live in `pipemix.linux` and
`pipemix.windows`, and are imported lazily so that importing this module on
Linux never touches the Windows tree (which needs `comtypes`) and vice versa.
"""

from __future__ import annotations

import sys


def main() -> None:
    if sys.platform == "win32":
        from pipemix.windows.main import main as _main
    else:
        from pipemix.linux.main import main as _main
    _main()


if __name__ == "__main__":
    main()
