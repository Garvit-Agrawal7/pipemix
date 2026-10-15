import sys

# Each platform's tests import its own backend (gi / comtypes), so run only the host's.
collect_ignore = ["linux"] if sys.platform == "win32" else ["windows"]
