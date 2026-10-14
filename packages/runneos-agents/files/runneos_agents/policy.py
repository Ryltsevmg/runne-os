"""Правила доступа агента: ворота хода с аудитом (семантика A, решение владельца 2026-10-13).

Идея взята из лучших образцов (гейт решает и записывает действие, отказ называет правило),
но реализация своя и минимальная: правило — это программа и, при необходимости, подкоманда.
Полноценные политики (выражения, аудит каждого вызова инструмента, экран управления) —
следующие срезы.

Принципы:
- внутри набора правил по умолчанию всё запрещено; пустой список правил означает
  «агент сам ничего не может»;
- сломанное или неоднозначное правило запрещает действие, а не разрешает;
- составная команда (`;`, `&&`, `|`, подстановка, перенаправление) запрещается: правило
  проверяет одну команду, а оболочка выполнит несколько.

Подключение к рантайму (INTERFACE.md v1.3): оркестратор на каждом ходе вычисляет решение
о запуске провайдера и пишет событие `policy.decision` до старта процесса. По умолчанию
(секции `policy` в манифесте нет) решение только журналируется, ход идёт как раньше —
политика «протокол, а не забор». Принуждение включается явно: `"enforce": true` в секции.
Ошибка разбора секции — fail-closed при любом enforce: ход не начинается, никогда
не «работаем без политики».
"""

from __future__ import annotations

import fnmatch
import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .model import now_iso
from .workspace import read_manifest, write_manifest

__all__ = [
    "Decision",
    "LoadedPolicy",
    "Policy",
    "Rule",
    "load_policy",
    "read_section",
    "write_section",
]

#: Признаки того, что перед нами не одна команда.
_SHELL_METACHARACTERS = (";", "&&", "||", "|", "`", "$(", ">", "<", "&", "\n")


@dataclass(frozen=True)
class Rule:
    """Одно правило: какая программа (и подкоманда) разрешена или запрещена и почему."""

    command: str
    allow: bool
    name: str
    args: str = ""
    note: str = ""


@dataclass(frozen=True)
class Decision:
    """Решение по действию: разрешено/нет и имя правила, которое его приняло."""

    allowed: bool
    rule: str
    reason: str = ""

    def __bool__(self) -> bool:  # удобно в коде: if decision:
        return self.allowed


