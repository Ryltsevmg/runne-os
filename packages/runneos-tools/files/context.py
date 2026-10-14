#!/usr/bin/python3
"""Compact RunneOS state for an agent starting a session.

One command instead of ten: what this machine is, what is running, what the
package repository looks like, what is waiting to be updated, and what the
knowledge base currently claims. Reuses runneos-doctor for the health checks
instead of duplicating them.

    runneos-context            # human-readable digest
    runneos-context --json     # the same as data
"""
import argparse
import datetime
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys


def run(args, timeout=10):
    """Return (code, stdout) — never raises, never prints."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                env=dict(os.environ, LC_ALL='C'))
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 127, ''


def load_doctor():
    """Import the installed doctor, falling back to the repository checkout.

    /usr/bin/runneos-doctor has no .py suffix, so the loader is stated
    explicitly: spec_from_file_location cannot infer it from the name.
    """
    for candidate in (pathlib.Path('/usr/bin/runneos-doctor'),
                      pathlib.Path(__file__).parent / 'doctor.py'):
        if candidate.exists():
            loader = importlib.machinery.SourceFileLoader('runneos_doctor', str(candidate))
            spec = importlib.util.spec_from_loader('runneos_doctor', loader)
            module = importlib.util.module_from_spec(spec)
            try:
                loader.exec_module(module)
                return module
            except Exception:
                continue
    return None


def system_section():
    kernel = run(['uname', '-r'])[1]
    host = run(['hostname'])[1]
    uptime = pathlib.Path('/proc/uptime').read_text().split()[0] if pathlib.Path('/proc/uptime').exists() else ''
    hours = f'{float(uptime) / 3600:.1f} ч' if uptime else '—'
    disk = shutil.disk_usage('/')
    return {
        'host': host,
        'kernel': kernel,
        'uptime': hours,
        'disk_free': f'{disk.free / 1024 ** 3:.0f} GiB ({disk.free / disk.total:.0%})',
    }


def session_section():
    section = {
        'type': os.environ.get('XDG_SESSION_TYPE', '—'),
        'desktop': os.environ.get('XDG_CURRENT_DESKTOP', '—'),
    }
    for name, package in (('niri', 'niri'), ('dms', 'dms-shell'), ('quickshell', 'quickshell')):
        code, value = run(['pacman', '-Q', package])
        section[name] = value.split()[-1] if code == 0 else '—'
    services = {}
    for unit in ('niri.service', 'dms.service'):
        code, value = run(['systemctl', '--user', 'is-active', unit])
        if not value:  # sandboxed agents cannot reach the user manager directly
            user = os.environ.get('USER') or os.environ.get('LOGNAME') or ''
            code, value = run(['systemctl', '--user', f'--machine={user}@.host', 'is-active', unit])
        services[unit] = value or f'неизвестно (код {code})'
    section['services'] = services
    return section


def packages_section():
    code, installed = run(['pacman', '-Qq'])
    runneos = [line for line in installed.splitlines() if line.startswith('runneos-')] if code == 0 else []
    code, pending = run(['pacman', '-Qu'])
    upgrades = [line for line in pending.splitlines() if line.startswith('runneos')] if code == 0 else []
    return {'installed': len(runneos), 'pending': len(upgrades), 'pending_list': upgrades[:5]}


def repository_section():
    conf = pathlib.Path('/etc/pacman.conf')
    if not conf.exists():
        return {'configured': False, 'detail': 'нет /etc/pacman.conf'}
    text = conf.read_text(encoding='utf-8', errors='replace')
    if '[runneos]' not in text:
        return {'configured': False, 'detail': 'раздел [runneos] отсутствует'}
    server = ''
    for line in text.splitlines():
        if line.strip().startswith('Server') and 'runneos' in line:
            server = line.split('=', 1)[1].strip().replace('$arch', 'x86_64')
    if not server:
        return {'configured': True, 'server': '', 'reachable': None, 'detail': 'адрес не найден'}
    probe = f'{server}/runneos.db'
    import socket
    import urllib.parse
    import urllib.request
    host = urllib.parse.urlsplit(probe).hostname or ''
    try:
        socket.getaddrinfo(host, 443)
    except socket.gaierror as error:
        # Distinguish "the name does not resolve here" from "the server is down".
        # MagicDNS is handled by Tailscale and NetworkManager can overwrite
        # /etc/resolv.conf, which silently breaks name resolution.
        return {'configured': True, 'server': server, 'reachable': None,
                'detail': f'имя {host} не разрешается из этого окружения ({error.strerror or error})'}
    try:
        urllib.request.urlopen(probe, timeout=5).read(1)
        reachable = True
    except Exception as error:
        return {'configured': True, 'server': server, 'reachable': False,
                'detail': f'сервер не ответил: {type(error).__name__}'}
    return {'configured': True, 'server': server, 'reachable': reachable}


def snapshots_section():
    # `snapper list` needs root. Passwordless sudo is common on these machines,
    # so it is tried non-interactively first and the tool degrades honestly
    # instead of failing when it is unavailable.
    code, value = run(['snapper', '-c', 'root', 'list', '--columns', 'number,date,description'])
    source = 'snapper'
    if code != 0:
        code, value = run(['sudo', '-n', 'snapper', '-c', 'root', 'list', '--columns', 'number,date,description'])
        source = 'sudo snapper'
    if code != 0:
        return {'available': False, 'detail': 'нужен root (snapper list требует прав)'}
    lines = [line for line in value.splitlines() if line.strip()]
    return {'available': True, 'last': lines[-1].strip() if lines else '—', 'count': max(len(lines) - 2, 0)}


def knowledge_section():
    for candidate in (pathlib.Path.home() / 'runneos/.runneos/index.yaml',
                      pathlib.Path.home() / '.runneos/index.yaml'):
        if candidate.exists():
            counts, generated, in_statuses = {}, '', False
            for line in candidate.read_text(encoding='utf-8').splitlines():
                if line.startswith('generated:'):
                    generated = line.split(':', 1)[1].strip()
                    continue
                if line.startswith('by-status:'):
                    in_statuses = True
                    continue
                if in_statuses:
                    # The block is a list of indented "status": count lines; the
                    # first line that is not indented ends it.
                    if not line.startswith('  '):
                        break
                    key, _, value = line.strip().partition(':')
                    if value.strip().isdigit():
                        counts[key.strip().strip('"')] = int(value.strip())
            return {'found': True, 'index': str(candidate), 'generated': generated, 'statuses': counts,
                    'entry': str(candidate.parent / 'START.md')}
    return {'found': False, 'detail': 'индекс знаний не найден (scripts/runneos-index.py)'}


def health_section():
    doctor = load_doctor()
    if doctor is None:
        return {'available': False, 'detail': 'runneos-doctor не найден'}
    rows = doctor.collect()['checks']
    attention = [row for row in rows if row['status'] == 'attention']
    return {
        'available': True,
        'ok': sum(1 for row in rows if row['status'] == 'ok'),
        'attention': [f'{row["name"]}: {row["detail"]}' for row in attention],
        'unknown': [row['name'] for row in rows if row['status'] == 'unknown'],
    }


def collect():
    return {
        'schemaVersion': 1,
        'generated': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'system': system_section(),
        'session': session_section(),
        'packages': packages_section(),
        'repository': repository_section(),
        'snapshots': snapshots_section(),
        'knowledge': knowledge_section(),
        'health': health_section(),
    }


def render(data):
    out = [f'RunneOS · {data["generated"]}']
    system = data['system']
    out.append(f'  система      {system["host"]}, Linux {system["kernel"]}, '
               f'работает {system["uptime"]}, свободно {system["disk_free"]}')
    session = data['session']
    services = ' '.join(f'{unit.split(".")[0]}={state}' for unit, state in session['services'].items())
    out.append(f'  сеанс        {session["type"]}/{session["desktop"]}, '
               f'niri {session["niri"]}, dms {session["dms"]}; {services}')
    packages = data['packages']
    pending = f', ждут обновления: {packages["pending"]}' if packages['pending'] else ', обновлений нет'
    out.append(f'  пакеты       runneos-*: {packages["installed"]} установлено{pending}')
    repository = data['repository']
    if not repository.get('configured'):
        out.append(f'  репозиторий  не подключён: {repository.get("detail", "")}')
    else:
        state = {True: 'отвечает', False: 'НЕ отвечает', None: 'не проверено'}[repository.get('reachable')]
        detail = repository.get('detail', '')
        out.append(f'  репозиторий  {repository.get("server", "—")} — {state}' + (f' ({detail})' if detail else ''))
    snapshots = data['snapshots']
    out.append(f'  снимки       последний: {snapshots.get("last", snapshots.get("detail", "—"))}')
    knowledge = data['knowledge']
    if knowledge['found']:
        statuses = ', '.join(f'{key} {value}' for key, value in knowledge['statuses'].items())
        out.append(f'  знания       {statuses}')
        out.append(f'               вход: {knowledge["entry"]}')
    else:
        out.append(f'  знания       {knowledge.get("detail", "не найдены")}')
    health = data['health']
    if health['available']:
        out.append(f'  здоровье     {health["ok"]} ok'
                   + (f', unknown: {len(health["unknown"])}' if health['unknown'] else ''))
        for item in health['attention']:
            out.append(f'  внимание     {item}')
    else:
        out.append(f'  здоровье     {health.get("detail")}')
    return '\n'.join(out)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    data = collect()
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else render(data))
    raise SystemExit(0)
