"""Модель данных агентов RunneOS: чистые структуры без ввода-вывода.

Контракт: packages/runneos-agents/INTERFACE.md, раздел 3 (FROZEN v1).
Правило проекта: неизвестное значение — None, а не 0 или пустая строка.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "AGENT_ID_RE",
    "AGENT_STATES",
    "Availability",
    "Agent",
    "Event",
    "Message",
    "ProviderUnavailable",
    "Session",
    "TurnResult",
    "new_agent_id",
    "new_session_id",
    "now_iso",
    "validate_agent_id",
]

#: Допустимые состояния агента (INTERFACE.md, раздел 3).
AGENT_STATES = ("idle", "running", "waiting_user", "error", "unavailable")

#: Допустимые состояния сессии.
SESSION_STATES = ("open", "closed", "error")

#: Роли сообщений.
MESSAGE_ROLES = ("user", "agent", "system")

AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: Провайдер по умолчанию, если пользователь не выбрал иной.
DEFAULT_PROVIDER = "codex"


def now_iso() -> str:
    """Текущее время в ISO-8601 UTC с суффиксом Z (секундная точность)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_session_id() -> str:
    """Внутренний идентификатор сессии: uuid4 без дефисов."""
    return uuid.uuid4().hex


def new_agent_id(name: str) -> str:
    """Слаг агента из произвольного имени.

    Латиница, цифры и дефисы; всё остальное — дефис; обрезка до 64 символов.
    Если слаг пуст — «agent».
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).strip().lower())
    slug = slug.strip("-")[:64].strip("-")
    if not slug or not AGENT_ID_RE.match(slug):
        # Кириллица и прочие непереводимые имена: стабильный слаг по хешу имени.
        import hashlib

        digest = hashlib.sha1(str(name).encode("utf-8")).hexdigest()[:8]
        slug = f"agent-{digest}"
    return slug


def validate_agent_id(agent_id: str) -> str:
    """Проверить идентификатор агента; вернуть его же или бросить ValueError."""
    if not AGENT_ID_RE.match(str(agent_id or "")):
        raise ValueError(
            "недопустимый идентификатор агента: ожидается [a-z0-9-], до 64 символов"
        )
    return agent_id


@dataclass(frozen=True)
class Agent:
    """Постоянный агент: имя, роль, провайдер и собственный воркспейс."""

    id: str
    name: str
    role: str = ""
    provider: str = DEFAULT_PROVIDER
    workspace: Path | None = None
    created_at: str = ""
    state: str = "idle"
    instructions: str = ""

    def __post_init__(self) -> None:
        validate_agent_id(self.id)
        if self.state not in AGENT_STATES:
            raise ValueError(f"недопустимое состояние агента: {self.state}")
        if not str(self.name).strip():
            raise ValueError("имя агента не может быть пустым")

    def replace(self, **changes: object) -> "Agent":
        """Копия агента с изменениями (created_at не меняется вызывающим по умолчанию)."""
        return replace(self, **changes)


@dataclass(frozen=True)
class Session:
    """Одна линия разговора с агентом; хранит идентификатор сессии у провайдера."""

    id: str
    agent_id: str
    title: str = ""
    provider_session_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    state: str = "open"

    def __post_init__(self) -> None:
        if self.state not in SESSION_STATES:
            raise ValueError(f"недопустимое состояние сессии: {self.state}")

    @property
    def resumable(self) -> bool:
        """Можно ли продолжать эту сессию у провайдера."""
        return bool(self.provider_session_id) and self.state == "open"


@dataclass(frozen=True)
class Message:
    """Сообщение в ленте сессии (то, что видит человек)."""

    session_id: str
    role: str
    text: str
    id: int | None = None
    created_at: str = ""
    event_id: int | None = None

    def __post_init__(self) -> None:
        if self.role not in MESSAGE_ROLES:
            raise ValueError(f"недопустимая роль сообщения: {self.role}")


@dataclass(frozen=True)
class Event:
    """Событие журнала: сначала запись, потом показ."""

    session_id: str
    kind: str
    payload: dict = field(default_factory=dict)
    id: int | None = None
    created_at: str = ""
    seq: int = 0

    def __post_init__(self) -> None:
        if not str(self.kind).strip():
            raise ValueError("вид события не может быть пустым")


@dataclass(frozen=True)
class TurnResult:
    """Итог одного хода: то, что возвращает Orchestrator.ask."""

    ok: bool
    session_id: str
    provider_session_id: str | None = None
    text: str = ""
    error: str | None = None
    usage: dict | None = None
    exit_code: int | None = None

    @property
    def failed(self) -> bool:
        return not self.ok


@dataclass(frozen=True)
class Availability:
    """Доступность провайдера: честное состояние вместо выдуманного ответа."""

    provider: str
    available: bool
    reason: str = ""
    version: str | None = None
    binary: str | None = None


class ProviderUnavailable(RuntimeError):
    """Провайдер недоступен: бинаря нет, режим не подтверждён или запуск невозможен.

    Живёт в model.py, потому что это общий контракт ядра и адаптеров: адаптеры бросают,
    оркестратор ловит и превращает в честный TurnResult(ok=False).
    """

    def __init__(self, provider: str, reason: str = "") -> None:
        self.provider = provider
        self.reason = reason or f"провайдер {provider} недоступен"
        super().__init__(self.reason)