@dataclass
class Policy:
    """Набор правил агента и его воркспейс."""

    rules: list[Rule] = field(default_factory=list)
    workspace: Path | None = None

    @classmethod
    def default_for(cls, workspace: Path | None) -> "Policy":
        """Разумный минимум: только безопасные команды чтения и состояние своего репозитория."""
        return cls(
            rules=[
                Rule("ls", allow=True, name="read-only-ls", note="просмотр каталога"),
                Rule("cat", allow=True, name="read-only-cat", note="чтение файла"),
                Rule("head", allow=True, name="read-only-head", note="начало файла"),
                Rule("tail", allow=True, name="read-only-tail", note="конец файла"),
                Rule("wc", allow=True, name="read-only-wc", note="подсчёт строк"),
                Rule("grep", allow=True, name="read-only-grep", note="поиск по файлам"),
                Rule("git", args="status", allow=True, name="read-only-git-status"),
                Rule("git", args="diff", allow=True, name="read-only-git-diff"),
                Rule("git", args="log", allow=True, name="read-only-git-log"),
                Rule("git", args="show", allow=True, name="read-only-git-show"),
            ],
            workspace=Path(workspace) if workspace else None,
        )

    def check_command(self, command: str) -> Decision:
        """Можно ли выполнить команду: одна команда, первое совпавшее правило решает."""
        text = " ".join(str(command or "").split())
        if not text:
            return Decision(False, "empty-command", "пустая команда")
        if any(marker in text for marker in _SHELL_METACHARACTERS):
            return Decision(
                False,
                "compound-command",
                "составная команда или перенаправление: правило проверяет одну команду",
            )
        try:
            parts = shlex.split(text)
        except ValueError:
            return Decision(False, "unparsable-command", "команду не удалось разобрать")
        if not parts:
            return Decision(False, "empty-command", "пустая команда")

        executable = Path(parts[0]).name
        arguments = parts[1:]
        allowed_rule: Rule | None = None
        for rule in self.rules:
            if rule.command != executable:
                continue
            if rule.args and (not arguments or arguments[0] != rule.args):
                continue
            if not rule.allow:
                return Decision(False, rule.name, rule.note or f"запрещено правилом {rule.name}")
            allowed_rule = rule
        if allowed_rule is not None:
            return Decision(
                True,
                allowed_rule.name,
                allowed_rule.note or f"разрешено правилом {allowed_rule.name}",
            )
        return Decision(False, "default-deny", f"правило не разрешает команду {executable}")

    def check_path(self, path: Path | str) -> Decision:
        """Можно ли трогать путь. Вне воркспейса — запрет, если правило не разрешает явно."""
        if self.workspace is None:
            return Decision(False, "no-workspace", "воркспейс агента не задан")
        candidate = Path(path)
        try:
            resolved = candidate.resolve()
            root = Path(self.workspace).resolve()
        except OSError:
            return Decision(False, "unresolvable-path", "путь не удалось разрешить")
        if resolved == root or root in resolved.parents:
            return Decision(True, "inside-workspace", "путь внутри воркспейса агента")
        for rule in self.rules:
            if rule.args and fnmatch.fnmatch(str(resolved), rule.args):
                return Decision(
                    rule.allow,
                    rule.name,
                    rule.note or ("разрешено" if rule.allow else f"запрещено правилом {rule.name}"),
                )
        return Decision(False, "outside-workspace", "путь вне воркспейса агента")

    def check_provider(self, provider: str) -> Decision:
        """Можно ли запускать ход через провайдера: имя бинаря сверяется с правилами.

        Правила с фильтром подкоманды (`args`) к запуску провайдера не применяются:
        argv провайдера — внутреннее дело адаптера, не часть решения.
        """
        name = str(provider or "").strip()
        if not name:
            return Decision(False, "empty-provider", "провайдер не указан")
        allowed_rule: Rule | None = None
        for rule in self.rules:
            if rule.command != name or rule.args:
                continue
            if not rule.allow:
                return Decision(False, rule.name, rule.note or f"запрещено правилом {rule.name}")
            allowed_rule = rule
        if allowed_rule is not None:
            return Decision(
                True,
                allowed_rule.name,
                allowed_rule.note or f"разрешено правилом {allowed_rule.name}",
            )
        return Decision(False, "default-deny", f"правило не разрешает запуск провайдера {name}")

    def allows_anything(self) -> bool:
        """Есть ли хотя бы одно разрешающее правило: честное состояние «ничего нельзя»."""
        return any(rule.allow for rule in self.rules)


@dataclass(frozen=True)
class LoadedPolicy:
    """Политика агента в том виде, в каком её прочитали для хода.

    `source` — откуда правила: `default` (секции нет, набор для чтения + явный допуск
    провайдера), `manifest` (секция `policy` разобрана). `error` непуст только при
    fail-closed из-за сломанной секции: тогда правила пусты и enforce принудителен.
    """

    policy: Policy
    enforce: bool
    source: str
    error: str | None = None

    def summary(self) -> str:
        """Короткое человеческое состояние для `doctor` и панели."""
        rules = len(self.policy.rules)
        allowing = sum(1 for rule in self.policy.rules if rule.allow)
        mode = "принуждение" if self.enforce else "только аудит"
        text = f"источник: {self.source}, режим: {mode}, правил: {rules} (разрешающих: {allowing})"
        if self.error:
            text += f"; сломана, ход запрещён: {self.error}"
        return text


