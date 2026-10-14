"""Локальный API (v1.9): построчный JSON поверх unix-сокета — для панели и внешних клиентов.

Оптический и по инварианту: запускается только вручную (`runneos-agents serve`),
ничего само ядро не поднимает. Сокет лежит в `$XDG_RUNTIME_DIR/runneos/` (каталог
0700, сокет 0600) — доступен только пользователю-владельцу; сетевых слушателей нет.

Протокол: один запрос — одна строка JSON, один ответ — одна строка JSON.
Операции: `ping`, `status`, `providers`, `auth-status`, `agents`, `sessions`,
`ask` (блокирующий ход через все ворота), `log`. Неизвестная операция и битый
JSON — честный `{"ok": false, "error": …}` без трассировки.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

from .model import now_iso
from .orchestrator import Orchestrator
from .routing import resolve_workspace_path
from .store import Store

__all__ = ["AgentApi", "default_socket_path", "serve"]


def _version() -> str:
    """Версия пакета лениво: api импортируется из __init__ до присвоения __version__."""
    from . import __version__

    return __version__

#: Таймаут ожидания нового соединения: за это время проверяется stop_event.
ACCEPT_TIMEOUT = 1.0


def default_socket_path() -> Path:
    """Сокет по умолчанию: `$XDG_RUNTIME_DIR/runneos/agents.sock`."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "runneos" / "agents.sock"


class AgentApi:
    """Диспетчер запросов. `handle` чистый по отношению к сети — проверяется тестами."""

    def __init__(self, store_factory, adapters=None, db_path=None) -> None:
        """`db_path` — путь или callable с путём базы: чтобы чтение не создавало данные
        (Store при открытии создаёт файл). None — считать, что база есть."""
        self._store_factory = store_factory
        self._adapters = adapters
        self._db_path = db_path
        self._ask_lock = threading.Lock()

    def _db_exists(self) -> bool:
        if self._db_path is None:
            return True
        path = self._db_path() if callable(self._db_path) else Path(self._db_path)
        return Path(path).exists()

    def _with_store(self, action, *, needs_existing_db: bool = False):
        if needs_existing_db and not self._db_exists():
            return None
        store = self._store_factory()
        try:
            return action(store)
        finally:
            try:
                store.close()
            except Exception:  # noqa: BLE001 — закрытие не должно ломать ответ
                pass

    def handle(self, request: object) -> dict:
        """Ответ на один разобранный запрос; никогда не бросает исключений."""
        if not isinstance(request, dict):
            return {"ok": False, "error": "запрос должен быть JSON-объектом"}
        operation = str(request.get("op") or "")
        try:
            return self._dispatch(operation, request)
        except Exception as error:  # noqa: BLE001 — наружу только честная причина
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}

    def _dispatch(self, operation: str, request: dict) -> dict:
        if operation == "ping":
            return {"ok": True, "pong": True, "version": _version()}
        if operation == "status":
            return self._with_store(self._status, needs_existing_db=True) or {
                "ok": True,
                "version": _version(),
                "counts": {},
            }
        if operation == "providers":
            return self._with_store(
                lambda store: {
                    "ok": True,
                    "providers": [_availability_row(item) for item in Orchestrator(store, self._adapters).providers()],
                }
            )
        if operation == "auth-status":
            return self._with_store(self._auth_status)
        if operation == "agents":
            return self._with_store(self._agents, needs_existing_db=True) or {"ok": True, "agents": []}
        if operation == "sessions":
            agent_id = str(request.get("agent") or "")
            limit = _positive_int(request.get("limit"), 20)
            return self._with_store(lambda store: self._sessions(store, agent_id, limit))
        if operation == "log":
            session_id = str(request.get("session") or "")
            limit = _positive_int(request.get("limit"), 50)
            return self._with_store(lambda store: self._log(store, session_id, limit))
        if operation == "task-add":
            agent_id = str(request.get("agent") or "")
            prompt = str(request.get("prompt") or "")
            return self._with_store(
                lambda store: {"ok": True, **_task_row(store.create_task(agent_id, prompt))},
                needs_existing_db=True,
            ) or {"ok": False, "error": "базы ещё нет (выполните runneos-agents init)"}
        if operation == "task-list":
            agent_id = request.get("agent")
            limit = _positive_int(request.get("limit"), 20)
            return self._with_store(
                lambda store: {
                    "ok": True,
                    "tasks": [_task_row(item) for item in store.list_tasks(
                        str(agent_id) if agent_id else None, limit)],
                },
                needs_existing_db=True,
            ) or {"ok": True, "tasks": []}
        if operation == "task-show":
            return self._with_store(
                lambda store: self._task_show(store, str(request.get("task") or "")),
                needs_existing_db=True,
            ) or {"ok": False, "error": "базы ещё нет"}
        if operation == "task-cancel":
            return self._with_store(
                lambda store: self._task_cancel(store, str(request.get("task") or "")),
                needs_existing_db=True,
            ) or {"ok": False, "error": "базы ещё нет"}
        if operation == "ask":
            return self._ask(request)
        return {"ok": False, "error": f"неизвестная операция: {operation or '(пусто)'}"}

    # --- операции

    def _status(self, store: Store) -> dict:
        counts = store.stats()["counts"]
        return {"ok": True, "version": _version(), "root": str(store.root), "counts": counts}

    def _auth_status(self, store: Store) -> dict:
        from .routing import default_registry  # локально: реестр не требует хранилища

        registry = self._adapters if self._adapters is not None else default_registry()
        authorizations = [
            {
                "provider": item.provider,
                "authorized": item.authorized,
                "detail": item.detail,
                "how": item.how,
            }
            for item in registry.authorization()
        ]
        return {"ok": True, "authorizations": authorizations}

    def _agents(self, store: Store) -> dict:
        rows = []
        for agent in store.list_agents():
            workspace = agent.workspace or resolve_workspace_path(agent, store.root)
            rows.append(
                {
                    "id": agent.id,
                    "name": agent.name,
                    "role": agent.role,
                    "provider": agent.provider,
                    "state": agent.state,
                    "workspace": str(workspace),
                    "created_at": agent.created_at,
                    "sessions": len(store.list_sessions(agent.id, limit=1000)),
                }
            )
        return {"ok": True, "agents": rows}

    def _sessions(self, store: Store, agent_id: str, limit: int) -> dict:
        if store.get_agent(agent_id) is None:
            return {"ok": False, "error": f"неизвестный агент: {agent_id}"}
        rows = []
        for session in store.list_sessions(agent_id, limit=limit):
            rows.append(
                {
                    "id": session.id,
                    "state": session.state,
                    "title": session.title,
                    "provider_session_id": session.provider_session_id,
                    "created_at": session.created_at,
                    "updated_at": session.updated_at,
                }
            )
        return {"ok": True, "sessions": rows}

    def _log(self, store: Store, session_id: str, limit: int) -> dict:
        if store.get_session(session_id) is None:
            return {"ok": False, "error": f"неизвестная сессия: {session_id}"}
        events = [
            {
                "seq": event.seq,
                "kind": event.kind,
                "created_at": event.created_at,
                "payload": event.payload,
            }
            for event in store.list_events(session_id, limit=limit)
        ]
        return {"ok": True, "events": events}

    def _task_show(self, store: Store, task_id: str) -> dict:
        task = store.get_task(task_id)
        if task is None:
            return {"ok": False, "error": f"неизвестная задача: {task_id}"}
        return {"ok": True, **_task_row(task)}

    def _task_cancel(self, store: Store, task_id: str) -> dict:
        task = store.get_task(task_id)
        if task is None:
            return {"ok": False, "error": f"неизвестная задача: {task_id}"}
        if task.state == "canceled":
            return {"ok": True, **_task_row(task)}  # идемпотентно: итог не трогаем
        try:
            task = store.set_task_state(task_id, "canceled", finished_at=now_iso())
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, **_task_row(task)}

    def _ask(self, request: dict) -> dict:
        agent_id = str(request.get("agent") or "")
        prompt = str(request.get("prompt") or "")
        if not agent_id or not prompt.strip():
            return {"ok": False, "error": "нужны agent и prompt"}
        with self._ask_lock:  # ходы сериализуемы: журнал монотонен, ворота честны
            return self._with_store(
                lambda store: _turn_row(
                    Orchestrator(store, self._adapters).ask(
                        agent_id,
                        prompt,
                        new_session=bool(request.get("new")),
                        model=request.get("model") or None,
                        timeout=_positive_float(request.get("timeout")),
                    )
                )
            )


