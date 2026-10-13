"""SQLite-хранилище агентов RunneOS: схема, миграции и журнал.

Контракт: packages/runneos-agents/INTERFACE.md, раздел 4 (FROZEN v1).
Журнал — источник правды: каждое событие сессии сначала пишется сюда, потом показывается.
Секреты в хранилище не попадают: сюда идёт только то, что вернул адаптер.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .model import (
    Agent,
    Event,
    Message,
    Session,
    new_session_id,
    now_iso,
    validate_agent_id,
)

__all__ = ["Store", "default_root", "default_db_path"]

#: Версия схемы. Увеличивается вместе с новой миграцией в _MIGRATIONS.
SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, list[str]] = {
    1: [
        """CREATE TABLE IF NOT EXISTS agents (
             id TEXT PRIMARY KEY,
             name TEXT NOT NULL,
             role TEXT NOT NULL DEFAULT '',
             provider TEXT NOT NULL DEFAULT 'codex',
             workspace TEXT,
             instructions TEXT NOT NULL DEFAULT '',
             state TEXT NOT NULL DEFAULT 'idle',
             created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS sessions (
             id TEXT PRIMARY KEY,
             agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
             title TEXT NOT NULL DEFAULT '',
             provider_session_id TEXT,
             state TEXT NOT NULL DEFAULT 'open',
             created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS messages (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
             role TEXT NOT NULL,
             text TEXT NOT NULL,
             created_at TEXT NOT NULL,
             event_id INTEGER)""",
        """CREATE TABLE IF NOT EXISTS events (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
             seq INTEGER NOT NULL,
             kind TEXT NOT NULL,
             payload TEXT NOT NULL,
             created_at TEXT NOT NULL)""",
        "CREATE UNIQUE INDEX IF NOT EXISTS events_session_seq ON events(session_id, seq)",
        "CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id)",
        "CREATE INDEX IF NOT EXISTS sessions_agent ON sessions(agent_id, updated_at)",
    ],
}


def default_root() -> Path:
    """Корень данных: RUNNEOS_AGENTS_HOME → XDG_DATA_HOME/runneos/agents → ~/.local/share/..."""
    explicit = os.environ.get("RUNNEOS_AGENTS_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "runneos" / "agents"


def default_db_path(root: Path | None = None) -> Path:
    """Путь к файлу базы внутри корня."""
    return (root or default_root()) / "agents.db"


class Store:
    """Хранилище агентов, сессий, сообщений и журнала событий.

    Использование: ``with Store() as store: ...`` либо явный ``close()``.
    Соединение одно на экземпляр; для потоков создавайте отдельный Store.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        if path is None:
            self.root = default_root()
            self.path = default_db_path(self.root)
        else:
            self.path = Path(path).expanduser()
            # Если передан каталог — база внутри него, как в default_db_path.
            if self.path.is_dir() or self.path.suffix == "":
                self.root = self.path
                self.path = default_db_path(self.root)
            else:
                self.root = self.path.parent
        self.root.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    # --- служебное -----------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def _write(self):
        """Транзакция записи с BEGIN IMMEDIATE: не теряет обновления при конкуренции.

        Возвращает контекстный менеджер транзакции. При вложенном вызове (например
        append_event внутри другой транзакции) отдаёт no-op, чтобы не ломать внешнюю.
        """
        if self._conn.in_transaction:
            return _null_transaction()
        return _immediate_transaction(self._conn)

    def _migrate(self) -> None:
        current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        for version in sorted(_MIGRATIONS):
            if version <= current:
                continue
            self._conn.execute("BEGIN IMMEDIATE")  # одна транзакция на миграцию
            try:
                for statement in _MIGRATIONS[version]:
                    self._conn.execute(statement)
                self._conn.execute(f"PRAGMA user_version={version}")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
            current = version

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:  # повторное закрытие не является ошибкой
            pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- агенты --------------------------------------------------------

    def upsert_agent(self, agent: Agent) -> Agent:
        """Создать или обновить агента; created_at существующего не меняется."""
        validate_agent_id(agent.id)
        row = self._conn.execute(
            "SELECT created_at FROM agents WHERE id = ?", (agent.id,)
        ).fetchone()
        created_at = row["created_at"] if row else (agent.created_at or now_iso())
        with self._write():
            self._conn.execute(
                """INSERT INTO agents (id, name, role, provider, workspace, instructions,
                                       state, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name,
                     role=excluded.role,
                     provider=excluded.provider,
                     workspace=excluded.workspace,
                     instructions=excluded.instructions,
                     state=excluded.state""",
                (
                    agent.id,
                    agent.name,
                    agent.role,
                    agent.provider,
                    str(agent.workspace) if agent.workspace else None,
                    agent.instructions,
                    agent.state,
                    created_at,
                ),
            )
        stored = self.get_agent(agent.id)
        assert stored is not None  # только что записали
        return stored

    def get_agent(self, agent_id: str) -> Agent | None:
        row = self._conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return _agent_from_row(row) if row else None

    def list_agents(self) -> list[Agent]:
        rows = self._conn.execute("SELECT * FROM agents ORDER BY name COLLATE NOCASE").fetchall()
        return [_agent_from_row(row) for row in rows]

    def remove_agent(self, agent_id: str) -> bool:
        """Удалить агента вместе с сессиями, сообщениями и журналом (каскад)."""
        with self._write():
            cursor = self._conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        return cursor.rowcount > 0

    def set_agent_state(self, agent_id: str, state: str) -> None:
        """Сменить состояние агента; неизвестное состояние — ошибка."""
        Agent(id=agent_id, name=agent_id, state=state)  # валидация состояния
        with self._write():
            self._conn.execute("UPDATE agents SET state = ? WHERE id = ?", (state, agent_id))

    # --- сессии --------------------------------------------------------

    def create_session(self, agent_id: str, title: str = "") -> Session:
        if self.get_agent(agent_id) is None:
            raise ValueError(f"неизвестный агент: {agent_id}")
        session = Session(
            id=new_session_id(),
            agent_id=agent_id,
            title=title,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        with self._write():
            self._conn.execute(
                """INSERT INTO sessions (id, agent_id, title, provider_session_id, state,
                                         created_at, updated_at)
                   VALUES (?, ?, ?, NULL, ?, ?, ?)""",
                (
                    session.id,
                    session.agent_id,
                    session.title,
                    session.state,
                    session.created_at,
                    session.updated_at,
                ),
            )
        return session

    def get_session(self, session_id: str) -> Session | None:
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return _session_from_row(row) if row else None

    def list_sessions(self, agent_id: str | None = None, limit: int = 50) -> list[Session]:
        limit = max(1, int(limit))
        if agent_id:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE agent_id = ? ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (agent_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_session_from_row(row) for row in rows]

    def latest_open_session(self, agent_id: str) -> Session | None:
        row = self._conn.execute(
            """SELECT * FROM sessions
               WHERE agent_id = ? AND state = 'open'
               ORDER BY updated_at DESC, rowid DESC LIMIT 1""",
            (agent_id,),
        ).fetchone()
        return _session_from_row(row) if row else None

    def set_provider_session_id(self, session_id: str, provider_session_id: str) -> None:
        with self._write():
            self._conn.execute(
                "UPDATE sessions SET provider_session_id = ?, updated_at = ? WHERE id = ?",
                (provider_session_id, now_iso(), session_id),
            )

    def set_session_state(self, session_id: str, state: str) -> None:
        Session(id=session_id, agent_id="", state=state)  # валидация состояния
        with self._write():
            self._conn.execute(
                "UPDATE sessions SET state = ?, updated_at = ? WHERE id = ?",
                (state, now_iso(), session_id),
            )

    def touch_session(self, session_id: str) -> None:
        with self._write():
            self._conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (now_iso(), session_id)
            )

    # --- сообщения и журнал -------------------------------------------

    def add_message(
        self, session_id: str, role: str, text: str, event_id: int | None = None
    ) -> Message:
        message = Message(
            session_id=session_id,
            role=role,
            text=text,
            created_at=now_iso(),
            event_id=event_id,
        )
        with self._write():
            cursor = self._conn.execute(
                """INSERT INTO messages (session_id, role, text, created_at, event_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (message.session_id, message.role, message.text, message.created_at, event_id),
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (message.created_at, session_id)
            )
        return Message(
            id=int(cursor.lastrowid),
            session_id=message.session_id,
            role=message.role,
            text=message.text,
            created_at=message.created_at,
            event_id=event_id,
        )

    def list_messages(self, session_id: str, limit: int = 200) -> list[Message]:
        limit = max(1, int(limit))
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [_message_from_row(row) for row in rows]

    def append_event(self, session_id: str, kind: str, payload: dict) -> Event:
        """Добавить событие в журнал. seq монотонен внутри сессии и уникален.

        Запись идёт в транзакции BEGIN IMMEDIATE: параллельные писатели не получат
        одинаковый seq и не потеряют событие.
        """
        event = Event(session_id=session_id, kind=kind, payload=payload or {}, created_at=now_iso())
        with self._write():
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS last FROM events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(row["last"]) + 1
            cursor = self._conn.execute(
                """INSERT INTO events (session_id, seq, kind, payload, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    session_id,
                    seq,
                    event.kind,
                    json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                    event.created_at,
                ),
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (event.created_at, session_id)
            )
        return Event(
            id=int(cursor.lastrowid),
            session_id=session_id,
            kind=event.kind,
            payload=event.payload,
            created_at=event.created_at,
            seq=seq,
        )

    def list_events(self, session_id: str, limit: int = 500) -> list[Event]:
        """Последние события сессии в порядке возрастания seq."""
        limit = max(1, int(limit))
        rows = self._conn.execute(
            """SELECT * FROM (
                 SELECT * FROM events WHERE session_id = ? ORDER BY seq DESC LIMIT ?
               ) ORDER BY seq ASC""",
            (session_id, limit),
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def events_after(self, session_id: str, seq: int, limit: int = 500) -> list[Event]:
        """События строго после указанного seq — для инкрементального чтения UI."""
        rows = self._conn.execute(
            "SELECT * FROM events WHERE session_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (session_id, int(seq), max(1, int(limit))),
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def stats(self) -> dict:
        """Сводка для doctor: размеры и последние временные метки."""
        counts = {}
        for table in ("agents", "sessions", "messages", "events"):
            counts[table] = int(
                self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            )
        last = self._conn.execute(
            "SELECT MAX(updated_at) AS last FROM sessions"
        ).fetchone()["last"]
        return {
            "schema_version": int(self._conn.execute("PRAGMA user_version").fetchone()[0]),
            "counts": counts,
            "last_activity": last,
            "db_path": str(self.path),
            "db_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }


# --- преобразование строк -------------------------------------------------


def _agent_from_row(row: sqlite3.Row) -> Agent:
    return Agent(
        id=row["id"],
        name=row["name"],
        role=row["role"],
        provider=row["provider"],
        workspace=Path(row["workspace"]) if row["workspace"] else None,
        created_at=row["created_at"],
        state=row["state"],
        instructions=row["instructions"],
    )


def _session_from_row(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        agent_id=row["agent_id"],
        title=row["title"],
        provider_session_id=row["provider_session_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        state=row["state"],
    )


def _message_from_row(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        session_id=row["session_id"],
        role=row["role"],
        text=row["text"],
        created_at=row["created_at"],
        event_id=row["event_id"],
    )


def _event_from_row(row: sqlite3.Row) -> Event:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {"unparsed": row["payload"]}
    return Event(
        id=row["id"],
        session_id=row["session_id"],
        seq=row["seq"],
        kind=row["kind"],
        payload=payload,
        created_at=row["created_at"],
    )


# --- транзакции -----------------------------------------------------------


class _ImediateTransaction:
    """Транзакция записи: BEGIN IMMEDIATE → COMMIT, при ошибке ROLLBACK."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if exc_type is None:
            self._conn.execute("COMMIT")
        else:
            self._conn.execute("ROLLBACK")
        return False


class _NullTransaction:
    """Пустая транзакция: используется, когда внешняя уже открыта."""

    def __init__(self) -> None:
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def _immediate_transaction(conn: sqlite3.Connection) -> _ImediateTransaction:
    return _ImediateTransaction(conn)


def _null_transaction() -> _NullTransaction:
    return _NullTransaction()
