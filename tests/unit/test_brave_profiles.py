"""
PipeMix — Brave Profile Discovery Diagnostic Script

This script helps diagnose process tree walking and profile mapping
for Brave browser.

Run this from the project root:
    python3 tests/unit/test_brave_profiles.py

What it does:
  1. Locates Brave's Local State configuration file.
  2. Parses and prints all configured profiles inside Brave (Gauravv, sam, etc.)
     along with their folder directory names (Default, Profile 1, etc.).
  3. Lists all active audio streams (sink-inputs) currently playing.
  4. Traces the process tree for each audio stream to see if it belongs to Brave,
     and attempts to map it to a friendly profile name.

Make sure Brave is playing some audio (YouTube/Spotify) when you run this!
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def get_brave_local_state_profiles() -> dict[str, str]:
    """Reads Brave's Local State configuration and returns {dir_name: profile_name}."""
    possible_paths = [
        os.path.expanduser("~/.config/BraveSoftware/Brave-Browser/Local State"),
        os.path.expanduser("~/.config/brave-browser/Local State"),
    ]
    
    profiles = {}
    for path in possible_paths:
        if os.path.exists(path):
            print(f"Found Brave Local State config at: {path}")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                profiles_cache = data.get("profile", {}).get("info_cache", {})
                for folder, info in profiles_cache.items():
                    name = info.get("name")
                    if name:
                        profiles[folder] = name
            except Exception as e:
                print(f"Error parsing Local State: {e}")
    return profiles


def trace_process_profile(pid: str, profile_cache: dict[str, str]) -> tuple[str | None, list[str]]:
    """Walks up the process tree starting at pid, looking for --profile-directory."""
    curr_pid = pid
    trace_lines = []
    
    for level in range(5):
        cmd_path = f"/proc/{curr_pid}/cmdline"
        if not os.path.exists(cmd_path):
            trace_lines.append(f"  Level {level}: PID {curr_pid} does not exist.")
            break
            
        try:
            with open(cmd_path, "rb") as f:
                content = f.read()
            args = [a.decode("utf-8", errors="ignore") for a in content.split(b"\x00") if a]
            cmdline = " ".join(args)
            
            trace_lines.append(f"  Level {level}: PID {curr_pid} -> {cmdline[:90]}...")
            
            profile_dir = None
            is_main_browser = True
            for arg in args:
                if arg.startswith("--type="):
                    is_main_browser = False
                if arg.startswith("--profile-directory="):
                    profile_dir = arg.split("=", 1)[1]
                    break
            
            if profile_dir:
                friendly = profile_cache.get(profile_dir, profile_dir)
                return friendly, trace_lines
                
            if is_main_browser:
                friendly = profile_cache.get("Default", "Default")
                return friendly, trace_lines
                
        except Exception as e:
            trace_lines.append(f"  Level {level}: Error reading PID {curr_pid}: {e}")
            break
            
        # Get PPID to walk up
        status_path = f"/proc/{curr_pid}/status"
        ppid = None
        if os.path.exists(status_path):
            try:
                with open(status_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.startswith("PPid:"):
                            ppid = line.split(":", 1)[1].strip()
                            break
            except Exception:
                pass
        if not ppid or ppid == curr_pid or ppid == "0":
            break
        curr_pid = ppid
        
    return None, trace_lines


def get_active_sink_inputs() -> list[dict]:
    """Queries pactl for active playing audio inputs."""
    try:
        res = subprocess.run(["pactl", "list", "sink-inputs"], capture_output=True, text=True, timeout=3)
        if res.returncode != 0:
            return []
    except Exception:
        return []
        
    streams = []
    current = {}
    for raw_line in res.stdout.splitlines():
        line = raw_line.strip()
        if raw_line.startswith("Sink Input #"):
            if current:
                streams.append(current)
            current = {"id": raw_line.split("#")[1].strip()}
        elif line.startswith("application.process.id ="):
            current["pid"] = line.split("=")[1].replace('"', '').strip()
        elif line.startswith("application.name ="):
            current["app"] = line.split("=")[1].replace('"', '').strip()
        elif line.startswith("media.name ="):
            current["media"] = line.split("=")[1].replace('"', '').strip()
    if current:
        streams.append(current)
    return streams


def main() -> None:
    print("\nPipeMix — Brave Profile Diagnostics")
    print("=" * 60)
    
    # 1. Fetch Brave Config profiles
    print("\nStep 1: Reading Brave Configuration Profiles...")
    profile_cache = get_brave_local_state_profiles()
    if profile_cache:
        for folder, name in profile_cache.items():
            print(f"  - Folder '{folder}'  =>  Profile Name: '{name}'")
    else:
        print("  No profiles cache discovered in Local State (or Brave config not found).")
        
    # 2. Get active audio streams
    print("\nStep 2: Checking Active Playing Audio Streams...")
    streams = get_active_sink_inputs()
    # Filter out PipeMix loopbacks
    active_streams = [s for s in streams if s.get("app") != "pipemix" and "pipemix_" not in s.get("media", "")]
    
    if not active_streams:
        print("  No active audio streams playing. Please play a video in Brave before running!")
        return
        
    for s in active_streams:
        print(f"\nStream Input #{s['id']}:")
        print(f"  App Name:   {s.get('app')}")
        print(f"  Media Name: {s.get('media')}")
        
        pid = s.get("pid")
        if not pid:
            print("  No process ID (PID) registered for this stream.")
            continue
            
        print(f"  Process ID: {pid}")
        print("  Tracing Process Tree:")
        resolved_name, trace = trace_process_profile(pid, profile_cache)
        
        for line in trace:
            print(f"  {line}")
            
        if resolved_name:
            print(f"  [SUCCESS] Resolved to Brave Profile: '{resolved_name}'")
        else:
            print("  [FAILED] Could not find Brave profile for this process.")


if __name__ == "__main__":
    main()
