# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for PipeMix on macOS — a one-dir build wrapped in
PipeMix.app. Invoked by build_mac.py, which builds the .icns first.

One executable serves both uses: double-clicking the app opens the window,
and `PipeMix.app/Contents/MacOS/PipeMix --list` (or --cli, --share) runs in
a terminal. macOS has no console/windowed subsystem split to work around,
unlike pipemix.spec's two Windows exes.

Data paths. frontend/dist ships under Contents/Resources, which is
`sys._MEIPASS` in a BUNDLE build — the first place macos/app.py's `_roots()`
looks.

Core Audio is reached through ctypes, plus pyobjc (which pywebview's Cocoa
backend brings anyway) for CATapDescription and the volume-key monitor.
build/_tapcopy.dylib is the per-app IOProc, compiled by build_mac.py; it
lands next to tapcopy.py, where that module looks for it.

NSAudioCaptureUsageDescription is what lets macOS ask for the System Audio
Recording permission per-app routing needs; without it, taps stay silent
and no prompt ever appears.
"""

from pathlib import Path

project_root = Path(SPECPATH)
src_dir = project_root / "src"
frontend_dist = project_root / "frontend" / "dist"
icon_file = project_root / "build" / "pipemix.icns"
tapcopy = project_root / "build" / "_tapcopy.dylib"

import tomllib
with open(project_root / "pyproject.toml", "rb") as f:
    version = tomllib.load(f)["project"]["version"]

a = Analysis(
    [str(src_dir / "pipemix" / "macos" / "main.py")],
    pathex=[str(src_dir)],
    binaries=[(str(tapcopy), "pipemix/macos")],
    datas=[(str(frontend_dist), "frontend/dist")],
    hiddenimports=["webview.platforms.cocoa", "PyObjCTools.AppHelper"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The Windows and Linux trees are never imported on macOS; keep their
    # heavy dependencies from being chased.
    excludes=["comtypes", "pycaw", "gi", "tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PipeMix",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="PipeMix")

app = BUNDLE(
    coll,
    name="PipeMix.app",
    icon=str(icon_file),
    bundle_identifier="com.pipemix.app",
    version=version,
    info_plist={
        "CFBundleName": "PipeMix",
        "CFBundleDisplayName": "PipeMix",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.music",
        "NSAudioCaptureUsageDescription":
            "PipeMix captures an app's audio only when you route that app to "
            "specific outputs, so it can play there instead.",
    },
)
