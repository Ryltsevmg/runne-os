"""runneos_agents — ядро локального «пульта агентов» RunneOS.

Пакет даёт: постоянных агентов с собственными воркспейсами, журнал сессий в SQLite
и запуск задач через уже установленные CLI-провайдеры (codex и другие).
Никакого облачного control plane и никаких собственных секретов: провайдер
пользуется своей уже настроенной подпиской.

Контракт и границы: packages/runneos-agents/INTERFACE.md.
"""

from __future__ import annotations

from .model import (
    Agent,
    Availability,
    Event,
    Message,
    ProviderUnavailable,
    Session,
    TurnResult,
    new_agent_id,
    now_iso,
)
from .orchestrator import Orchestrator, run_turn
from .routing import AdapterRegistry, default_registry, resolve_workspace_path
from .store import Store, default_db_path, default_root
from .workspace import Workspace, agent_workspace_path, ensure_workspace

__version__ = "0.1.0"

__all__ = [
    "AdapterRegistry",
    "Agent",
    "Availability",
    "Event",
    "Message",
    "Orchestrator",
    "ProviderUnavailable",
    "Session",
    "Store",
    "TurnResult",
    "Workspace",
    "__version__",
    "agent_workspace_path",
    "default_db_path",
    "default_registry",
    "default_root",
    "ensure_workspace",
    "new_agent_id",
    "now_iso",
    "resolve_workspace_path",
    "run_turn",
]
