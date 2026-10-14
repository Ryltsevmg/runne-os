"""Адаптер codex CLI (OpenAI Codex CLI, команда `codex exec --json`).

Измерено живьём 2026-10-12 на codex-cli 0.162.1; реальные строки сохранены в
`adapters/fixtures/codex_*.jsonl` (идентификаторы и пути отредактированы).

Подтверждённые факты, на которых стоит адаптер:

* `codex exec --json --skip-git-repo-check -C <dir> -s <sandbox> -- '<prompt>'` печатает JSONL:
  `thread.started{thread_id}` → `turn.started` → `item.completed{…}` → `turn.completed{usage}`;
* `codex exec resume <thread_id> --json --skip-git-repo-check -- '<prompt>'` продолжает сессию
  (в `thread.started` приходит тот же `thread_id`);
* у `resume` нет `-C/--cd` и `-s/--sandbox`: рабочий каталог задаёт `cwd` процесса, а политику
  песочницы передаём ключом конфигурации `sandbox_mode` — ключ проверен `--strict-config`
  (неизвестный ключ там падает, `sandbox_mode` проходит) и принят живым `resume`;
* без разделителя `--` задача с ведущим дефисом ломает разбор аргументов
  («unexpected argument '-P' found»), поэтому `--` стоит всегда;
* stdin закрываем (`DEVNULL`): иначе codex пишет «Reading additional input from stdin...»;
* бинарь пишет в свой `~/.codex`; `CODEX_HOME` из окружения уходит дальше как есть — адаптер
  окружение не подменяет и ничего из `~/.codex` не читает.

Разбор строк в нормализованные события (INTERFACE.md, раздел 5):

* `thread.started` → `provider_session`;
* `item.completed` типа `agent_message` → `agent_message`, `reasoning` → `reasoning`,
  `command_execution`/`local_shell_call` → `command`, `file_change` → `file_change`;
* `turn.completed.usage` → `usage`; конверты `error` и `turn.failed` → `error`;
* `item.completed` с неизвестным типом → `raw`, ничего не теряем.

Два осознанных решения:

1. `turn.started` провайдера не дублируется: адаптер сам отдаёт `turn.started` первым событием
   каждого хода, поэтому у всех провайдеров одинаковое начало потока.
2. `item.completed` с `item.type == "error"` уходит в `raw`, а не в `error`. Живые прогоны
   показывают, что так codex сообщает и о необязательных предупреждениях («Model metadata …
   not found», «This session was recorded with model … but is resuming with …»), после которых
   ход завершается успешно. Итог хода определяют конверты `error`/`turn.failed` и код возврата,
   иначе успешный ход получал бы `ok=False`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .base import CliJsonlAdapter, RunContext

__all__ = ["CodexAdapter"]

#: Допустимые значения `-s/--sandbox` (из справки `codex exec --help`).
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")


class CodexAdapter(CliJsonlAdapter):
    """codex как внешний процесс: JSONL в stdout, нормализованные события наружу."""

    provider = "codex"
    binary_name = "codex"
    #: Проверено на codex-cli 0.162.1 (2026-10-13): при входе печатает
    #: «Logged in using ChatGPT» и возвращает 0.
    auth_args = ("login", "status")
    auth_how = "codex login"

    def data_dirs(self) -> list[Path]:
        """codex пишет сессии и состояние входа в свой `~/.codex` (rw для bwrap)."""
        return [Path.home() / ".codex"]

    def __init__(
        self,
        binary: str | None = None,
        *,
        sandbox: str = "workspace-write",
        **kwargs: Any,
    ) -> None:
        """`sandbox` — политика песочницы codex для новых и продолженных ходов.

        По умолчанию `workspace-write`: воркспейс агента — его рабочая область.
        """
        if sandbox not in SANDBOX_MODES:
            raise ValueError(
                f"недопустимая политика песочницы: {sandbox!r}; ожидается одна из {SANDBOX_MODES}"
            )
        super().__init__(binary, **kwargs)
        self.sandbox = str(sandbox)

    # --- командная строка

    def interpret_auth(self, returncode: int, stdout: str, stderr: str) -> tuple[bool | None, str]:
        """`codex login status`: 0 и «Logged in …» — вход выполнен.

        Порядок проверок важен: «Not logged in» содержит подстроку «logged in»,
        поэтому отрицательная форма распознаётся раньше положительной. Ненулевой
        код без узнаваемого текста трактуем как «нет входа» с цитатой вывода.
        """
        text = " ".join((stdout or "").split()) or " ".join((stderr or "").split())
        quoted = text[:200] or f"пустой вывод, код {returncode}"
        lowered = text.lower()
        if "not logged in" in lowered:
            return False, quoted
        if returncode == 0 and "logged in" in lowered:
            return True, quoted
        if returncode != 0:
            return False, quoted
        return None, f"неизвестный ответ codex login status (код 0): {quoted}"

    def build_argv(
        self,
        binary: str,
        workspace: Path,
        prompt: str,
        *,
        model: str | None,
        provider_session_id: str | None,
    ) -> list[str]:
        """Командная строка `codex exec` (новый ход или продолжение сессии)."""
        if provider_session_id:
            # У `resume` нет -C/--cd и -s: каталог задаёт cwd, песочницу — sandbox_mode.
            argv = [
                binary,
                "exec",
                "resume",
                str(provider_session_id),
                "--json",
                "--skip-git-repo-check",
                "-c",
                f"sandbox_mode={self.sandbox}",
            ]
        else:
            argv = [
                binary,
                "exec",
                "--json",
                "--skip-git-repo-check",
                "-C",
                str(workspace),
                "-s",
                self.sandbox,
            ]
        if model:
            argv += ["-m", str(model)]
        argv += ["--", prompt]
        return argv

    # --- разбор строк

    def extract_session_id(self, obj: dict) -> str | None:
        """Идентификатор сессии: `thread_id` из `thread.started`."""
        session_id = obj.get("thread_id")
        return str(session_id) if session_id else None

    def normalize(self, obj: dict, ctx: RunContext) -> Iterator[dict]:
        """Разобрать строку JSONL codex в нормализованные события."""
        envelope = str(obj.get("type") or "")

        if envelope == "thread.started":
            return  # идентификатор сессии уже отдан отдельным событием
        if envelope == "turn.started":
            return  # адаптер уже отдал turn.started первым событием хода
        if envelope == "turn.completed":
            usage = obj.get("usage")
            if isinstance(usage, dict) and usage:
                yield {"kind": "usage", "usage": dict(usage)}
            return
        if envelope == "error":
            text = self._envelope_error_text(obj)
            if text:
                # Codex печатает конвертом `error` и временные сообщения вроде
                # «Reconnecting... 2/5 (stream disconnected…)»: ход после них завершается
                # успешно. Помечаем такие как transient, чтобы оркестратор не считал
                # провалом recovered-обрыв.
                event: dict = {"kind": "error", "text": text}
                if _is_transient_notice(text):
                    event["transient"] = True
                yield event
                return
            yield {"kind": "raw", "payload": obj}
            return
        if envelope == "turn.failed":
            text = self._envelope_error_text(obj)
            if text:
                yield {"kind": "error", "text": text}
                return
            yield {"kind": "raw", "payload": obj}
            return
        if envelope in ("item.completed", "item.started", "item.updated"):
            yield from self._normalize_item(obj, envelope)
            return

        yield {"kind": "raw", "payload": obj}

    # --- разбор частей

    def _normalize_item(self, obj: dict, envelope: str) -> Iterator[dict]:
        """Разобрать элемент хода (`item.completed` и родственные конверты)."""
        item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        item_type = str(item.get("type") or "")

        # Незавершённый элемент пропускаем только для известных типов: тот же элемент придёт
        # completed целиком. Неизвестные типы отдаём всегда, чтобы ничего не потерять.
        if envelope != "item.completed" and item_type in (
            "agent_message",
            "reasoning",
            "command_execution",
            "local_shell_call",
            "file_change",
            "error",
        ):
            return

        if item_type == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text:
                yield {"kind": "agent_message", "text": text}
                return
            yield {"kind": "raw", "payload": obj}
            return

        if item_type == "reasoning":
            text = self._reasoning_text(item)
            if text:
                yield {"kind": "reasoning", "text": text}
                return
            yield {"kind": "raw", "payload": obj}
            return

        if item_type in ("command_execution", "local_shell_call"):
            command = item.get("command")
            if isinstance(command, list):
                command = " ".join(str(part) for part in command)
            if isinstance(command, str) and command.strip():
                yield {"kind": "command", "text": command.strip()}
                return
            yield {"kind": "raw", "payload": obj}
            return

        if item_type == "file_change":
            for event in self._file_change_events(item):
                yield event
            return

        yield {"kind": "raw", "payload": obj}

    @staticmethod
    def _envelope_error_text(obj: dict) -> str:
        """Текст ошибки из конверта `error`/`turn.failed` (структура у codex разная)."""
        for key in ("error", "message"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                for inner in ("message", "text"):
                    nested = value.get(inner)
                    if isinstance(nested, str) and nested.strip():
                        return nested.strip()
        return ""

    @staticmethod
    def _reasoning_text(item: dict) -> str:
        """Текст рассуждения: codex кладёт его в `text`, иногда в список `summary`."""
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            return text
        summary = item.get("summary")
        if isinstance(summary, list):
            parts = [str(part) for part in summary if isinstance(part, str) and part.strip()]
            return "\n".join(parts)
        if isinstance(summary, str) and summary.strip():
            return summary
        return ""

    @staticmethod
    def _file_change_events(item: dict) -> Iterator[dict]:
        """События об изменённых файлах: по одному на путь, если путей несколько."""
        changes = item.get("changes")
        if isinstance(changes, list) and changes:
            for change in changes:
                if not isinstance(change, dict):
                    continue
                path = change.get("path") or change.get("file")
                if not isinstance(path, str) or not path:
                    continue
                text = change.get("kind") or change.get("text") or change.get("description") or ""
                yield {"kind": "file_change", "path": path, "text": str(text)}
            return
        path = item.get("path") or item.get("file")
        if isinstance(path, str) and path:
            text = item.get("text") or item.get("kind") or ""
            yield {"kind": "file_change", "path": path, "text": str(text)}
            return
        yield {"kind": "raw", "payload": item}


#: Признаки временного сообщения: провайдер сам сообщает о повторе и продолжает работу.
_TRANSIENT_MARKERS = ("reconnecting", "retrying", "stream disconnected", "connection reset")


def _is_transient_notice(text: str) -> bool:
    """Временное ли это сообщение (провайдер повторяет попытку), а не провал хода.

    Наблюдение 2026-10-12: codex-cli 0.162.1 при обрыве потока печатает конвертами `error`
    «Reconnecting... 2/5 (stream disconnected before completion: Connection reset by peer)»,
    после чего успешно завершает ход и отдаёт ответ. Считать это провалом нельзя.
    """
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)
