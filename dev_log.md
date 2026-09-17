# Development Log — PipeMix (`feat/pywebview-migration`)

## Session Summary
- **Target OS:** Linux
- **Branch:** `feat/pywebview-migration`
- **Date:** 2026-09-17

### What Was Built / Set Up
1. Fetched remote branches and checked out local branch `feat/pywebview-migration`.
2. Built frontend assets using Vite/TypeScript (`npm install && npm run build`).
3. Installed missing system runtime dependency: `python3-webview`.
4. Recompiled the Debian installer package: `build/pipemix.deb`.

### What Was Tested & Passed
- **Root Cause & Fix for Built-in Speaker Audio:**
  - **Issue:** In PipeWire, loading `module-combine-sink` with legacy `slaves=` parameter only attached Bluetooth sinks and failed to bind to ALSA hardware speakers (`alsa_output.pci-0000_04_00.6.analog-stereo`).
  - **Fix:** Updated `pactl_backend.py` to pass both `slaves=` and `sinks=` parameters to `module-combine-sink`.
  - **Verification:** Verified via `pw-link -l` that `module-combine-sink` now creates audio output links to BOTH the Bluetooth headset and built-in ALSA speaker simultaneously.

### Next Steps
- Re-run `pipemix` or `PYTHONPATH=src python3 -m pipemix.main` to verify simultaneous audio playback on both built-in speakers and Bluetooth earbuds.
