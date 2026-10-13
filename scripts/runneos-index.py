#!/usr/bin/env python3
"""Build and check the RunneOS knowledge index.

Reads the front-matter of every reference under .agents/skills/runneos/ and
writes .runneos/index.yaml (machine catalog) and .runneos/INDEX.md (the same
grouped by category for people and agents).

    ./scripts/runneos-index.py            # rebuild and report
    ./scripts/runneos-index.py --check    # only validate, non-zero on problems

Checks, because a knowledge base that lies is worse than none:
  * every reference carries front-matter with the required keys;
  * status is one of the agreed values and updated is a real date;
  * every local link in every reference resolves;
  * every reference is reachable from SKILL.md, so nothing becomes invisible.
"""
import argparse
import datetime
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL = ROOT / '.agents/skills/runneos'
REFERENCES = SKILL / 'references'
OUT = ROOT / '.runneos'
STATUSES = ('проверено', 'реализовано', 'предложено', 'известная проблема', 'справка')
REQUIRED = ('status', 'scope', 'component', 'updated')
LINK = re.compile(r'\]\(([^)]+)\)')
FRONT = re.compile(r'^---\n(.*?)\n---\n', re.S)


def parse(path):
    """Return (front-matter dict, body) for one reference."""
    text = path.read_text(encoding='utf-8')
    match = FRONT.match(text)
    if not match:
        return None, text
    fields = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, _, value = line.partition(':')
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields, text[match.end():]


def summarise(body):
    """First substantive paragraph after the title — the one-line 'what is this'.

    Lines that merely restate metadata ('Дата:', 'Обновлено:', 'Статус:') are
    skipped, so the index summarises the subject instead of the bookkeeping.
    """
    noise = re.compile(r'^(Дата|Обновлено|Область|Статус|Основание)\b', re.I)
    paragraph = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', '|', '-', '*')):
            if paragraph:
                break
            continue
        if not paragraph and noise.match(line):
            continue
        paragraph.append(line)
    return ' '.join(paragraph)[:200]


def parse_tags(value):
    """`[a, b]` from front-matter into a list."""
    inner = value.strip().strip('[]')
    return [tag.strip().strip('"').strip("'") for tag in inner.split(',') if tag.strip()]


def title_of(body, fallback):
    for line in body.splitlines():
        if line.startswith('# '):
            return line[2:].strip()
    return fallback


def collect():
    entries, problems = [], []
    documented = set()
    skill_text = (SKILL / 'SKILL.md').read_text(encoding='utf-8')
    for link in LINK.findall(skill_text):
        if link.startswith('references/'):
            documented.add(link.split('/', 1)[1])

    for path in sorted(REFERENCES.glob('*.md')):
        fields, body = parse(path)
        if fields is None:
            problems.append(f'{path.name}: нет front-matter')
            continue
        for key in REQUIRED:
            if not fields.get(key):
                problems.append(f'{path.name}: не заполнено поле {key}')
        if fields.get('status') not in STATUSES:
            problems.append(f'{path.name}: неизвестный статус {fields.get("status")!r}')
        try:
            datetime.date.fromisoformat(fields.get('updated', ''))
        except ValueError:
            problems.append(f'{path.name}: updated не дата: {fields.get("updated")!r}')

        for link in LINK.findall(body):
            target = link.split('#')[0].strip()
            if not target or target.startswith(('http', 'mailto:')):
                continue
            if not (path.parent / target).exists():
                problems.append(f'{path.name}: битая ссылка {target}')

        if path.name not in documented:
            problems.append(f'{path.name}: нет ссылки из SKILL.md — справка невидима')

        entries.append({
            'file': path.name,
            'title': title_of(body, path.stem),
            'summary': summarise(body),
            **{key: fields.get(key, '') for key in ('status', 'scope', 'component', 'verify', 'updated', 'tags')},
        })
    return entries, problems


