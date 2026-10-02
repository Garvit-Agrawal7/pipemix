import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path


def main():
    project_root = Path(__file__).parent.resolve()
    build_dir = project_root / "build"
    frontend_dist = project_root / "frontend" / "dist"
    vb_cable_zip = project_root / "VBCABLE_Driver_Pack45.zip"

    # Single source of truth for the version, shared with build_deb.py.
    with open(project_root / "pyproject.toml", "rb") as f:
        version = tomllib.load(f)["project"]["version"]

    # Bail out early so a missing build never produces a half-empty installer
    if not frontend_dist.is_dir():
        sys.exit(f"[ERROR] Frontend build not found: {frontend_dist}\n"
                 "Build the UI first: cd frontend && npm install && npm run build")

    # ISCC isn't on PATH by default; ISCC_PATH overrides.
    iscc = (os.environ.get("ISCC_PATH") or shutil.which("ISCC")
            or rf"{os.environ['ProgramFiles(x86)']}\Inno Setup 6\ISCC.exe")

    shutil.rmtree(build_dir, ignore_errors=True)

    vb_cable_dir = build_dir / "vbcable"
    # Inno Setup can't read a zip, so it gets the extracted tree. The check
    # matters because a missing setup exe still compiles into a broken installer.
    with zipfile.ZipFile(vb_cable_zip) as package:
        if "VBCABLE_Setup_x64.exe" not in package.namelist():
            sys.exit("[ERROR] VB-CABLE archive does not contain VBCABLE_Setup_x64.exe")
        package.extractall(vb_cable_dir)

    dist_dir = build_dir / "dist"

    print("Freezing with PyInstaller...")
    from PyInstaller.__main__ import run as pyinstaller_run

    pyinstaller_run([
        str(project_root / "pipemix.spec"),
        "--distpath", str(dist_dir),
        "--workpath", str(build_dir / "work"),
        "--noconfirm",
    ])

    print("Building installer with Inno Setup...")
    subprocess.run(
        [
            iscc,
            f"/DMyAppVersion={version}",
            f"/DPipemixDistDir={dist_dir / 'pipemix'}",
            f"/DPipemixIconFile={project_root / 'data' / 'icons' / 'pipemix.ico'}",
            f"/DVBcableDir={vb_cable_dir}",
            f"/O{build_dir}",
            str(project_root / "installer.iss"),
        ],
        check=True,
    )
    print(f"\n[SUCCESS] Created installer: {build_dir / 'pipemix-setup.exe'}")


if __name__ == "__main__":
    main()
