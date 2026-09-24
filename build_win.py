import importlib.util
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def find_iscc() -> Path | None:
    """ISCC.exe: an explicit override first, then PATH, then the default install location.

    Inno Setup does not put ISCC.exe on PATH by default, and there is no
    single correct install path across machines -- ISCC_PATH lets a user
    point at whatever they actually have without us hardcoding one.
    """
    override = os.environ.get("ISCC_PATH")
    if override:
        p = Path(override)
        return p if p.is_file() else None

    which = shutil.which("ISCC.exe") or shutil.which("ISCC")
    if which:
        return Path(which)

    for env_var in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env_var)
        if base:
            candidate = Path(base) / "Inno Setup 6" / "ISCC.exe"
            if candidate.is_file():
                return candidate
    return None


def main():
    project_root = Path(__file__).parent.resolve()
    build_dir = project_root / "build"
    frontend_dist = project_root / "frontend" / "dist"
    spec_path = project_root / "pipemix.spec"
    installer_path = project_root / "installer.iss"

    # Single source of truth for the version; mirrors build_deb.py so the
    # .exe and the .deb can never drift apart on a release.
    with open(project_root / "pyproject.toml", "rb") as f:
        version = tomllib.load(f)["project"]["version"]

    # Bail out early so a missing build never produces a half-empty installer
    if not frontend_dist.is_dir():
        print(f"[ERROR] Frontend build not found: {frontend_dist}")
        print("Build the UI first, then re-run this script:")
        print("  cd frontend && npm install && npm run build")
        sys.exit(1)

    # Both the exes and the installer take their icon from here, and Inno
    # refuses to compile a SetupIconFile that is not there.
    icon_file = project_root / "data" / "icons" / "pipemix.ico"
    if not icon_file.is_file():
        print(f"[ERROR] Application icon not found: {icon_file}")
        sys.exit(1)

    if importlib.util.find_spec("PyInstaller") is None:
        print("[ERROR] PyInstaller is not installed.")
        print("Install it, then re-run this script:")
        print("  pip install pyinstaller")
        sys.exit(1)

    iscc = find_iscc()
    if iscc is None:
        print("[ERROR] Inno Setup's ISCC.exe was not found.")
        print("Install Inno Setup 6 from https://jrsoftware.org/isdl.php --")
        print("ISCC.exe is not added to PATH by default. Either add it to PATH,")
        print("or point this script at it directly:")
        print(r'  set ISCC_PATH=C:\Program Files (x86)\Inno Setup 6\ISCC.exe')
        sys.exit(1)

    if build_dir.exists():
        shutil.rmtree(build_dir)

    dist_dir = build_dir / "dist"
    work_dir = build_dir / "work"

    print("Freezing with PyInstaller...")
    from PyInstaller.__main__ import run as pyinstaller_run

    try:
        pyinstaller_run([
            str(spec_path),
            "--distpath", str(dist_dir),
            "--workpath", str(work_dir),
            "--noconfirm",
        ])
    except SystemExit as e:
        if e.code not in (0, None):
            print(f"\n[ERROR] PyInstaller failed (exit code {e.code}).")
            sys.exit(1)

    exe_dir = dist_dir / "pipemix"
    if not (exe_dir / "pipemix.exe").exists():
        print(f"[ERROR] PyInstaller did not produce {exe_dir / 'pipemix.exe'}")
        sys.exit(1)

    print("Building installer with Inno Setup...")
    result = subprocess.run(
        [
            str(iscc),
            f"/DMyAppVersion={version}",
            f"/DPipemixDistDir={exe_dir}",
            f"/DPipemixIconFile={icon_file}",
            f"/O{build_dir}",
            str(installer_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"\n[SUCCESS] Created installer: {build_dir / 'pipemix-setup.exe'}")
    else:
        print(f"\n[ERROR] Failed to build installer: {result.stdout}\n{result.stderr}")
        sys.exit(1)


if __name__ == "__main__":
    main()
