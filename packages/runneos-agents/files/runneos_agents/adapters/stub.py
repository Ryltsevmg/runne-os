"""Фиктивный адаптер: детерминированные события без процессов, сети и таймеров провайдера.

Нужен тестам ядра и панели: оркестратор должен проверяться без живого codex.

    from runneos_agents.adapters.stub import StubAdapter

    adapter = StubAdapter(reply="готово", provider_session_id="stub-1")
    registry = adapter.as_registry()          # AdapterRegistry с одним провайдером
    Orchestrator(store, adapters=registry).ask("tester", "привет")

Что умеет: `available=False` (старт бросает ProviderUnavailable), `error` (событие `error`
и `ok=False` у оркестратора), свой `events` (полностью свой сценарий), `delay` (медленный
провайдер для проверок потоков UI) и `calls` — журнал вызовов с аргументами.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from ..model import Authorization, Availability, ProviderUnavailable
from .base import Adapter

__all__ = ["StubAdapter"]


class StubAdapter:
    """Адаптер-заглушка, реализующий протокол Adapter."""

    def __init__(
        self,
        provider: str = "stub",
        *,
        available: bool = True,
        reason: str = "",
        version: str | None = "stub-0",
        provider_session_id: str = "stub-session-1",
        reply: str | None = None,
        events: Sequence[dict] | None = None,
        error: str | None = None,
        usage: dict | None = None,
        exit_code: int = 0,
        delay: float = 0.0,
        authorized: bool | None = True,
        auth_detail: str = "",
    ) -> None:
        """`events` заменяет весь сценарий целиком; `reply=None` — ответ «заглушка: <задача>».

        `authorized=None` — заглушка честно «не знает» про авторизацию; `auth_detail` —
        причина, которую отдаёт `authorization()`.
        """
        self.provider = str(provider or "stub")
        self.available_flag = bool(available)
        self.reason = str(reason or "")
        self.version = version
        self.provider_session_id = str(provider_session_id or "")
        self.reply = reply
        self.events = [dict(event) for event in events] if events is not None else None
        self.error = str(error) if error else None
        self.usage = dict(usage) if usage else None
        self.exit_code = int(exit_code)
        self.delay = max(float(delay or 0.0), 0.0)
        self.authorized_flag = authorized
        self.auth_detail = str(auth_detail or "")
        #: Журнал вызовов: метод, воркспейс, задача, модель, инструкции, таймаут.
        self.calls: list[dict] = []

    # --- контракт адаптера

    def availability(self) -> Availability:
        reason = self.reason
        if not self.available_flag and not reason:
            reason = "заглушка недоступна"
        return Availability(
            provider=self.provider,
            available=self.available_flag,
            reason=reason,
            version=self.version,
            binary=None,
        )

    def authorization(self) -> Authorization:
        return Authorization(
            provider=self.provider,
            authorized=self.authorized_flag,
            detail=self.auth_detail or "состояние заглушки",
            how=f"{self.provider} login",
        )

    def data_dirs(self) -> list[Path]:
        return []

    def start(
        self,
        ws: Path,
        prompt: str,
        *,
        model: str | None = None,
        instructions: str = "",
        timeout: float | None = None,
        sandbox_prefix=None,
    ) -> Iterator[dict]:
        """Начать ход-заглушку. Недоступная заглушка бросает ProviderUnavailable сразу."""
        self._record(
            "start",
            ws,
            prompt,
            model=model,
            instructions=instructions,
            timeout=timeout,
            sandbox_prefix=list(sandbox_prefix) if sandbox_prefix else None,
        )
        self._require_available()
        return self._iterate(self._scenario(prompt, self.provider_session_id, resuming=False))

    def resume(
        self,
        ws: Path,
        provider_session_id: str,
        prompt: str,
        *,
        model: str | None = None,
        timeout: float | None = None,
        sandbox_prefix=None,
    ) -> Iterator[dict]:
        """Продолжить ход-заглушку."""
        self._record(
            "resume",
            ws,
            prompt,
            model=model,
            instructions="",
            timeout=timeout,
            provider_session_id=provider_session_id,
            sandbox_prefix=list(sandbox_prefix) if sandbox_prefix else None,
        )
        self._require_available()
        session_id = str(provider_session_id or self.provider_session_id)
        return self._iterate(self._scenario(prompt, session_id, resuming=True))

    # --- удобства для тестов

    def as_registry(self):
        """Реестр ядра с одним этим адаптером (импорт отложен, чтобы не плодить циклы)."""
        from ..routing import AdapterRegistry

        registry = AdapterRegistry()
        registry.register(self)
        return registry

    @property
    def last_call(self) -> dict | None:
        """Последний вызов start/resume или None."""
        return self.calls[-1] if self.calls else None

    # --- внутреннее

    def _require_available(self) -> None:
        if not self.available_flag:
            raise ProviderUnavailable(self.provider, self.reason or "заглушка недоступна")

    def _record(
        self,
        method: str,
        ws: Path,
        prompt: str,
        *,
        model: str | None,
        instructions: str,
        timeout: float | None,
        provider_session_id: str | None = None,
        sandbox_prefix: list[str] | None = None,
    ) -> None:
        self.calls.append(
            {
                "method": method,
                "ws": Path(ws),
                "prompt": str(prompt),
                "model": model,
                "instructions": instructions,
                "timeout": timeout,
                "provider_session_id": provider_session_id,
                "sandbox_prefix": sandbox_prefix,
            }
        )

    def _scenario(self, prompt: str, session_id: str, *, resuming: bool) -> list[dict]:
        """Список событий хода: либо заданный явно, либо собранный по умолчанию."""
        if self.events is not None:
            return [dict(event) for event in self.events]
        result: list[dict] = []
        # При resume идентификатор сессии уже известен вызывающему — не дублируем.
        if session_id and not resuming:
            result.append({"kind": "provider_session", "provider_session_id": session_id})
        result.append({"kind": "turn.started"})
        if self.error:
            result.append({"kind": "error", "text": self.error})
        text = self.reply if self.reply is not None else f"заглушка: {prompt}"
        result.append({"kind": "agent_message", "text": text})
        if self.usage:
            result.append({"kind": "usage", "usage": dict(self.usage)})
        result.append({"kind": "turn.completed", "exit_code": self.exit_code})
        return result

    def _iterate(self, events: Sequence[dict]) -> Iterator[dict]:
        """Отдать события, выдерживая заданную задержку между ними."""
        for event in events:
            if self.delay:
                time.sleep(self.delay)
            yield dict(event)
