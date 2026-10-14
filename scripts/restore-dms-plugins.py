#!/usr/bin/env python3
"""Restore the original RunneOS six-plugin installation backup (2026-10-10)."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("backup", type=Path)
args = parser.parse_args()
backup = args.backup.expanduser().resolve()
manifest = json.loads((backup/"manifest.json").read_text())
allowed = {".config/DankMaterialShell/settings.json", ".config/DankMaterialShell/plugin_settings.json", ".config/DankMaterialShell/plugins.lock.json", ".config/niri/niri-bindings.kdl"}
if set(manifest) != allowed or not all(isinstance(v, bool) for v in manifest.values()):
    raise SystemExit("Use the original pre-installation backup, not an overlay backup")
for relative, existed in manifest.items():
    if existed and not (backup/relative).is_file():
        raise SystemExit("Backup incomplete: "+relative)
for plugin in ("calculator", "dankAIUsage", "keyboardLayoutSearch", "niriWindows", "screenRecorder", "screenshotPlus"):
    subprocess.run(["dms", "ipc", "call", "plugins", "disable", plugin], check=True)
    subprocess.run(["dms", "plugins", "uninstall", plugin], check=True)
for relative, existed in manifest.items():
    target = Path.home()/relative
    if existed:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup/relative, target)
    else:
        target.unlink(missing_ok=True)
subprocess.run(["niri", "validate"], check=True)
print("Restored pre-installation settings and bindings. Packages retained.")
