"""Правила доступа агента: заготовка v1, без UI и без CEL.

Идея взята из лучших образцов (гейт решает и записывает действие, отказ называет правило),
но реализация своя и минимальная: правило — это программа и, при необходимости, подкоманда.
Полноценные политики (выражения, аудит каждого вызова инструмента, экран управления) —
отдельный срез после v1.

Принципы:
- по умолчанию всё запрещено; пустой список правил означает «агент сам ничего не может»;
- сломанное или неоднозначное правило запрещает действие, а не разрешает;
- составная команда (`;`, `&&`, `|`, подстановка, перенаправление) запрещается: правило
  проверяет одну команду, а оболочка выполнит несколько.
"""

from __future__ import annotations

import fnmatch
import shlex
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Decision", "Policy", "Rule"]

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

    def allows_anything(self) -> bool:
        """Есть ли хотя бы одно разрешающее правило: честное состояние «ничего нельзя»."""
        return any(rule.allow for rule in self.rules)
