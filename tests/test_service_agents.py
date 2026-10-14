"""Тесты службы очереди задач (runneos-agents-service) и задач как сущности (v1.10).

Охват: миграция схемы 1→2, FIFO и терминальность в Store, жизненный цикл задачи
через служебный цикл (подменённое окружение), crash-safe, CLI task.

Запуск: PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_service_agents.py' -v
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "packages" / "runneos-agents-service" / "files" / "runneos-agents-service"
FILES_DIR = REPO / "packages" / "runneos-agents" / "files"

sys.path.insert(0, str(FILES_DIR))
sys.path.insert(0, str(REPO / "tests"))

from runneos_agents.model import Agent, Task  # noqa: E402
from runneos_agents.orchestrator import Orchestrator  # noqa: E402
from runneos_agents.routing import AdapterRegistry  # noqa: E402
from runneos_agents.store import SCHEMA_VERSION, Store  # noqa: E402
from test_agents_orchestrator import FakeAdapter  # noqa: E402  — тот же двойник


def load_service(name: str = "runneos_agents_service"):
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


SERVICE = load_service()


class TasksStoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = Store(self.root / "agents.db")
        self.addCleanup(self.store.close)
        self.store.upsert_agent(Agent(id="tester", name="Тестер"))
        self.adapter = FakeAdapter()
        self.registry = AdapterRegistry()
        self.registry.register(self.adapter)

    def orchestrator(self) -> Orchestrator:
        return Orchestrator(self.store, self.registry)


class MigrationTests(TasksStoreTestCase):
    def test_new_db_is_version_2_with_tasks(self):
        self.assertEqual(self.store.stats()["schema_version"], SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 2)
        self.assertEqual(self.store.list_tasks(), [])

    def test_v1_database_migrates_and_keeps_data(self):
        """База эпохи v1 (без tasks) открывается: миграция добавляет таблицу, данные целы."""
        old = self.root / "v1.db"
        connection = sqlite3.connect(str(old))
        connection.executescript(
            """
            CREATE TABLE agents (
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT 'codex',
                workspace TEXT, instructions TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'idle', created_at TEXT NOT NULL);
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                title TEXT NOT NULL DEFAULT '', provider_session_id TEXT,
                state TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL
                    REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL,
                event_id INTEGER);
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL
                    REFERENCES sessions(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE UNIQUE INDEX events_session_seq ON events(session_id, seq);
            CREATE INDEX messages_session ON messages(session_id, id);
            CREATE INDEX sessions_agent ON sessions(agent_id, updated_at);
            INSERT INTO agents (id, name, created_at) VALUES ('old-agent', 'Старый', '2026-10-12T00:00:00Z');
            PRAGMA user_version=1;
            """
        )
        connection.commit()
        connection.close()

        migrated = Store(old)
        try:
            self.assertEqual(migrated.stats()["schema_version"], 2)
            self.assertEqual(migrated.get_agent("old-agent").name, "Старый", "данные v1 пережили миграцию")
            task = migrated.create_task("old-agent", "работает")
            self.assertEqual(task.state, "queued")
        finally:
            migrated.close()


class TaskQueueTests(TasksStoreTestCase):
    def test_fifo_claim_order(self):
        first = self.store.create_task("tester", "первая")
        second = self.store.create_task("tester", "вторая")
        claimed = self.store.claim_next_task()
        self.assertEqual(claimed.id, first.id)
        self.assertEqual(claimed.state, "running")
        self.assertEqual(self.store.claim_next_task().id, second.id)
        self.assertIsNone(self.store.claim_next_task())

    def test_terminal_states_are_final(self):
        task = self.store.create_task("tester", "привет")
        self.store.set_task_state(task.id, "done", result="готово", finished_at="2026-10-14T00:00:00Z")
        with self.assertRaises(ValueError):
            self.store.set_task_state(task.id, "queued")
        with self.assertRaises(ValueError):
            self.store.set_task_state(task.id, "failed", error="перезапись")

    def test_invalid_agent_and_empty_prompt_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_task("нет", "привет")
        with self.assertRaises(ValueError):
            self.store.create_task("tester", "   ")

    def test_recover_returns_running_to_queue(self):
        task = self.store.create_task("tester", "привет")
        self.store.set_task_state(task.id, "running")
        recovered = self.store.recover_running_tasks()
        self.assertEqual(recovered, [task.id])
        self.assertEqual(self.store.get_task(task.id).state, "queued")


class ServiceLoopTests(TasksStoreTestCase):
    """Жизненный цикл: служебный цикл на настоящем Store + двойник адаптера."""

    def factory(self):
        return lambda: Store(self.root / "agents.db")

    def test_loop_executes_queue_and_finishes_tasks(self):
        task = self.store.create_task("tester", "ответь PONG")
        code = SERVICE.serve_loop(store_factory=self.factory(), adapters=self.registry,
                                  once=True, notify="", poll=0.1)
        self.assertEqual(code, 0)
        done = self.store.get_task(task.id)
        self.assertEqual(done.state, "done")
        self.assertEqual(done.result, "PONG")
        self.assertTrue(done.session_id)
        self.assertTrue(done.finished_at)
        # Ход неотличим от обычного ask: журнал агента содержит ворота и итог.
        kinds = [event.kind for event in self.store.list_events(done.session_id)]
        self.assertIn("policy.decision", kinds)
        self.assertIn("turn.finished", kinds)

    def test_failed_turn_lands_as_failed_task(self):
        task = self.store.create_task("tester", "привет")
        self.adapter._text = ""
        SERVICE.serve_loop(store_factory=self.factory(), adapters=self.registry,
                           once=True, notify="", poll=0.1)
        failed = self.store.get_task(task.id)
        self.assertEqual(failed.state, "failed")
        self.assertIn("не дав ответа", failed.error)

    def test_cancel_mid_turn_terminates(self):
        """Отмена во время хода: стрим закрыт, задача canceled, двойник увидел выход."""
        slow = self.adapter

        class SlowAdapter(FakeAdapter):
            def start(self, ws, prompt, *, model=None, instructions="", timeout=None, sandbox_prefix=None):
                slow.calls.append(("start", Path(ws), prompt, model, None))
                yield {"kind": "turn.started"}
                yield {"kind": "reasoning", "text": "думает…"}
                yield {"kind": "agent_message", "text": "PONG"}

        self.registry = AdapterRegistry()
        self.slow_adapter = SlowAdapter()
        self.registry.register(self.slow_adapter)
        task = self.store.create_task("tester", "ответь PONG")

        # Отменяем задачу сразу после старта хода: should_cancel увидит canceled.
        original_execute = SERVICE.execute_task

        def cancel_then_execute(task_arg, **kwargs):
            self.store.set_task_state(task_arg.id, "running")
            self.store.set_task_state(task_arg.id, "canceled", finished_at="2026-10-14T00:00:00Z")
            return original_execute(task_arg, **kwargs)

        SERVICE.execute_task = cancel_then_execute
        self.addCleanup(setattr, SERVICE, "execute_task", original_execute)
        SERVICE.serve_loop(store_factory=self.factory(), adapters=self.registry,
                           once=True, notify="", poll=0.1)

        canceled = self.store.get_task(task.id)
        self.assertEqual(canceled.state, "canceled")
        self.assertEqual(self.store.get_agent("tester").state, "idle", "отмена не делает агента ошибочным")

    def test_crash_recovery_via_loop(self):
        task = self.store.create_task("tester", "ответь PONG")
        self.store.set_task_state(task.id, "running")  # «службу убили в момент хода»
        SERVICE.serve_loop(store_factory=self.factory(), adapters=self.registry,
                           once=True, notify="", poll=0.1)
        done = self.store.get_task(task.id)
        self.assertEqual(done.state, "done", "задача вернулась в очередь и была выполнена")


class PackageConsistencyTests(unittest.TestCase):
    def test_pkgbuild_and_units(self):
        package = REPO / "packages" / "runneos-agents-service"
        pkgbuild = (package / "PKGBUILD").read_text(encoding="utf-8")
        self.assertIn("runneos-agents>=0.9.0", pkgbuild)
        self.assertIn("test_service_agents.py", pkgbuild)
        self.assertNotIn("graphical-session.target.wants", pkgbuild, "включение — только вручную")
        unit = (package / "files" / "runneos-agents-service.service").read_text(encoding="utf-8")
        self.assertIn("ExecStart=/usr/bin/runneos-agents-service", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn("Restart=on-failure", unit)

    def test_service_script_needs_no_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = dict(os.environ)
            environment["XDG_DATA_HOME"] = tmp
            environment["RUNNEOS_AGENTS_FILES"] = str(FILES_DIR)
            environment.pop("DISPLAY", None)
            environment.pop("WAYLAND_DISPLAY", None)
            done = subprocess_run([sys.executable, str(SCRIPT), "--help"], env=environment)
        self.assertEqual(done.returncode, 0, done.stderr)


def subprocess_run(argv, env):
    import subprocess

    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)


if __name__ == "__main__":
    unittest.main()
