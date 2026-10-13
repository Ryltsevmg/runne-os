#!/usr/bin/env python3
"""Safe product baseline: selected versions/state/hashes, never credentials."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess


PACKAGES = ('niri', 'dms-shell', 'ghostty', 'runneos-config', 'runneos-software',
            'runneos-ai-usage', 'gnome-keyring', 'seahorse', 'bitwarden', 'snapper')


def run(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20,
                            env=dict(os.environ, LC_ALL='C'))
    return result.stdout.strip() if result.returncode == 0 else None


def baseline(root):
    versions = {}
    licenses = {}
    for name in PACKAGES:
        row = run(['pacman', '-Q', name])
        versions[name] = row.split(' ', 1)[1] if row else None
        info = run(['pacman', '-Qi', name]) or ''
        license_row = next((line for line in info.splitlines() if line.startswith('Licenses')), '')
        licenses[name] = license_row.partition(':')[2].strip()
    services = {}
    for name in ('niri.service', 'dms.service', 'gnome-keyring-daemon.service', 'xdg-desktop-portal.service'):
        services[name] = run(['systemctl', '--user', 'show', name, '--property=ActiveState', '--value'])
    hashes = {}
    for relative in ('packages/runneos-ai-usage/files/collect.py', 'scripts/runneos-terminal.py',
                     'packages/runneos-software/files/gui.py'):
        path = root/relative
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    config_hashes = {}
    for relative in ('.config/niri/config.kdl', '.config/niri/niri-bindings.kdl',
                     '.config/ghostty/config', '.config/DankMaterialShell/settings.json',
                     '.config/xdg-desktop-portal/niri-portals.conf'):
        path = Path.home()/relative
        config_hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    installed_hashes = {}
    for name, path in {'collector':'/usr/bin/runneos-ai-usage', 'terminal':'/usr/bin/runneos-terminal',
                       'software':'/usr/lib/runneos-software/gui.py'}.items():
        installed_hashes[name] = hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None
    autostart = {}
    for label, path in {'system':'/etc/xdg/autostart', 'user':str(Path.home()/'.config/autostart')}.items():
        autostart[label] = sorted(p.name for p in Path(path).glob('*.desktop'))
    defaults = {}
    settings = Path.home()/'.config/DankMaterialShell/settings.json'
    if settings.is_file():
        data = json.loads(settings.read_text())
        defaults['dmsTheme'] = data.get('currentThemeName')
        defaults['dmsCustomThemeDirectory'] = Path(data.get('customThemeFile') or '').parent.name
    defaults['terminalEntryPoint'] = 'runneos-terminal'
    defaults['niriConfiguration'] = 'user config includes DMS-generated input/output files; config hash recorded'
    ai = run(['dms', 'ipc', 'call', 'runneos-ai-usage', 'status'])
    try:
        parsed = json.loads(ai or '{}')
        providers = [{key: row.get(key) for key in ('id', 'status', 'quotas', 'balances', 'local')}
                     for row in parsed.get('providers', [])]
    except ValueError:
        providers = []
    return {'date': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'scope': 'workstation observation, not clean-install acceptance',
            'versions': versions, 'userServices': services, 'sourceSha256': hashes,
            'installedSha256': installed_hashes, 'managedConfigSha256': config_hashes,
            'corePackageLicenseLabels': licenses,
            'autostartEntryNames': autostart, 'selectedDefaults': defaults,
            'aiProviderStatus': providers,
            'keyringAutoUnlock': 'not tested after fresh login',
            'runtimeIntegration': 'future stage, not installed by this task',
            'licenseScope': 'core + bundled dependencies/assets/models; firmware and optional apps require separate classification'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(baseline(args.root), ensure_ascii=False, indent=2)+'\n')
    print('Safe baseline saved:', args.output)
