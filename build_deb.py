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
        sys.exit(f"[ERROR] Frontend build not found: {frontend_dist}\n"
                 "Build the UI first: cd frontend && npm install && npm run build")

    shutil.rmtree(build_dir, ignore_errors=True)

    def put(rel, text, mode=None):
        path = pkg_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if mode:
            path.chmod(mode)

    # This keeps the exact import hierarchy: import pipemix.app works out-of-the-box
    shutil.copytree(
        project_root / "src" / "pipemix",
        pkg_dir / "usr" / "share" / "pipemix" / "src" / "pipemix",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )

    # pywebview loads this at runtime
    shutil.copytree(frontend_dist, pkg_dir / "usr" / "share" / "pipemix" / "web")

    icon_dir = pkg_dir / "usr" / "share" / "icons" / "hicolor" / "512x512" / "apps"
    icon_dir.mkdir(parents=True)
    shutil.copy(project_root / "data" / "icons" / "pipemix.png", icon_dir / "pipemix.png")

    # pipewire-pulse, not pulseaudio-utils: the app refuses plain PulseAudio.
    # pipewire-bin (pw-dump, run directly) comes with pipewire-pulse. gir1.2-* is
    # pywebview's GTK backend, which python3-webview doesn't pull in.
    put("DEBIAN/control", f"""Package: pipemix
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
""")

    # PYTHONPATH so `import pipemix.linux.app` resolves from the installed tree
    put("usr/bin/pipemix", """#!/bin/bash
export PYTHONPATH="/usr/share/pipemix/src:$PYTHONPATH"
exec python3 /usr/share/pipemix/src/pipemix/linux/main.py "$@"
""", 0o755)

    put("usr/share/applications/pipemix.desktop", """[Desktop Entry]
Name=PipeMix
Comment=Route audio to several outputs at once
Exec=/usr/bin/pipemix
Icon=pipemix
Terminal=false
Type=Application
Categories=AudioVideo;Audio;Utility;
Keywords=Audio;Bluetooth;PipeWire;Mixer;
StartupNotify=true
""")

    # Debian policy and the GPL require the licence to ship with the binary
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
    put("usr/share/doc/pipemix/copyright", header + licence)

    print("Building Debian package...")
    subprocess.run(["dpkg-deb", "--build", str(pkg_dir), str(build_dir / "pipemix.deb")], check=True)
    print(f"\n[SUCCESS] Created Debian package: {build_dir / 'pipemix.deb'}")
    print("To install it, run:")
    print("  sudo dpkg -i build/pipemix.deb")

if __name__ == "__main__":
    main()
