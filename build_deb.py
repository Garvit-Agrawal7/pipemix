import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

MAINTAINER = "Garvit <134291696+Garvit-Agrawal7@users.noreply.github.com>"
UPLOADERS = "Gaurav <221670409+gaurav-066@users.noreply.github.com>"

def main():
    project_root = Path(__file__).parent.resolve()
    build_dir = project_root / "build"
    pkg_dir = build_dir / "pipemix-pkg"
    frontend_dist = project_root / "frontend" / "dist"

    # Single source of truth for the version; a mismatch here makes apt
    # refuse the next release as a downgrade
    with open(project_root / "pyproject.toml", "rb") as f:
        version = tomllib.load(f)["project"]["version"]

    # Bail out early so a missing build never produces a half-empty .deb
    if not frontend_dist.is_dir():
        print(f"[ERROR] Frontend build not found: {frontend_dist}")
        print("Build the UI first, then re-run this script:")
        print("  cd frontend && npm install && npm run build")
        sys.exit(1)

    if build_dir.exists():
        shutil.rmtree(build_dir)

    os.makedirs(pkg_dir / "DEBIAN", exist_ok=True)
    os.makedirs(pkg_dir / "usr" / "bin", exist_ok=True)
    os.makedirs(pkg_dir / "usr" / "share" / "pipemix" / "src", exist_ok=True)
    os.makedirs(pkg_dir / "usr" / "share" / "applications", exist_ok=True)
    os.makedirs(pkg_dir / "usr" / "share" / "icons" / "hicolor" / "512x512" / "apps", exist_ok=True)

    # This keeps the exact import hierarchy: import pipemix.app works out-of-the-box
    shutil.copytree(
        project_root / "src" / "pipemix",
        pkg_dir / "usr" / "share" / "pipemix" / "src" / "pipemix",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )

    # pywebview loads this at runtime
    shutil.copytree(frontend_dist, pkg_dir / "usr" / "share" / "pipemix" / "web")

    logo_src = project_root / "data" / "icons" / "pipemix.png"
    if logo_src.exists():
        shutil.copy(logo_src, pkg_dir / "usr" / "share" / "icons" / "hicolor" / "512x512" / "apps" / "pipemix.png")

    # pipewire-pulse, not pulseaudio-utils alone: plain PulseAudio satisfies
    # pactl but the app refuses to run on it. pipewire-bin (pw-dump) is already
    # pulled in through pipewire-pulse, but the app now runs it directly. The
    # gir1.2-* packages are pywebview's GTK backend, which python3-webview does
    # not pull in.
    control_content = f"""Package: pipemix
Version: {version}
Section: sound
Priority: optional
Architecture: all
Maintainer: {MAINTAINER}
Uploaders: {UPLOADERS}
Depends: python3 (>= 3.12), python3-gi, python3-webview, gir1.2-gtk-3.0,
 gir1.2-webkit2-4.1, pipewire-pulse, pipewire-bin, pulseaudio-utils
Description: Route audio to several outputs at once
 PipeMix plays the same audio through any number of PipeWire sinks --
 Bluetooth, USB, HDMI or built-in -- with a volume fader for each one,
 saved presets, and per-application routing.
"""
    with open(pkg_dir / "DEBIAN" / "control", "w", encoding="utf-8") as f:
        f.write(control_content)

    # PYTHONPATH so `import pipemix.linux.app` resolves from the installed tree
    launcher_content = """#!/bin/bash
export PYTHONPATH="/usr/share/pipemix/src:$PYTHONPATH"
exec python3 /usr/share/pipemix/src/pipemix/linux/main.py "$@"
"""
    launcher_path = pkg_dir / "usr" / "bin" / "pipemix"
    with open(launcher_path, "w", encoding="utf-8") as f:
        f.write(launcher_content)
    os.chmod(launcher_path, 0o755)

    desktop_content = """[Desktop Entry]
Name=PipeMix
Comment=Route audio to several outputs at once
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

    # Required by Debian policy, and the GPL requires the
    # licence text to travel with the binary
    doc_dir = pkg_dir / "usr" / "share" / "doc" / "pipemix"
    os.makedirs(doc_dir, exist_ok=True)
    header = (
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        "Upstream-Name: pipemix\n"
        "Source: https://github.com/gaurav-066/pipemix\n"
        "\n"
        "Files: *\n"
        f"Copyright: 2026 {MAINTAINER}\n"
        f"           2026 {UPLOADERS}\n"
        "License: GPL-3.0-or-later\n"
        "\n"
    )
    licence = (project_root / "LICENSE").read_text(encoding="utf-8")
    licence = "".join(" " + ln if ln.strip() else " .\n" for ln in licence.splitlines(keepends=True))
    (doc_dir / "copyright").write_text(header + licence, encoding="utf-8")

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
