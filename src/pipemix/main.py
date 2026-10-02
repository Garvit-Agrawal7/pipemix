"""Entry-point dispatch: picks the platform implementation at call time.

The actual implementations live in `pipemix.linux`, `pipemix.windows` and
`pipemix.macos`, imported lazily so that only the running platform's is loaded.
"""

from __future__ import annotations

import sys


def main() -> None:
    if sys.platform == "win32":
        from pipemix.windows.main import main as _main
    elif sys.platform == "darwin":
        from pipemix.macos.main import main as _main
    else:
        from pipemix.linux.main import main as _main
    _main()


if __name__ == "__main__":
    main()