def _turn_row(result) -> dict:
    return {
        "ok": result.ok,
        "session_id": result.session_id,
        "provider_session_id": result.provider_session_id,
        "text": result.text,
        "error": result.error,
        "usage": result.usage,
        "exit_code": result.exit_code,
    }


def _task_row(task) -> dict:
    return {
        "id": task.id,
        "agent_id": task.agent_id,
        "prompt": task.prompt,
        "state": task.state,
        "position": task.position,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "session_id": task.session_id,
        "error": task.error,
        "result": task.result,
    }


def _availability_row(item) -> dict:
    return {
        "provider": item.provider,
        "available": item.available,
        "reason": item.reason,
        "version": item.version,
    }


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def serve(
    socket_path: Path | str,
    store_factory,
    adapters=None,
    stop_event: threading.Event | None = None,
) -> None:
    """Поднять сокет и обслуживать запросы до stop_event или SIGINT.

    Старый сокетный файл на пути стирается (это признак прошлого запуска),
    права — 0600, каталог — 0700.
    """
    target = Path(socket_path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists():
        target.unlink()  # только сокетный файл прошлого запуска; иначе bind упал бы
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stop = stop_event or threading.Event()
    api = AgentApi(store_factory, adapters)
    try:
        # bind создаёт файл с umask-правами: между bind и chmod гонка (найдено
        # ночным таймером публикации 2026-10-14, mode&0o077==45). Устанавливаем
        # приватные права прямо на создание.
        old_umask = os.umask(0o077)
        try:
            server.bind(str(target))
        finally:
            os.umask(old_umask)
        os.chmod(target, 0o600)
        server.listen(8)
        server.settimeout(ACCEPT_TIMEOUT)
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            with connection:
                _serve_connection(connection, api)
    finally:
        server.close()
        try:
            target.unlink()
        except OSError:
            pass


def _serve_connection(connection: socket.socket, api: AgentApi) -> None:
    """Построчный цикл одного клиента; закрытие соединения — конец цикла."""
    file = connection.makefile("rw", encoding="utf-8", newline="\n")
    try:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError as error:
                response = {"ok": False, "error": f"запрос не разбирается: {error}"}
            else:
                response = api.handle(request)
            file.write(json.dumps(response, ensure_ascii=False) + "\n")
            file.flush()
    except (OSError, ValueError):
        pass  # клиент оборвал соединение — штатно
    finally:
        try:
            file.close()
        except OSError:
            pass