def check_navigation():
    """Validate links inside .runneos — run after the index is written, so the
    check sees the fresh generated files rather than the previous ones."""
    problems = []
    for path in sorted((ROOT / '.runneos').rglob('*.md')):
        body = path.read_text(encoding='utf-8')
        for link in LINK.findall(body):
            target = link.split('#')[0].strip()
            if not target or target.startswith(('http', 'mailto:')):
                continue
            if not (path.parent / target).exists():
                problems.append(f'.runneos/{path.relative_to(ROOT / ".runneos")}: битая ссылка {target}')
    return problems


def quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def write_index(entries):
    OUT.mkdir(exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    lines = [
        '# Сгенерировано scripts/runneos-index.py — не редактировать вручную.',
        '# Источник: front-matter в .agents/skills/runneos/references/*.md',
        f'generated: {stamp}',
        f'references: {len(entries)}',
        'by-status:',
    ]
    for status in STATUSES:
        count = sum(1 for entry in entries if entry['status'] == status)
        if count:
            lines.append(f'  {quote(status)}: {count}')
    lines.append('entries:')
    for entry in entries:
        lines.append(f'  - file: {quote(entry["file"])}')
        lines.append(f'    title: {quote(entry["title"])}')
        for key in ('status', 'scope', 'component', 'updated'):
            lines.append(f'    {key}: {quote(entry[key])}')
        if entry['verify']:
            lines.append(f'    verify: {quote(entry["verify"])}')
        tags = parse_tags(entry['tags'])
        if tags:
            lines.append('    tags:')
            for tag in tags:
                lines.append(f'      - {quote(tag)}')
        if entry['summary']:
            lines.append(f'    summary: {quote(entry["summary"])}')
    (OUT / 'index.yaml').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    grouped = {}
    for entry in entries:
        grouped.setdefault(entry['scope'], []).append(entry)
    markdown = [
        '# Каталог знаний RunneOS',
        '',
        f'Сгенерировано `scripts/runneos-index.py`, {stamp}. Источник — front-matter справок;',
        'ручные правки здесь теряются, меняйте метаданные в самих справках.',
        '',
        'Статусы: **проверено** — подтверждено на машине или в артефакте; **реализовано** —',
        'сделано, но приёмка не пройдена полностью; **предложено** — решение не принято;',
        '**известная проблема** — воспроизводится, причина или исправление не найдены;',
        '**справка** — описание внешнего или исторического, не состояние системы.',
        '',
    ]
    for scope in sorted(grouped):
        markdown += [f'## {scope}', '', '| Справка | О чём | Статус | Проверка |', '|---|---|---|---|']
        for entry in sorted(grouped[scope], key=lambda item: item['title']):
            verify = f'`{entry["verify"]}`' if entry['verify'] else '—'
            markdown.append(
                f'| [{entry["title"]}](../.agents/skills/runneos/references/{entry["file"]}) | {entry["summary"]} | {entry["status"]} | {verify} |')
        markdown.append('')
    (OUT / 'INDEX.md').write_text('\n'.join(markdown), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='только проверить, ничего не писать')
    args = parser.parse_args()

    entries, problems = collect()
    if not args.check:
        write_index(entries)
    # Checked after writing: the generated index is part of what must be valid.
    problems += check_navigation()
    counts = {status: sum(1 for entry in entries if entry['status'] == status) for status in STATUSES}
    print(f'справок: {len(entries)}')
    print('статусы: ' + ', '.join(f'{status}={count}' for status, count in counts.items() if count))
    if not args.check:
        print(f'записано: {OUT.relative_to(ROOT)}/index.yaml, {OUT.relative_to(ROOT)}/INDEX.md')
    if problems:
        print(f'\nПРОБЛЕМЫ ({len(problems)}):')
        for problem in problems:
            print('  ' + problem)
        return 1
    print('проблем нет: метаданные, ссылки и видимость справок в порядке')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
