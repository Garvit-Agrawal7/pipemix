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
