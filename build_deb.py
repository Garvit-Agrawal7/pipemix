import os
import shutil
import subprocess
import sys
from pathlib import Path

def main():
    project_root = Path(__file__).parent.resolve()
    build_dir = project_root / "build"
    pkg_dir = build_dir / "pipemix-pkg"
    frontend_dist = project_root / "frontend" / "dist"

    # 1. Require a built frontend before touching anything
    # Bail out early so a missing build never produces a half-empty .deb
    if not frontend_dist.is_dir():
        print(f"[ERROR] Frontend build not found: {frontend_dist}")
        print("Build the UI first, then re-run this script:")
        print("  cd frontend && npm install && npm run build")
        sys.exit(1)

    # 2. Clean previous build folders
    if build_dir.exists():
        shutil.rmtree(build_dir)

    # 3. Create directory structures
    os.makedirs(pkg_dir / "DEBIAN", exist_ok=True)
    os.makedirs(pkg_dir / "usr" / "bin", exist_ok=True)
    os.makedirs(pkg_dir / "usr" / "share" / "pipemix" / "src", exist_ok=True)
    os.makedirs(pkg_dir / "usr" / "share" / "applications", exist_ok=True)
    os.makedirs(pkg_dir / "usr" / "share" / "icons" / "hicolor" / "512x512" / "apps", exist_ok=True)

    # 4. Copy source files recursively
    # This keeps the exact import hierarchy: import pipemix.app works out-of-the-box
    shutil.copytree(
        project_root / "src" / "pipemix",
        pkg_dir / "usr" / "share" / "pipemix" / "src" / "pipemix",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )

    # 5. Copy the built frontend, this is what pywebview loads at runtime
    shutil.copytree(frontend_dist, pkg_dir / "usr" / "share" / "pipemix" / "web")

    # 6. Copy logo icon
    logo_src = project_root / "data" / "icons" / "pipemix.png"
    if logo_src.exists():
        shutil.copy(logo_src, pkg_dir / "usr" / "share" / "icons" / "hicolor" / "512x512" / "apps" / "pipemix.png")

    # 7. Create DEBIAN/control file
    control_content = """Package: pipemix
Version: 1.0.0
Architecture: all
Maintainer: Gaurav <gaurav@mint-pc>
Depends: python3, python3-gi, python3-webview, pulseaudio-utils
Description: Dual Bluetooth Audio Manager
 Route application streams and combine outputs using PipeWire.
"""
    with open(pkg_dir / "DEBIAN" / "control", "w", encoding="utf-8") as f:
        f.write(control_content)

    # 8. Create usr/bin/pipemix launcher wrapper
    # We add the /usr/share/pipemix/src directory to PYTHONPATH so imports resolve correctly
    launcher_content = """#!/bin/bash
export PYTHONPATH="/usr/share/pipemix/src:$PYTHONPATH"
exec python3 /usr/share/pipemix/src/pipemix/main.py "$@"
"""
    launcher_path = pkg_dir / "usr" / "bin" / "pipemix"
    with open(launcher_path, "w", encoding="utf-8") as f:
        f.write(launcher_content)
    os.chmod(launcher_path, 0o755) # Make executable

    # 9. Create desktop entry file
    desktop_content = """[Desktop Entry]
Name=PipeMix
Comment=Dual Bluetooth Audio Manager
Exec=/usr/bin/pipemix
Icon=pipemix
Terminal=false
Type=Application
Categories=AudioVideo;Audio;Utility;
Keywords=Audio;Bluetooth;PipeWire;Mixer;
StartupNotify=true
"""
    with open(pkg_dir / "usr" / "share" / "applications" / "pipemix.desktop", "w", encoding="utf-8") as f:
        f.write(desktop_content)

    # 10. Build Debian package
    print("Building Debian package...")
    result = subprocess.run(
        ["dpkg-deb", "--build", str(pkg_dir), str(build_dir / "pipemix.deb")],
        capture_output=True,
        text=True
    )
    if result.returncode == 0:
        print(f"\n[SUCCESS] Created Debian package: {build_dir / 'pipemix.deb'}")
        print("To install it, run:")
        print("  sudo dpkg -i build/pipemix.deb")
    else:
        print(f"\n[ERROR] Failed to build package: {result.stderr}")
        sys.exit(1)

if __name__ == "__main__":
    main()
