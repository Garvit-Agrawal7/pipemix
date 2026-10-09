# PyInstaller one-dir spec, run by build_win.py: a windowed pipemix.exe and a console pipemix-cli.exe over one COLLECT.
from pathlib import Path

project_root = Path(SPECPATH)
icon_dir = project_root / "data" / "icons"

a = Analysis(
    [str(project_root / "src" / "pipemix" / "windows" / "main.py")],
    pathex=[str(project_root / "src")],
    datas=[
        (str(project_root / "frontend" / "dist"), "frontend/dist"),
        (str(icon_dir), "data/icons"),
    ],
    hiddenimports=["pystray._win32"],  # pystray imports its backend by name
    excludes=["comtypes.gen"],
)

pyz = PYZ(a.pure)

_common = dict(exclude_binaries=True, icon=str(icon_dir / "pipemix.ico"))
exe_gui = EXE(pyz, a.scripts, [], name="pipemix", console=False, **_common)
exe_cli = EXE(pyz, a.scripts, [], name="pipemix-cli", console=True, **_common)

coll = COLLECT(exe_gui, exe_cli, a.binaries, a.datas, name="pipemix")
