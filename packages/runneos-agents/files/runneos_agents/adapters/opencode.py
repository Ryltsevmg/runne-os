"""Адаптер opencode CLI (opencode v2, команда `opencode run --format json`).

Измерено живьём 2026-10-12 на opencode v2.0.26 (строки сохранены в
`adapters/fixtures/opencode_*.jsonl`):

* `opencode run --standalone --format json '<prompt>'` печатает NDJSON в stdout;
* `--session <sessionID>` продолжает сессию: идентификатор приходит в поле `sessionID`
  каждой строки, поэтому его видно и на первом, и на последующих ходах;
* `--` перед текстом задачи обязателен как разделитель: без него текст с ведущим дефисом
  разбирается как флаг (поведение то же, что у codex);
* формы строк (подтверждены живым прогоном):
  `step_start` → `step_start` → `tool_use` (part.type=tool, state.input/output) →
  `step_finish` (part.tokens) → … → `text` (part.type=text, part.text);
  сбой → `{"type":"error","error":{"type":"provider.no-route","message":"…"}}` и код 1;
* `step_finish.part.tokens` = `{"input":…,"output":…,"reasoning":…,"cache":{"read":…,"write":…}}`
  приходит по шагам. Чтобы итог хода не показывал только последний шаг, токены копятся в
  контексте прогона и отдаются одним событием `usage` в конце (ключи — как у codex:
  `input_tokens`, `output_tokens`, `cached_input_tokens`, …).

Ограничение v1: адаптер запускает opencode без автоодобрения прав. Флаг `--auto` в справке
есть, но живым прогоном не проверялся, поэтому не используется: задача, которой нужно
подтверждение, не завершится и упрётся в таймаут — это честное поведение, а не выдуманное.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .base import CliJsonlAdapter, RunContext

__all__ = ["OpencodeAdapter"]

#: Соответствие полей токенов opencode ключам, которые понимает остальной пакет.
_TOKEN_KEYS = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("reasoning", "reasoning_output_tokens"),
)

#: Поля, откуда берём «цель» вызова инструмента для показа человеку.
_TARGET_KEYS = ("path", "command", "pattern", "query", "url", "file", "notebook")


class OpencodeAdapter(CliJsonlAdapter):
    """opencode как внешний процесс: только подтверждённый неинтерактивный JSON-режим."""

    provider = "opencode"
    binary_name = "opencode"
    #: Проверено на opencode v2.0.26 (2026-10-13): `auth list` печатает по строке на
    #: интеграцию, у авторизованных — пометка «stored»; без входа строк нет.
    auth_args = ("auth", "list")
    auth_how = "opencode auth login"

    def data_dirs(self) -> list[Path]:
        """opencode хранит конфиг, данные, кэш и состояние в стандартных каталогах XDG."""
        home = Path.home()
        return [
            home / ".config" / "opencode",
            home / ".local" / "share" / "opencode",
            home / ".cache" / "opencode",
            home / ".local" / "state" / "opencode",
        ]

    def interpret_auth(self, returncode: int, stdout: str, stderr: str) -> tuple[bool | None, str]:
        """`opencode auth list`: есть строка с «stored» — интеграция авторизована."""
        if returncode != 0:
            text = " ".join((stderr or stdout or "").split())
            detail = text[:200] or f"пустой вывод, код {returncode}"
            return None, f"opencode auth list вернул код {returncode}: {detail}"
        lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
        stored = [line for line in lines if "stored" in line.lower()]
        if stored:
            return True, "авторизовано: " + "; ".join(line[:60] for line in stored[:3])
        if lines:
            return False, "авторизованных интеграций нет: " + "; ".join(line[:60] for line in lines[:3])
        return False, "авторизованных интеграций нет"

    def __init__(
        self,
        binary: str | None = None,
        *,
        standalone: bool = True,
        **kwargs: Any,
    ) -> None:
        """`standalone=True` — свой сервер на один прогон, без фоновой службы opencode."""
        super().__init__(binary, **kwargs)
        self.standalone = bool(standalone)

    # --- командная строка

    def build_argv(
        self,
        binary: str,
        workspace: Path,
        prompt: str,
        *,
        model: str | None,
        provider_session_id: str | None,
    ) -> list[str]:
        """Командная строка `opencode run` (рабочий каталог задаётся через cwd).

        У `opencode run` нет аргумента каталога, поэтому воркспейс передаётся только `cwd`.
        """
        argv = [binary, "run"]
        if self.standalone:
            argv.append("--standalone")
        argv += ["--format", "json"]
        if model:
            argv += ["--model", str(model)]
        if provider_session_id:
            argv += ["--session", str(provider_session_id)]
        argv += ["--", prompt]
        return argv

    # --- разбор строк

    def extract_session_id(self, obj: dict) -> str | None:
        """opencode кладёт идентификатор сессии в `sessionID` каждой строки."""
        session_id = obj.get("sessionID")
        return str(session_id) if session_id else None

    def normalize(self, obj: dict, ctx: RunContext) -> Iterator[dict]:
        """Разобрать строку NDJSON opencode в нормализованные события."""
        envelope = str(obj.get("type") or "")
        part = obj.get("part") if isinstance(obj.get("part"), dict) else {}
        part_type = str(part.get("type") or "")

        if envelope == "text" or part_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text:
                yield {"kind": "agent_message", "text": text}
                return
            yield {"kind": "raw", "payload": obj}
            return

        if envelope == "error":
            info = obj.get("error") if isinstance(obj.get("error"), dict) else {}
            message = info.get("message") or info.get("type") or obj.get("message")
            if isinstance(message, str) and message.strip():
                yield {"kind": "error", "text": message.strip()}
                return
            yield {"kind": "raw", "payload": obj}
            return

        if envelope in ("reasoning", "thinking") or part_type in ("reasoning", "thinking"):
            text = part.get("text") if isinstance(part.get("text"), str) else ""
            if text:
                yield {"kind": "reasoning", "text": text}
                return
            yield {"kind": "raw", "payload": obj}
            return

        if envelope in ("tool_use", "tool") or part_type == "tool":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = state.get("status")
            # Незавершённый вызов не показываем: тот же вызов придёт completed с результатом.
            if status not in (None, "completed", "success"):
                return
            tool = str(part.get("tool") or part.get("name") or "tool")
            target = self._tool_target(state)
            yield {"kind": "command", "text": f"{tool} {target}".strip()}
            return

        if envelope == "step_finish":
            # Шаг сворачивается в итоговый usage прогона; отдельного вида для него нет.
            ctx.add_usage(self._tokens_to_usage(self._token_source(obj, part)))
            return

        # step_start и всё, чего мы ещё не видели, сохраняем как есть.
        yield {"kind": "raw", "payload": obj}

    # --- разбор частей

    @staticmethod
    def _token_source(obj: dict, part: dict) -> dict:
        """Где искать токены: у step_finish это `part.tokens`, иначе `usage` строки."""
        tokens = part.get("tokens")
        if isinstance(tokens, dict):
            return tokens
        usage = obj.get("usage")
        return usage if isinstance(usage, dict) else {}

    @staticmethod
    def _tokens_to_usage(tokens: dict) -> dict[str, int] | None:
        """Поля токенов opencode → ключи, общие для всех провайдеров пакета."""
        usage: dict[str, int] = {}
        for source, target in _TOKEN_KEYS:
            value = tokens.get(source)
            if isinstance(value, int) and not isinstance(value, bool):
                usage[target] = value
        cache = tokens.get("cache")
        if isinstance(cache, dict):
            for source, target in (("read", "cached_input_tokens"), ("write", "cache_write_input_tokens")):
                value = cache.get(source)
                if isinstance(value, int) and not isinstance(value, bool):
                    usage[target] = value
        return usage or None

    @staticmethod
    def _tool_target(state: dict) -> str:
        """Короткое описание того, над чем работал инструмент (для показа)."""
        payload = state.get("input") if isinstance(state.get("input"), dict) else {}
        for key in _TARGET_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        title = state.get("title")
        return str(title) if isinstance(title, str) else ""