def _rule_from_dict(raw: object, index: int) -> Rule:
    """Правило из словаря секции; любая честная ошибка — ValueError с номером правила."""
    if not isinstance(raw, dict):
        raise ValueError(f"правило {index}: ожидается объект, а не {type(raw).__name__}")
    command = str(raw.get("command") or "").strip()
    if not command:
        raise ValueError(f"правило {index}: пусто command")
    name = str(raw.get("name") or "").strip() or f"rule-{index}"
    args = str(raw.get("args") or "")
    note = str(raw.get("note") or "")
    allow = raw.get("allow")
    if not isinstance(allow, bool):
        raise ValueError(f"правило {index} ({name}): allow должно быть true/false")
    return Rule(command=command, args=args, allow=allow, name=name, note=note)


def _broken(workspace: Path | None, error: str) -> LoadedPolicy:
    """Fail-closed: сломанная секция запрещает ход при любом enforce, ошибка названа."""
    return LoadedPolicy(policy=Policy(rules=[], workspace=workspace), enforce=True, source="broken", error=error)


def _default_loaded(workspace: Path | None, provider: str) -> LoadedPolicy:
    """Семантика A без секции: правила чтения + явный допуск провайдера, только аудит."""
    policy = Policy.default_for(workspace)
    policy.rules.append(
        Rule(
            provider,
            allow=True,
            name="default-allow-provider",
            note="семантика A: запуск провайдера разрешён, решение журналируется",
        )
    )
    return LoadedPolicy(policy=policy, enforce=False, source="default")


def _manifest_loaded(section: object, workspace: Path | None) -> LoadedPolicy:
    """Политика из разобранной секции `policy`; сломанная секция — fail-closed."""
    if not isinstance(section, dict):
        return _broken(workspace, f"секция policy — объект, а не {type(section).__name__}")
    try:
        raw_rules = section.get("rules", [])
        if not isinstance(raw_rules, list):
            raise ValueError("rules должен быть списком")
        rules = [_rule_from_dict(raw, index) for index, raw in enumerate(raw_rules, start=1)]
    except ValueError as error:
        return _broken(workspace, str(error))
    enforce = section.get("enforce") is True
    return LoadedPolicy(policy=Policy(rules=rules, workspace=workspace), enforce=enforce, source="manifest")


def load_policy(workspace: Path | str | None, provider: str) -> LoadedPolicy:
    """Политика агента для хода: из секции `policy` манифеста или набор по умолчанию.

    Секция отсутствует (или воркспейса нет) — семантика A: `default_for` плюс явный
    допуск запуска провайдера, enforce выключен. Секция сломана — fail-closed:
    пустые правила, enforce включён, причина названа.
    """
    resolved = Path(workspace) if workspace else None
    section = read_manifest(resolved).get("policy") if resolved is not None else None
    if section is None:
        return _default_loaded(resolved, provider)
    return _manifest_loaded(section, resolved)


def policy_from_json(text: str, provider: str = "") -> LoadedPolicy:
    """Политика из JSON-текста секции — для тестов и будущего экрана управления."""
    try:
        section = json.loads(text)
    except ValueError as error:
        return _broken(None, f"секция policy не разбирается: {error}")
    return _manifest_loaded(section, None)


# --- чтение и запись секции (v1.7: CLI `agent policy` и экран панели) --------


def read_section(workspace: Path | str | None) -> dict | None:
    """Секция `policy` манифеста как есть; None, если её нет (или нет воркспейса)."""
    if workspace is None:
        return None
    return read_manifest(workspace).get("policy")


def write_section(workspace: Path | str, section: dict | None, provider: str) -> None:
    """Записать (или удалить через None) секцию `policy`, сохранив остальной манифест.

    Секция валидируется тем же кодом, что читает её на ходе: сломанную секцию
    сохранить нельзя — `ValueError` с причиной, манифест не меняется.
    """
    target = Path(workspace)
    manifest = read_manifest(target)
    if section is None:
        manifest.pop("policy", None)
    else:
        if not isinstance(section, dict):
            raise ValueError("секция policy должна быть объектом")
        loaded = _manifest_loaded(section, target)
        if loaded.error:
            raise ValueError(loaded.error)
        manifest["policy"] = section
    manifest["updated_at"] = now_iso()
    target.mkdir(parents=True, exist_ok=True)
    write_manifest(target, manifest)
