"""Адаптеры провайдеров RunneOS: реестр и общие типы.

Контракт: packages/runneos-agents/INTERFACE.md, раздел 5 (FROZEN v1).

Ядро (`runneos_agents.routing.default_registry`) зовёт `build_registry()` и принимает
либо `AdapterRegistry`, либо список адаптеров. Имена провайдеров здесь ровно те, что знает
ядро: `codex` и `opencode`.

Типы `Availability` и `ProviderUnavailable` живут в ядре (`runneos_agents.model`) и здесь
только реэкспортируются: у ядра и адаптеров должен быть один тип, а не две похожие копии.
"""

from __future__ import annotations

from ..model import Availability, ProviderUnavailable
from .base import (
    MASK,
    Adapter,
    CliJsonlAdapter,
    RunContext,
    collect_secrets,
    find_binary,
    redact,
    redact_argv,
    redact_object,
    stream_process,
    terminate_process,
)
from .codex import CodexAdapter
from .opencode import OpencodeAdapter
from .stub import StubAdapter

__all__ = [
    "Adapter",
    "Availability",
    "CliJsonlAdapter",
    "CodexAdapter",
    "MASK",
    "OpencodeAdapter",
    "ProviderUnavailable",
    "RunContext",
    "StubAdapter",
    "available",
    "build_registry",
    "collect_secrets",
    "find_binary",
    "get_adapter",
    "provider_names",
    "redact",
    "redact_argv",
    "redact_object",
    "stream_process",
    "terminate_process",
]

#: Адаптеры этой сборки: имя провайдера → фабрика.
ADAPTER_FACTORIES = {
    "codex": CodexAdapter,
    "opencode": OpencodeAdapter,
}


def provider_names() -> tuple[str, ...]:
    """Имена провайдеров, адаптеры которых есть в сборке."""
    return tuple(sorted(ADAPTER_FACTORIES))


def build_registry():
    """Собрать реестр адаптеров этой сборки (возвращает `AdapterRegistry` ядра).

    Импорт `routing` отложен: ядро само импортирует этот модуль, и встречный импорт на
    уровне модуля был бы лишним риском цикла.
    """
    from ..routing import AdapterRegistry

    registry = AdapterRegistry()
    for name in provider_names():
        registry.register(ADAPTER_FACTORIES[name]())
    return registry


def get_adapter(provider: str) -> Adapter:
    """Адаптер по имени провайдера; неизвестный провайдер — честный ProviderUnavailable."""
    key = str(provider or "").strip().lower()
    factory = ADAPTER_FACTORIES.get(key)
    if factory is None:
        raise ProviderUnavailable(provider or "", f"неизвестный провайдер: {provider or '<пусто>'}")
    return factory()


def available() -> list[Availability]:
    """Доступность всех провайдеров сборки; не бросает исключений."""
    result: list[Availability] = []
    for name in provider_names():
        try:
            result.append(ADAPTER_FACTORIES[name]().availability())
        except Exception as error:  # noqa: BLE001 — панель состояний не должна падать
            result.append(
                Availability(
                    provider=name,
                    available=False,
                    reason=f"ошибка проверки доступности: {type(error).__name__}",
                )
            )
    return result
