import os
import shutil
import subprocess
from pathlib import Path

def main():
    project_root = Path(__file__).parent.resolve()
    build_dir = project_root / "build"
    pkg_dir = build_dir / "pipemix-pkg"

    # 1. Clean previous build folders
    if build_dir.exists():
        shutil.rmtree(build_dir)

    # 2. Create directory structures
    os.makedirs(pkg_dir / "DEBIAN", exist_ok=True)
    os.makedirs(pkg_dir / "usr" / "bin", exist_ok=True)
    os.makedirs(pkg_dir / "usr" / "share" / "pipemix" / "src", exist_ok=True)
    os.makedirs(pkg_dir / "usr" / "share" / "applications", exist_ok=True)
    os.makedirs(pkg_dir / "usr" / "share" / "icons" / "hicolor" / "512x512" / "apps", exist_ok=True)

    # 3. Copy source files recursively
    # This keeps the exact import hierarchy: import pipemix.app works out-of-the-box
    shutil.copytree(
        project_root / "src" / "pipemix",
        pkg_dir / "usr" / "share" / "pipemix" / "src" / "pipemix",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )

    # 4. Copy logo icon
    logo_src = project_root / "data" / "icons" / "pipemix.png"
    if logo_src.exists():
        shutil.copy(logo_src, pkg_dir / "usr" / "share" / "icons" / "hicolor" / "512x512" / "apps" / "pipemix.png")

    # 5. Create DEBIAN/control file
    control_content = """Package: pipemix
Version: 1.0.0
Architecture: all
Maintainer: Gaurav <gaurav@mint-pc>
Depends: python3, python3-gi, gir1.2-gtk-4.0, gir1.2-glib-2.0, pulseaudio-utils, python3-pyqt6
Description: Dual Bluetooth Audio Manager
 Route application streams and combine outputs using PipeWire.
"""
    with open(pkg_dir / "DEBIAN" / "control", "w", encoding="utf-8") as f:
        f.write(control_content)

    # 6. Create usr/bin/pipemix launcher wrapper
    # We add the /usr/share/pipemix/src directory to PYTHONPATH so imports resolve correctly
    launcher_content = """#!/bin/bash
export PYTHONPATH="/usr/share/pipemix/src:$PYTHONPATH"
exec python3 /usr/share/pipemix/src/pipemix/main.py "$@"
"""
    launcher_path = pkg_dir / "usr" / "bin" / "pipemix"
    with open(launcher_path, "w", encoding="utf-8") as f:
        f.write(launcher_content)
    os.chmod(launcher_path, 0o755) # Make executable

    # 7. Create desktop entry file
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

    # 8. Build Debian package
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

if __name__ == "__main__":
    main()
