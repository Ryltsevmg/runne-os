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
    Authorization,
    Availability,
    Event,
    Message,
    ProviderUnavailable,
    Session,
    TurnResult,
    new_agent_id,
    now_iso,
)
from .api import AgentApi, default_socket_path, serve
from .channels import HandoffResult, delegate
from .orchestrator import Orchestrator, run_turn
from .policy import (
    Decision,
    LoadedPolicy,
    Policy,
    Rule,
    load_policy,
    read_section,
    write_section,
)
from .routing import AdapterRegistry, default_registry, resolve_workspace_path
from .sandbox import LoadedSandbox, SandboxLimits, load_sandbox
from .store import Store, default_db_path, default_root
from .workspace import Workspace, agent_workspace_path, ensure_workspace

__version__ = "0.9.0"

__all__ = [
    "AdapterRegistry",
    "Agent",
    "AgentApi",
    "Authorization",
    "Availability",
    "Decision",
    "Event",
    "HandoffResult",
    "LoadedPolicy",
    "LoadedSandbox",
    "Message",
    "Orchestrator",
    "Policy",
    "ProviderUnavailable",
    "Rule",
    "SandboxLimits",
    "Session",
    "Store",
    "TurnResult",
    "Workspace",
    "__version__",
    "agent_workspace_path",
    "default_db_path",
    "default_registry",
    "default_root",
    "default_socket_path",
    "delegate",
    "ensure_workspace",
    "load_policy",
    "load_sandbox",
    "new_agent_id",
    "now_iso",
    "read_section",
    "resolve_workspace_path",
    "run_turn",
    "serve",
    "write_section",
]
