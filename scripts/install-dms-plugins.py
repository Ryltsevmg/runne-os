#!/usr/bin/env python3
"""Install the selected, pinned DMS plugins and overlay the RunneOS AI widget."""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    output = result.stdout.strip()
    if any(marker in output for marker in ("ERROR:", "FAILED", "NOT_FOUND")):
        raise RuntimeError(output)
    return output


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".runneos.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n")
    temp.chmod(0o600)
    os.replace(temp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-download", action="store_true", help="Only apply to plugins already at the locked commits")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    assets = repo/"packages/runneos-config/files"
    if not (assets/"dms-plugin-settings.json").is_file():
        assets = Path("/usr/share/runneos/dms")
    lock = assets/"dms-plugins.lock.json"
    defaults = json.loads((assets/"dms-plugin-settings.json").read_text())
    selected = json.loads(lock.read_text())["plugins"]
    home = Path.home()
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(home/".config")))/"DankMaterialShell"
    state = Path(os.environ.get("XDG_STATE_HOME", str(home/".local/state")))/"runneos/backups"
    backup = state/("dms-plugins-"+datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    backup.mkdir(parents=True, mode=0o700)
    existed = {}
    for name in ("settings.json", "plugin_settings.json", "plugins.lock.json"):
        path = config/name
        existed[name] = path.exists()
        if path.exists():
            shutil.copy2(path, backup/name)
    write_json(backup/"manifest.json", {"config": str(config), "existed": existed, "plugins": list(selected)})
    print("Backup:", backup)
    if not args.skip_download:
        # Do not prune plugins outside the selected set.
        print(run("dms", "plugins", "restore", str(lock)))
    for plugin, metadata in selected.items():
        path = config/"plugins"/plugin
        commit = run("git", "-C", str(path), "rev-parse", "HEAD")
        if commit != metadata["commit"]:
            raise RuntimeError("Plugin commit differs from lock: "+plugin)
    overlay = Path("/usr/share/runneos/ai-usage")
    ai = config/"plugins/dankAIUsage"
    for name in ("RunneAIUsageWidget.qml", "RunneAIUsageSettings.qml"):
        shutil.copy2(overlay/name, ai/name)
    manifest_path = ai/"plugin.json"
    manifest = json.loads(manifest_path.read_text())
    shutil.copy2(manifest_path, backup/"dankAIUsage-plugin.json")
    manifest.update(name="AI Usage · RunneOS", description="Usage and available account metrics for nine AI providers",
                     version="0.2.0-runneos", component="./RunneAIUsageWidget.qml", settings="./RunneAIUsageSettings.qml",
                     requires=["runneos-ai-collect", "runneos-ai"], requires_dms=">=1.6.3")
    manifest["settings_schema"] = {}
    write_json(manifest_path, manifest)
    # Русификация Screenshot+: DMS читает translations/<локаль>.json из каталога
    # плагина (PluginService._loadPluginTranslations), поэтому достаточно добавить
    # русский каталог поверх закреплённого upstream; код и ключи не меняются.
    sp_translations = assets/"dms-overlays/screenshotPlus/translations"
    sp_translations_dir = config/"plugins/screenshotPlus/translations"
    for name in ("ru_RU.json", "ru.json"):
        if (sp_translations/name).is_file():
            sp_translations_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sp_translations/name, sp_translations_dir/name)
    settings_path = config/"plugin_settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    for plugin, values in defaults.items():
        settings[plugin] = dict(values, **settings.get(plugin, {}))
        settings[plugin]["enabled"] = True
    # The custom widget intentionally has no automatic session reset operations.
    settings["dankAIUsage"]["enableClaudePrime"] = False
    settings["dankAIUsage"]["publicResetAnnouncements"] = False
    write_json(settings_path, settings)
    print(run("dms", "ipc", "call", "plugin-scan", "scan"))
    time.sleep(1)
    print(run("dms", "ipc", "call", "plugin-scan", "rescan", "dankAIUsage"))
    time.sleep(1)
    for plugin in selected:
        print(run("dms", "ipc", "call", "plugins", "reload", plugin))
    # Settings IPC cannot set arrays in DMS 1.6.3. Its file watcher owns reload.
    settings_path = config/"settings.json"
    data = json.loads(settings_path.read_text())
    bars = data.get("barConfigs", [])
    bar = next((b for b in bars if b.get("enabled", True)), None)
    if bar is None:
        raise RuntimeError("No enabled DMS bar; enable a bar before installing widgets")
    right = bar.setdefault("rightWidgets", [])
    for plugin in ("dankAIUsage", "screenRecorder"):
        present = any((w if isinstance(w, str) else w.get("id")) == plugin for b in bars for key in ("leftWidgets", "centerWidgets", "rightWidgets") for w in b.get(key, []))
        if not present:
            right.insert(0, plugin)
    write_json(settings_path, data)
    print("Installed six plugins; AI Usage and Screen Recorder added to bar.")


if __name__ == "__main__":
    main()
