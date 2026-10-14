"""Выбор адаптера по агенту и разрешение каталогов.

Задача модуля — отвечать на два вопроса: «чем этот агент будет работать» и «где его воркспейс».
Реестр не падает, если адаптер ещё не написан: он отдаёт честную недоступность с причиной.
"""

from __future__ import annotations

import os
from pathlib import Path

from .model import Agent, Authorization, Availability, ProviderUnavailable
from .workspace import agent_workspace_path

__all__ = ["AdapterRegistry", "default_registry", "resolve_workspace_path"]

#: Провайдеры, о которых ядро знает по имени. Недоступный провайдер — не ошибка пакета.
KNOWN_PROVIDERS = ("codex", "opencode")


class _UnavailableAdapter:
    """Заглушка для провайдера, адаптера которого нет в сборке."""

    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        self._reason = reason

    def availability(self) -> Availability:
        return Availability(provider=self.provider, available=False, reason=self._reason)

    def authorization(self) -> Authorization:
        return Authorization(provider=self.provider, authorized=None, detail=self._reason, how="")

    def start(self, *args: object, **kwargs: object):
        raise ProviderUnavailable(self.provider, self._reason)
        yield  # pragma: no cover — генератор по контракту

    def resume(self, *args: object, **kwargs: object):
        raise ProviderUnavailable(self.provider, self._reason)
        yield  # pragma: no cover


class AdapterRegistry:
    """Реестр адаптеров: провайдер → объект с методами availability/start/resume."""

    def __init__(self) -> None:
        self._adapters: dict[str, object] = {}

    def register(self, adapter: object) -> None:
        provider = getattr(adapter, "provider", "")
        if not provider:
            raise ValueError("адаптер без имени провайдера")
        self._adapters[str(provider)] = adapter

    def providers(self) -> list[str]:
        return sorted(self._adapters)

    def get(self, provider: str) -> object:
        """Адаптер провайдера; для незарегистрированного — честная заглушка."""
        key = (provider or "").strip()
        if key in self._adapters:
            return self._adapters[key]
        reason = (
            f"адаптер {key or 'без имени'} не входит в эту сборку"
            if key
            else "у агента не указан провайдер"
        )
        if key not in KNOWN_PROVIDERS and key:
            reason = f"неизвестный провайдер: {key}"
        return _UnavailableAdapter(key, reason)

    def availability(self) -> list[Availability]:
        """Доступность всех известных провайдеров, включая не входящие в сборку."""
        result: list[Availability] = []
        names = sorted(set(KNOWN_PROVIDERS) | set(self._adapters))
        for name in names:
            adapter = self.get(name)
            try:
                result.append(adapter.availability())  # type: ignore[attr-defined]
            except Exception as error:  # адаптер не должен ронять панель состояний
                result.append(
                    Availability(
                        provider=name,
                        available=False,
                        reason=f"ошибка проверки доступности: {type(error).__name__}",
                    )
                )
        return result

    def authorization(self) -> list[Authorization]:
        """Авторизация всех известных провайдеров; сбой адаптера — честное «неизвестно»."""
        result: list[Authorization] = []
        names = sorted(set(KNOWN_PROVIDERS) | set(self._adapters))
        for name in names:
            adapter = self.get(name)
            method = getattr(adapter, "authorization", None)
            if method is None:
                result.append(
                    Authorization(
                        provider=name,
                        authorized=None,
                        detail="адаптер не сообщает состояние авторизации",
                    )
                )
                continue
            try:
                result.append(method())
            except Exception as error:  # адаптер не должен ронять панель состояний
                result.append(
                    Authorization(
                        provider=name,
                        authorized=None,
                        detail=f"ошибка проверки авторизации: {type(error).__name__}",
                    )
                )
        return result


def default_registry() -> AdapterRegistry:
    """Собрать реестр из адаптеров, которые есть в этой сборке.

    Порядок поиска:
    1. `RUNNEOS_AGENTS_ADAPTER_PATH` — путь к python-файлу с `build_registry()`;
       это точка расширения для тестов и будущих внешних адаптеров.
    2. Штатный пакет `runneos_agents.adapters`.
    Если адаптеров нет, реестр пуст и все провайдеры честно недоступны.
    """
    override = os.environ.get("RUNNEOS_AGENTS_ADAPTER_PATH", "").strip()
    if override:
        registry = _registry_from_file(override)
        if registry is not None:
            return registry

    registry = AdapterRegistry()
    try:
        from .adapters import build_registry  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        return registry  # адаптеры ещё не написаны — все провайдеры честно недоступны
    try:
        built = build_registry()
    except Exception:
        return registry
    if isinstance(built, AdapterRegistry):
        return built
    for adapter in built or []:
        registry.register(adapter)
    return registry


def _registry_from_file(path: str) -> AdapterRegistry | None:
    """Загрузить внешний набор адаптеров. Ошибки не скрываются, но и не роняют запуск."""
    import importlib.util

    target = Path(path).expanduser()
    if not target.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("runneos_agents_external_adapters", target)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        return None
    build = getattr(module, "build_registry", None)
    if build is None:
        return None
    try:
        built = build()
    except Exception:
        return None
    if isinstance(built, AdapterRegistry):
        return built
    registry = AdapterRegistry()
    for adapter in built or []:
        registry.register(adapter)
    return registry


def resolve_workspace_path(agent: Agent, root: Path) -> Path:
    """Каталог, в котором работает агент: явный из агента либо вычисленный из корня."""
    return Path(agent.workspace) if agent.workspace else agent_workspace_path(root, agent.id)
