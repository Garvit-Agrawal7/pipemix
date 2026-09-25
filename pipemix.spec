# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for PipeMix — one-dir build. Invoked by build_win.py, not by
hand: `pyinstaller pipemix.spec` alone will not find frontend/dist unless it
happens to already exist at the path below.

Entry point. Analysis points straight at src/pipemix/windows/main.py rather
than a separate launcher stub -- that file already ends in
`if __name__ == "__main__": main()`, so a stub would just be one more file
saying the same thing.

Two executables, one build. A windowed exe gets no console to write to, and a
console exe flashes a black window behind every GUI launch; PipeMix needs
both behaviours, so it ships both binaries over one shared COLLECT. They are
the same code and the same bundle -- only the Windows subsystem flag differs.
`pipemix.exe` is what the Start Menu points at; `pipemix-cli.exe` is what
--cli/--list/--share/--refresh want, and is the one to run from a terminal.

Data paths. frontend/dist and data/icons ship at the exact paths
windows/app.py looks for them: `_roots()` tries `parents[3]` (a source
checkout) then `parents[2]`. A module frozen into the PYZ archive gets a
synthetic __file__ of `<sys._MEIPASS>/pipemix/windows/app.py`, so
`parents[2]` is the bundle root -- `_internal/` next to the exe in onedir
mode. Naming the datas "frontend/dist" and "data/icons" lands them there, so
the installed app finds its UI and its icon without falling back to the
%PROGRAMDATA% path app.py also knows about.

No comtypes.client.GetModule anywhere in this tree: every COM interface is
hand-declared (pycaw, or wasapi/com.py for the four pycaw doesn't have), so
nothing needs the wrapper cache GetModule writes at runtime -- which is also
the classic way comtypes breaks once frozen. comtypes.gen is excluded so a
stray cache module can't sneak in through some transitive import.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

project_root = Path(SPECPATH)
src_dir = project_root / "src"
frontend_dist = project_root / "frontend" / "dist"
icon_dir = project_root / "data" / "icons"
icon_file = icon_dir / "pipemix.ico"

# comtypes and psutil are imported lazily inside functions all over
# src/pipemix/windows (by design -- see wasapi/devices.py), and pycaw's own
# api.* submodules are only reached the same way, so they get hand-added
# here rather than trusted to modulegraph's static scan.
hiddenimports = ["comtypes", "psutil"] + collect_submodules("pycaw")

a = Analysis(
    [str(src_dir / "pipemix" / "windows" / "main.py")],
    pathex=[str(src_dir)],
    binaries=[],
    datas=[
        (str(frontend_dist), "frontend/dist"),
        (str(icon_dir), "data/icons"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["comtypes.gen"],
    noarchive=False,
)

pyz = PYZ(a.pure)

_common = dict(
    exclude_binaries=True,
    debug=False,
    strip=False,
    upx=False,
    icon=str(icon_file),
)

# The Start Menu entry. No console, so launching it does not flash a terminal.
exe_gui = EXE(pyz, a.scripts, [], name="pipemix", console=False, **_common)

# The same program with a console attached, so --list and --cli have somewhere
# to print. Nothing else differs.
exe_cli = EXE(pyz, a.scripts, [], name="pipemix-cli", console=True, **_common)

coll = COLLECT(
    exe_gui,
    exe_cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="pipemix",
)
