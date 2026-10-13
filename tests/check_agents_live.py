#!/usr/bin/python3
"""Живой протокол вертикального сценария runneos-agents на этой машине.

Запуск:  cd ~/runneos && python tests/check_agents_live.py [--no-live] [--cli PATH] [--keep]

Что делает: проходит сценарий из раздела 10 контракта
packages/runneos-agents/INTERFACE.md — init, провайдеры, агент, реальный прогон
codex, продолжение сессии, новая сессия, недоступный провайдер, поиск секретов,
doctor (§10.2) — и печатает по каждому шагу команду, код возврата и значимый
фрагмент вывода.

Границы честности:
* данные runneos-agents изолированы во временном XDG_DATA_HOME (tempfile.mkdtemp),
  реальный ~/.local/share/runneos не затрагивается;
* живой прогон codex тратит подписку пользователя: промпты короткие, не более
  четырёх прогонов за запуск, флаг --no-live пропускает шаги 4–6 (SKIP);
* codex хранит свои сессии в ~/.codex, как при обычном запуске CLI; скрипт
  туда не заглядывает;
* скрипт ничего не пишет в БД и не запускает задач, кроме явных команд CLI;
* если CLI ещё не реализован, скрипт честно сообщает об этом, а не падает.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC_DIR = REPO / 'packages/runneos-agents/files'
SOURCE_CLI = SRC_DIR / 'runneos-agents'
MAX_LIVE = 4
ASK_TIMEOUT = 300
STEP_ORDER = ['init', 'providers', 'agent-add', 'ask-pong', 'ask-resume', 'ask-new',
              'missing-provider', 'secrets', 'doctor']


def indent(text, prefix='    '):
    return '\n'.join(prefix + line for line in str(text).splitlines())


def fragment(text, limit=1200):
    text = str(text or '').strip('\n')
    if not text:
        return '(пусто)'
    if len(text) <= limit:
        return text
    half = limit // 2
    return f'{text[:half]}\n… (обрезано {len(text) - limit} символов) …\n{text[-half:]}'


def short(text, limit=110):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def unwrap(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ('agents', 'sessions', 'events', 'messages', 'providers', 'items'):
            if isinstance(value.get(key), list):
                return value[key]
        return [value]
    return [value]


def parse_json_objects(text):
    """Принимает и одиночный JSON, и JSON Lines, и обёртки вида {"agents": [...]}."""
    text = str(text or '').strip()
    if not text:
        return []
    try:
        return unwrap(json.loads(text))
    except json.JSONDecodeError:
        pass
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return unwrap(rows)


def pick_text(stdout, stderr):
    """Финальный текст ответа: сначала stdout --json, затем события agent_message в stderr."""
    for source in (stdout, stderr):
        for row in parse_json_objects(source):
            if not isinstance(row, dict):
                continue
            for key in ('text', 'answer', 'reply', 'output', 'message'):
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip(), 'поле ' + key
            inner = row.get('result')
            if isinstance(inner, dict):
                for key in ('text', 'answer', 'reply', 'output'):
                    value = inner.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip(), 'поле result.' + key
    return '', ''


def find_row(rows, *identifiers):
    identifiers = [value for value in identifiers if value]
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ('id', 'agent_id', 'slug', 'name', 'session_id'):
            if row.get(key) in identifiers:
                return row
    return None


class Runner:
    def __init__(self, cli_argv, data_home, live, ask_timeout):
        self.cli = cli_argv
        self.data_home = data_home
        self.live = live
        self.ask_timeout = ask_timeout
        self.env = dict(os.environ)
        self.env['XDG_DATA_HOME'] = data_home
        self.env['PYTHONDONTWRITEBYTECODE'] = '1'
        local_bin = str(Path.home() / '.local/bin')
        if local_bin not in self.env.get('PATH', '').split(os.pathsep):
            self.env['PATH'] = local_bin + os.pathsep + self.env.get('PATH', '')
        if cli_argv[0] == sys.executable:  # CLI из репозитория, а не установленный
            self.env['PYTHONPATH'] = str(SRC_DIR) + (
                os.pathsep + self.env['PYTHONPATH'] if self.env.get('PYTHONPATH') else '')
        self.root = Path(data_home) / 'runneos/agents'
        self.db = self.root / 'agents.db'
        self.runs = self.root / 'runs'
        self.live_runs = 0
        self.transcript = []
        self.availability = {}
        self.agent = 'tester'
        self.results = {}

    def call(self, args, timeout=60, note=''):
        argv = list(self.cli) + list(args)
        print(f'  $ {shlex.join(argv)}' + (f'   # {note}' if note else ''))
        started = time.monotonic()
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, env=self.env,
                                  timeout=timeout, cwd=str(REPO))
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except FileNotFoundError:
            code, out, err = None, '', f'команда не найдена: {argv[0]}'
        except subprocess.TimeoutExpired as expired:
            code = None
            out = expired.stdout.decode() if isinstance(expired.stdout, bytes) else (expired.stdout or '')
            err = (expired.stderr.decode() if isinstance(expired.stderr, bytes) else (expired.stderr or ''))
            err += f'\nтаймаут {timeout} с'
        except OSError as error:
            code, out, err = None, '', f'{type(error).__name__}: {error}'
        elapsed = time.monotonic() - started
        shown_code = 'нет (не запустилась)' if code is None else str(code)
        print(f'    код возврата: {shown_code} · {elapsed:.1f} с')
        for label, text in (('stdout', out), ('stderr', err)):
            if text.strip():
                print(f'    {label}:')
                print(indent(fragment(text), '      '))
        self.transcript.append((shlex.join(argv), code, out, err))
        return code, out, err

    def ask(self, args, note=''):
        """Живой прогон: считаем бюджет и не превышаем MAX_LIVE."""
        self.live_runs += 1
        print(f'  [живой прогон {self.live_runs} из {MAX_LIVE} — тратит подписку пользователя]')
        return self.call(['ask'] + list(args), timeout=self.ask_timeout, note=note)


def step_init(ctx):
    code, out, err = ctx.call(['init'], timeout=60)
    if code != 0:
        return 'FAIL', f'init вернул код {code}: {fragment(err or out, 200)}'
    if not ctx.db.exists():
        return 'FAIL', f'init прошёл, но БД не найдена: {ctx.db}'
    tables = []
    try:
        with sqlite3.connect(f'file:{ctx.db}?mode=ro', uri=True) as connection:
            tables = sorted(row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
    except sqlite3.Error as error:
        return 'FAIL', f'БД создана, но не читается: {error}'
    required = {'agents', 'sessions', 'messages', 'events'}
    missing = required - set(tables)
    if missing:
        return 'FAIL', f'в БД нет таблиц: {", ".join(sorted(missing))}'
    return 'PASS', f'корень и БД созданы ({ctx.db.stat().st_size} Б), таблицы: {", ".join(tables)}'


def step_providers(ctx):
    code, out, err = ctx.call(['provider', 'list', '--json'], timeout=60)
    if code != 0:
        return 'FAIL', f'provider list вернул код {code}: {fragment(err or out, 200)}'
    rows = [row for row in parse_json_objects(out) if isinstance(row, dict)]
    if not rows:
        return 'FAIL', f'provider list --json не дал разбираемых строк: {fragment(out, 200)}'
    for row in rows:
        provider = str(row.get('provider') or row.get('id') or '?')
        ctx.availability[provider] = row
    available = [name for name, row in ctx.availability.items() if row.get('available')]
    codex = ctx.availability.get('codex')
    if not codex or not codex.get('available'):
        reason = (codex or {}).get('reason') or 'провайдер codex не подтверждён'
        return 'FAIL', f'codex недоступен: {reason}'
    version = codex.get('version') or 'версия неизвестна'
    unavailable = [name for name, row in ctx.availability.items() if not row.get('available')]
    detail = f'доступны: {", ".join(sorted(available)) or "нет"}; codex={version}'
    if unavailable:
        detail += f'; недоступны: {", ".join(sorted(unavailable))}'
    return 'PASS', detail


def step_agent_add(ctx):
    code, out, err = ctx.call(['agent', 'add', ctx.agent, '--role', 'проверочный агент'], timeout=60)
    if code != 0:
        return 'FAIL', f'agent add вернул код {code}: {fragment(err or out, 200)}'
    workspace = ctx.root / 'workspaces' / ctx.agent
    if not workspace.is_dir():
        return 'FAIL', f'агент добавлен, но воркспейса нет: {workspace}'
    list_code, list_out, list_err = ctx.call(['agent', 'list', '--json'], timeout=60)
    rows = parse_json_objects(list_out) if list_code == 0 else []
    row = find_row(rows, ctx.agent)
    if row is None:
        return 'FAIL', f'агент не найден в agent list --json: {fragment(list_err or list_out, 200)}'
    state = str(row.get('state') or row.get('status') or '')
    return 'PASS', f'агент {ctx.agent} создан, воркспейс {workspace}, состояние {state or "?"}'


def sessions_of(ctx, agent=None):
    code, out, err = ctx.call(['sessions', agent or ctx.agent, '--json'], timeout=60)
    if code != 0:
        return None, f'код {code}: {fragment(err or out, 200)}'
    rows = [row for row in parse_json_objects(out) if isinstance(row, dict)]
    return rows, ''


def step_ask_pong(ctx):
    if not ctx.live:
        return 'SKIP', 'режим --no-live: живой прогон не выполняется'
    if ctx.live_runs >= MAX_LIVE:
        return 'SKIP', f'лимит живых прогонов ({MAX_LIVE}) исчерпан'
    code, out, err = ctx.ask([ctx.agent, 'ответь ровно PONG', '--json'])
    if code != 0:
        return 'FAIL', f'ask вернул код {code}: {fragment(err or out, 300)}'
    text, source = pick_text(out, err)
    if not text:
        return 'FAIL', f'в выводе нет текста ответа (не найдено поле text/answer в stdout/stderr)'
    if 'PONG' not in text.upper():
        return 'FAIL', f'ответ не содержит PONG: {fragment(text, 200)}'
    state_code, state_out, state_err = ctx.call(['agent', 'list', '--json'], timeout=60)
    state = '?'
    if state_code == 0:
        row = find_row(parse_json_objects(state_out), ctx.agent)
        state = str((row or {}).get('state') or (row or {}).get('status') or '?')
    if state != 'idle':
        return 'FAIL', f'ответ получен ({fragment(text, 80)}), но состояние агента {state}, а не idle'
    rows, problem = sessions_of(ctx)
    provider_session = ''
    if rows:
        provider_session = str(rows[0].get('provider_session_id') or '')
    ctx.results['provider_session_id'] = provider_session
    detail = f'ответ «{fragment(text, 60)}» ({source}), состояние idle'
    detail += f', provider_session_id={provider_session or "нет"}'
    return 'PASS', detail


def step_ask_resume(ctx):
    if not ctx.live:
        return 'SKIP', 'режим --no-live: живой прогон не выполняется'
    if ctx.live_runs >= MAX_LIVE:
        return 'SKIP', f'лимит живых прогонов ({MAX_LIVE}) исчерпан'
    code, out, err = ctx.ask([ctx.agent, 'назови число 42', '--json'], note='без --new: та же сессия')
    if code != 0:
        return 'FAIL', f'повторный ask вернул код {code}: {fragment(err or out, 300)}'
    text, _ = pick_text(out, err)
    rows, problem = sessions_of(ctx)
    if rows is None:
        return 'FAIL', f'sessions не прочитался: {problem}'
    if len(rows) != 1:
        return 'FAIL', f'сессий {len(rows)}, а после resume должна остаться одна'
    provider_session = str(rows[0].get('provider_session_id') or '')
    if not provider_session:
        return 'FAIL', 'у единственной сессии нет provider_session_id: resume не подтверждён провайдером'
    previous = ctx.results.get('provider_session_id') or ''
    if previous and provider_session != previous:
        return 'FAIL', f'provider_session_id изменился ({previous} → {provider_session}): это новая сессия'
    detail = f'сессия одна, provider_session_id={provider_session}'
    if previous:
        detail += ' (совпал с шагом 4)'
    if text:
        detail += f', ответ «{fragment(text, 40)}»'
    return 'PASS', detail


def step_ask_new(ctx):
    if not ctx.live:
        return 'SKIP', 'режим --no-live: живой прогон не выполняется'
    if ctx.live_runs >= MAX_LIVE:
        return 'SKIP', f'лимит живых прогонов ({MAX_LIVE}) исчерпан'
    code, out, err = ctx.ask([ctx.agent, 'назови число 7', '--new', '--json'], note='--new: новая сессия')
    if code != 0:
        return 'FAIL', f'ask --new вернул код {code}: {fragment(err or out, 300)}'
    rows, problem = sessions_of(ctx)
    if rows is None:
        return 'FAIL', f'sessions не прочитался: {problem}'
    if len(rows) != 2:
        return 'FAIL', f'сессий {len(rows)}, а после --new ожидалось две'
    return 'PASS', f'сессий две, новая создана; ids: {", ".join(str(row.get("id"))[:8] for row in rows)}'


def step_missing_provider(ctx):
    """Негативный случай: провайдер агента недоступен, ask обязан дать код 1 без трассировки.

    Порядок выбора: сначала известный, но недоступный провайдер (opencode и т.п.),
    затем заведомо отсутствующий адаптер. Второй вариант — честная проверка того же
    контракта («отсутствие провайдера — состояние, а не падение»), потому что на этой
    машине и codex, и opencode подтверждены.
    """
    unavailable = [name for name, row in ctx.availability.items() if not row.get('available')]
    provider = 'opencode' if not (ctx.availability.get('opencode') or {}).get('available') else (
        unavailable[0] if unavailable else '')
    fallback = not provider
    if fallback:
        provider = 'nonexistent'
        reason = 'адаптера с таким именем нет в сборке'
    else:
        reason = str((ctx.availability.get(provider) or {}).get('reason') or 'доступность не подтверждена')
    agent_id = 'tester-missing'
    add_code, add_out, add_err = ctx.call(
        ['agent', 'add', agent_id, '--provider', provider, '--role', 'негативная проверка'], timeout=60)
    if add_code != 0 and fallback:
        return 'SKIP', (f'все известные провайдеры доступны, а CLI не принимает несуществующий '
                        f'({provider}, код {add_code}): негативный случай не воспроизводится')
    if add_code != 0:
        return 'FAIL', f'agent add --provider {provider} вернул код {add_code}: {fragment(add_err or add_out, 200)}'
    code, out, err = ctx.call(['ask', agent_id, 'привет', '--json'], timeout=120,
                              note=f'провайдер {provider} недоступен: {fragment(reason, 60)}')
    combined = (out or '') + (err or '')
    if code != 1:
        return 'FAIL', f'ожидался код 1 для недоступного провайдера, получен {code}: {fragment(combined, 300)}'
    if 'Traceback' in combined:
        return 'FAIL', f'в выводе есть Traceback: {fragment(combined, 300)}'
    message = ''
    for row in parse_json_objects(out) + parse_json_objects(err):
        if isinstance(row, dict) and str(row.get('error') or '').strip():
            message = str(row['error']).strip()
            break
    if not message:
        message = (err or out or '').strip()
    if not message or not re.search(r'[A-Za-zА-Яа-я]', message):
        return 'FAIL', f'нет человекочитаемой причины: {fragment(combined, 200)}'
    state = '?'
    state_code, state_out, _ = ctx.call(['agent', 'list', '--json'], timeout=60)
    if state_code == 0:
        row = find_row(parse_json_objects(state_out), agent_id)
        state = str((row or {}).get('state') or (row or {}).get('status') or '?')
    mode = 'несуществующий адаптер' if fallback else 'известный недоступный провайдер'
    return 'PASS', (f'{provider} ({mode}) → код 1 без трассировки, состояние {state}; '
                    f'причина: {fragment(message, 120)}')


def secrets_in_env():
    pattern = re.compile(r'(_API_KEY|_TOKEN|_SECRET|_PASSWORD)$', re.IGNORECASE)
    found = {}
    for name, value in os.environ.items():
        if pattern.search(name) and len(str(value)) >= 8:
            found[name] = str(value)
    return found


def secret_patterns():
    return {
        'sk-': re.compile(r'sk-[A-Za-z0-9]{8,}'),
        'ghp_': re.compile(r'ghp_[A-Za-z0-9]{8,}'),
        'xoxb-': re.compile(r'xoxb-[A-Za-z0-9-]{8,}'),
        'AIza': re.compile(r'AIza[A-Za-z0-9_-]{8,}'),
        'Bearer ': re.compile(r'Bearer\s+[A-Za-z0-9._-]{12,}'),
    }


def scanned_files(ctx):
    """БД + журналы прогонов (корень и воркспейсы) + манифесты воркспейсов.

    Журналы прогонов сейчас пишутся не всеми сборками: каталог runs/ может быть
    пуст. Проверка честно сообщает, сколько файлов реально просмотрено.
    """
    files = []
    if ctx.db.exists():
        files.append(ctx.db)
    if ctx.runs.is_dir():
        files.extend(sorted(path for path in ctx.runs.rglob('*') if path.is_file()))
    workspaces = ctx.root / 'workspaces'
    if workspaces.is_dir():
        for workspace in sorted(path for path in workspaces.iterdir() if path.is_dir()):
            runs = workspace / 'runs'
            if runs.is_dir():
                files.extend(sorted(path for path in runs.rglob('*') if path.is_file()))
            manifest = workspace / 'agent.json'
            if manifest.is_file():
                files.append(manifest)
    return files


def step_secrets(ctx):
    values = secrets_in_env()
    files = scanned_files(ctx)
    if not files:
        return 'FAIL', 'нечего проверять: БД и каталог логов прогонов не найдены'
    logs = [path for path in files if 'runs' in path.parts]
    jsonl = [path for path in logs if path.suffix == '.jsonl']
    hits = []
    for path in files:
        try:
            blob = path.read_bytes()
        except OSError as error:
            return 'FAIL', f'{path} не читается: {error}'
        for name, value in values.items():
            if value.encode() in blob:
                hits.append(f'{path.name}: значение переменной {name}')
    patterns = secret_patterns()
    pattern_hits = []
    for path in files:
        text = path.read_bytes().decode('utf-8', 'replace')
        for label, regex in patterns.items():
            for match in regex.finditer(text):
                pattern_hits.append(f'{path.name}: шаблон {label} (длина {len(match.group(0))})')
    transcript_hits = []
    for command, _code, out, err in ctx.transcript:
        blob = ((out or '') + (err or '')).encode()
        for name, value in values.items():
            if value.encode() in blob:
                transcript_hits.append(f'вывод «{fragment(command, 40)}»: значение переменной {name}')
    detail = (f'файлов проверено: {len(files)} (agents.db {ctx.db.stat().st_size if ctx.db.exists() else 0} Б; '
              f'журналов прогонов: {len(logs)} (из них .jsonl: {len(jsonl)}); '
              f'манифестов воркспейсов: {len([path for path in files if path.name == "agent.json"])}); '
              f'переменных окружения *_API_KEY/*_TOKEN/*_SECRET/*_PASSWORD (длина ≥ 8): {len(values)}'
              + (f' ({", ".join(sorted(values))})' if values else ' — таких переменных в окружении нет'))
    if ctx.live_runs and not jsonl:
        return 'FAIL', (f'было {ctx.live_runs} живых прогонов, но файлового журнала .jsonl нет: '
                        f'оркестратор не записал <workspace>/runs/<session>.jsonl. {detail}')
    if hits or pattern_hits or transcript_hits:
        evidence = '; '.join(hits + pattern_hits + transcript_hits)
        return 'FAIL', f'найдены секреты: {evidence}. {detail}'
    return 'PASS', detail + '; совпадений значений и шаблонов (sk-, ghp_, xoxb-, AIza, Bearer) нет'


def version_core(text):
    """Версия без префиксов: «codex-cli 0.162.1» и «0.162.1» дают одно ядро."""
    match = re.search(r'\d+\.\d+(?:\.\d+)?', str(text or ''))
    return match.group(0) if match else ''


def step_doctor(ctx):
    """§10.2 контракта: doctor --json — корень/БД/воркспейсы ок и живой codex виден."""
    code, out, err = ctx.call(['doctor', '--json'], timeout=60)
    if code != 0:
        return 'FAIL', f'doctor вернул код {code}: {fragment(err or out, 300)}'
    rows = [row for row in parse_json_objects(out) if isinstance(row, dict)]
    payload = rows[0] if rows else {}
    if not payload or not isinstance(payload.get('checks'), list):
        return 'FAIL', f'doctor --json не дал разбираемого объекта: {fragment(out, 300)}'
    reported_root = str(payload.get('root') or '')
    if reported_root != str(ctx.root):
        return 'FAIL', (f'doctor смотрит не в изолированный корень: {reported_root or "нет"} '
                        f'вместо {ctx.root} — XDG_DATA_HOME не соблюдается')
    checks = {str(item.get('name')): item for item in payload['checks'] if isinstance(item, dict)}
    required = ('корень данных', 'база', 'воркспейсы агентов')
    problems = []
    details = []
    for name in required:
        item = checks.get(name)
        if item is None:
            problems.append(f'нет проверки «{name}»')
            continue
        details.append(f'{name}={item.get("detail")}')
        if not item.get('ok'):
            problems.append(f'«{name}» не ок: {item.get("detail")}')
    codex_check = checks.get('провайдер codex')
    if codex_check is None:
        problems.append('doctor не сообщил состояние провайдера codex')
    else:
        details.append(f'провайдер codex={codex_check.get("detail")}')
        actual = version_core(codex_check.get('detail'))
        if not codex_check.get('ok'):
            problems.append(f'провайдер codex не ок: {codex_check.get("detail")}')
        elif not actual:
            problems.append(f'в состоянии провайдера codex нет живой версии: {codex_check.get("detail")}')
        else:
            expected = version_core((ctx.availability.get('codex') or {}).get('version'))
            if expected and expected != actual:
                problems.append(f'версия codex в doctor ({actual}) '
                                f'не совпала с provider list ({expected})')
    if problems:
        return 'FAIL', '; '.join(problems)
    counts = ((payload.get('stats') or {}).get('counts') or {}) if isinstance(payload.get('stats'), dict) else {}
    data = (f'данные: агентов {counts.get("agents", "?")}, сессий {counts.get("sessions", "?")}'
            if counts else 'статистика в ответе отсутствует')
    return 'PASS', f'doctor ок, {"; ".join(details)}; {data}'


STEPS = [
    ('init', 'создание корня и БД', [], step_init),
    ('providers', 'доступность провайдеров', ['init'], step_providers),
    ('agent-add', 'агент и воркспейс', ['init', 'providers'], step_agent_add),
    ('ask-pong', 'реальный прогон: ответ PONG', ['agent-add'], step_ask_pong),
    ('ask-resume', 'продолжение той же сессии', ['ask-pong'], step_ask_resume),
    ('ask-new', 'новая сессия (--new)', ['ask-resume'], step_ask_new),
    ('missing-provider', 'недоступный провайдер', ['providers', 'agent-add'], step_missing_provider),
    ('secrets', 'секреты в БД и логах', ['init'], step_secrets),
    ('doctor', 'doctor: корень, БД, воркспейсы, codex', ['init', 'providers', 'agent-add'], step_doctor),
]


def resolve_cli(explicit):
    if explicit:
        path = Path(explicit)
        if not path.exists():
            return None, f'указанный --cli {explicit} не существует'
        argv = [str(path)] if os.access(path, os.X_OK) else [sys.executable, str(path)]
        return argv, f'--cli {explicit}'
    env_path = os.environ.get('RUNNEOS_AGENTS_CLI')
    if env_path:
        path = Path(env_path)
        if path.exists():
            argv = [str(path)] if os.access(path, os.X_OK) else [sys.executable, str(path)]
            return argv, f'RUNNEOS_AGENTS_CLI={env_path}'
    found = shutil.which('runneos-agents')
    if found:
        return [found], f'PATH ({found})'
    if SOURCE_CLI.exists():
        argv = [str(SOURCE_CLI)] if os.access(SOURCE_CLI, os.X_OK) else [sys.executable, str(SOURCE_CLI)]
        return argv, f'исходники репозитория ({SOURCE_CLI})'
    return None, 'ни в PATH, ни в packages/runneos-agents/files/'


def print_header(cli, source, data_home, live):
    print('=' * 78)
    print('RunneOS Agents · живой протокол вертикального сценария')
    print(f'репозиторий: {REPO}')
    print(f'CLI: {shlex.join(cli)}  [{source}]')
    print(f'изолированный XDG_DATA_HOME: {data_home}')
    print(f'  (реальный ~/.local/share/runneos не затрагивается; это и есть корень БД)')
    print(f'живой прогон codex: {"выключен (--no-live)" if not live else f"включён, не более {MAX_LIVE} прогонов"}')
    print('=' * 78)


def main():
    parser = argparse.ArgumentParser(description='Живой протокол вертикального сценария runneos-agents')
    parser.add_argument('--no-live', action='store_true', help='пропустить шаги 4–6 (реальные прогоны codex) и пометить их SKIP')
    parser.add_argument('--cli', help='путь к исполняемому runneos-agents (по умолчанию PATH или исходники репозитория)')
    parser.add_argument('--keep', action='store_true', help='сохранить временный XDG_DATA_HOME после проверки')
    parser.add_argument('--ask-timeout', type=int, default=ASK_TIMEOUT, help=f'таймаут одного ask, с (по умолчанию {ASK_TIMEOUT})')
    args = parser.parse_args()

    cli, source = resolve_cli(args.cli)
    data_home = tempfile.mkdtemp(prefix='runneos-agents-check-')
    live = not args.no_live
    if cli is None:
        print_header([args.cli or 'runneos-agents'], source, data_home, live)
        print()
        print('КОМПОНЕНТ ПОКА НЕДОСТУПЕН: CLI runneos-agents не найден.')
        print(f'  искали: --cli / RUNNEOS_AGENTS_CLI / PATH / {SOURCE_CLI}')
        print('  Живая проверка не может быть выполнена честно; трассировки нет намеренно.')
        print('  Ожидаемо, пока не завершена задача A3 (CLI). Повторите запуск после её готовности.')
        print()
        print('=' * 78)
        print('СВОДКА')
        print(f'1. init                              FAIL   CLI runneos-agents не найден')
        for index, (key, title, _deps, _fn) in enumerate(STEPS[1:], start=2):
            print(f'{index}. {title:<33} SKIP   зависит от шага 1')
        print('-' * 78)
        print(f'PASS: 0  FAIL: 1  SKIP: {len(STEPS) - 1}   живых прогонов: 0 из {MAX_LIVE}')
        print(f'код возврата: 1')
        shutil.rmtree(data_home, ignore_errors=True)
        return 1

    runner = Runner(cli, data_home, live, args.ask_timeout)
    print_header(cli, source, data_home, live)
    if runner.env.get('PYTHONPATH', '').startswith(str(SRC_DIR)):
        print(f'CLI запускается из исходников: PYTHONPATH={SRC_DIR}')
    if str(Path.home() / '.local/bin') in runner.env['PATH']:
        print('PATH дополнен ~/.local/bin (там лежит codex)')
    print()

    results = {}
    for index, (key, title, deps, function) in enumerate(STEPS, start=1):
        blocked = [STEP_ORDER.index(dep) + 1 for dep in deps if results.get(dep, ('', ''))[0] != 'PASS']
        print(f'--- Шаг {index}: {title} ---')
        if blocked:
            reasons = ', '.join(f'шаг {number} ({results[STEP_ORDER[number - 1]][0]})' for number in blocked)
            status, detail = 'SKIP', f'зависимость не выполнена: {reasons}'
            print(f'    пропущено: {detail}')
        else:
            try:
                status, detail = function(runner)
            except Exception as error:  # никаких трассировок наружу
                status, detail = 'FAIL', f'непредвиденная ошибка шага: {type(error).__name__}: {error}'
            print(f'    {status}: {detail}')
        results[key] = (status, detail)
        print()

    print('=' * 78)
    print('СВОДКА')
    for index, (key, title, _deps, _fn) in enumerate(STEPS, start=1):
        status, detail = results[key]
        print(f'{index}. {title:<33} {status:<5} {short(detail, 110)}')
    passed = sum(1 for status, _ in results.values() if status == 'PASS')
    failed = sum(1 for status, _ in results.values() if status == 'FAIL')
    skipped = sum(1 for status, _ in results.values() if status == 'SKIP')
    print('-' * 78)
    print(f'PASS: {passed}  FAIL: {failed}  SKIP: {skipped}   живых прогонов: {runner.live_runs} из {MAX_LIVE}')
    if failed:
        print('Проверка не пройдена: смотрите шаги FAIL выше.')
    elif skipped:
        print('Пройдено с пропусками: часть сценария не выполнялась (см. SKIP).')
    else:
        print('Все шаги сценария пройдены.')
    keep = args.keep or failed
    if keep:
        print(f'временный XDG_DATA_HOME сохранён для разбора: {data_home}')
    else:
        print(f'временный XDG_DATA_HOME удалён: {data_home}')
        shutil.rmtree(data_home, ignore_errors=True)
    print(f'код возврата: {1 if failed else 0}')
    return 1 if failed else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nПрервано пользователем.')
        sys.exit(130)
    except Exception as error:  # страховка: наружу не должно уходить трассировки
        print(f'\nНепредвиденная ошибка проверки: {type(error).__name__}: {error}')
        sys.exit(1)
