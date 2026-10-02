"""Build PipeMix.app and PipeMix.dmg on macOS.

    npm --prefix frontend install && npm --prefix frontend run build
    pip install -e . pyinstaller
    python3 build_mac.py

Leaves build/dist/PipeMix.app and build/PipeMix-<version>.dmg. The app is
ad-hoc signed only, so on another Mac the first launch needs right-click →
Open (or `xattr -dr com.apple.quarantine /Applications/PipeMix.app`).
"""

import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def run(*cmd: str) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def make_icns(png: Path, out: Path) -> None:
    """An .icns from the one 512px PNG we have, via sips and iconutil."""
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "pipemix.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = size * scale
                if px > 512:
                    px = 512  # no 1024 source; macOS scales the 512 up for @2x
                suffix = "" if scale == 1 else "@2x"
                run("sips", "-z", str(px), str(px), str(png),
                    "--out", str(iconset / f"icon_{size}x{size}{suffix}.png"))
        run("iconutil", "-c", "icns", str(iconset), "-o", str(out))


def make_dmg(app: Path, out: Path) -> None:
    """A compressed disk image with the app and an /Applications shortcut."""
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "PipeMix"
        stage.mkdir()
        run("ditto", str(app), str(stage / app.name))
        (stage / "Applications").symlink_to("/Applications")
        out.unlink(missing_ok=True)
        run("hdiutil", "create", "-volname", "PipeMix", "-srcfolder", str(stage),
            "-ov", "-format", "UDZO", str(out))


def main() -> None:
    if sys.platform != "darwin":
        sys.exit("[ERROR] build_mac.py only runs on macOS.")

    root = Path(__file__).parent.resolve()
    build = root / "build"
    dist = build / "dist"

    with open(root / "pyproject.toml", "rb") as f:
        version = tomllib.load(f)["project"]["version"]

    if not (root / "frontend" / "dist" / "index.html").is_file():
        print("[ERROR] Frontend build not found. Build it first:")
        print("  npm --prefix frontend install && npm --prefix frontend run build")
        sys.exit(1)

    if shutil.which("pyinstaller") is None and not (Path(sys.executable).parent / "pyinstaller").exists():
        sys.exit("[ERROR] PyInstaller not found: pip install pyinstaller")

    build.mkdir(exist_ok=True)
    make_icns(root / "data" / "icons" / "pipemix.png", build / "pipemix.icns")

    # The per-app routing IOProc (real-time, so C), universal like the rest.
    sys.path.insert(0, str(root / "src"))
    from pipemix.macos.tapcopy import compile_to
    compile_to(build / "_tapcopy.dylib")
    print("+ compiled build/_tapcopy.dylib")

    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", str(dist), "--workpath", str(build / "pyinstaller"),
        str(root / "pipemix-macos.spec"))

    app = dist / "PipeMix.app"
    # PyInstaller signs ad hoc already; re-sign the whole bundle so the
    # resources added after its pass are covered too.
    run("codesign", "--force", "--deep", "--sign", "-", str(app))

    dmg = build / f"PipeMix-{version}.dmg"
    make_dmg(app, dmg)
    print(f"\nBuilt {app}\n      {dmg}")


if __name__ == "__main__":
    main()
