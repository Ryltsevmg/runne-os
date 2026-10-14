"""Тесты локального API (v1.9): диспетчер и сокет-сервер без реальных провайдеров.

Запуск: PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_agents_api.py' -v
"""

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "runneos-agents" / "files"))
sys.path.insert(0, str(REPO / "tests"))

from runneos_agents.api import AgentApi, default_socket_path, serve  # noqa: E402
from runneos_agents.model import Agent  # noqa: E402
from runneos_agents.routing import AdapterRegistry  # noqa: E402
from runneos_agents.store import Store  # noqa: E402
from test_agents_orchestrator import FakeAdapter  # noqa: E402  — тот же двойник


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = Store(self.root / "agents.db")
        self.addCleanup(self.store.close)
        self.store.upsert_agent(Agent(id="tester", name="Тестер"))
        self.registry = AdapterRegistry()
        self.adapter = FakeAdapter()
        self.registry.register(self.adapter)
        self.db_path = self.root / "agents.db"
        self.api = AgentApi(lambda: Store(self.db_path), self.registry, db_path=lambda: self.db_path)


class DispatcherTests(ApiTestCase):
    def test_ping(self):
        answer = self.api.handle({"op": "ping"})
        self.assertTrue(answer["ok"])
        self.assertTrue(answer["pong"])

    def test_status_honest_without_data(self):
        # Корень без базы: статус честный (counts пуст), а не выдуманный ноль таблиц.
        empty = AgentApi(
            lambda: Store(self.root / "пусто" / "agents.db"),
            self.registry,
            db_path=lambda: self.root / "пусто" / "agents.db",
        )
        answer = empty.handle({"op": "status"})
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["counts"], {})
        agents = empty.handle({"op": "agents"})
        self.assertEqual(agents["agents"], [], "чтение не создаёт данные")

    def test_agents_and_ask(self):
        answer = self.api.handle({"op": "agents"})
        self.assertTrue(answer["ok"])
        self.assertEqual([row["id"] for row in answer["agents"]], ["tester"])
        turn = self.api.handle({"op": "ask", "agent": "tester", "prompt": "ответь PONG"})
        self.assertTrue(turn["ok"], turn["error"])
        self.assertEqual(turn["text"], "PONG")
        session_id = turn["session_id"]
        log = self.api.handle({"op": "log", "session": session_id})
        kinds = [event["kind"] for event in log["events"]]
        self.assertIn("policy.decision", kinds)
        self.assertIn("agent_message", kinds)

    def test_ask_honest_failures(self):
        empty = self.api.handle({"op": "ask", "agent": "tester", "prompt": "  "})
        self.assertFalse(empty["ok"])
        unknown = self.api.handle({"op": "ask", "agent": "нет", "prompt": "привет"})
        self.assertFalse(unknown["ok"])
        self.assertIn("неизвестный агент", unknown["error"])

    def test_unknown_op_and_bad_requests(self):
        self.assertFalse(self.api.handle({"op": "полететь"})["ok"])
        self.assertIn("неизвестная операция", self.api.handle({"op": ""})["error"])
        self.assertFalse(self.api.handle("не-объект")["ok"])
        self.assertFalse(self.api.handle(None)["ok"])

    def test_sessions_unknown_agent(self):
        answer = self.api.handle({"op": "sessions", "agent": "нет"})
        self.assertFalse(answer["ok"])

    def test_task_lifecycle_over_api(self):
        added = self.api.handle({"op": "task-add", "agent": "tester", "prompt": "ответь PONG"})
        self.assertTrue(added["ok"], added)
        self.assertEqual(added["state"], "queued")
        listed = self.api.handle({"op": "task-list"})
        self.assertEqual([row["id"] for row in listed["tasks"]], [added["id"]])
        canceled = self.api.handle({"op": "task-cancel", "task": added["id"]})
        self.assertTrue(canceled["ok"])
        self.assertEqual(canceled["state"], "canceled")
        again = self.api.handle({"op": "task-cancel", "task": added["id"]})
        self.assertTrue(again["ok"], "повторная отмена идемпотентна")
        self.assertEqual(again["finished_at"], canceled["finished_at"], "итог не переписывается")
        unknown = self.api.handle({"op": "task-show", "task": "task-нет"})
        self.assertFalse(unknown["ok"])


class SocketServeTests(ApiTestCase):
    """Полный цикл: поднять сокет, подключиться, спросить, погасить."""

    def test_serve_roundtrip_and_cleanup(self):
        socket_path = Path(self._tmp.name) / "runneos" / "agents.sock"
        stop = threading.Event()
        thread = threading.Thread(
            target=serve,
            args=(socket_path, lambda: Store(self.root / "agents.db"), self.registry, stop),
            daemon=True,
        )
        thread.start()
        # Ждём не существования, а приватных прав: между bind и chmod у файла
        # umask-права, и тест на существование ловил гонку (ночной таймер 2026-10-14).
        for _ in range(100):
            if socket_path.exists() and socket_path.stat().st_mode & 0o077 == 0:
                break
            threading.Event().wait(0.05)
        self.assertTrue(socket_path.exists(), "сокет должен появиться")
        self.assertEqual(socket_path.stat().st_mode & 0o077, 0, "сокет только для владельца")

        def ask_over_socket(payloads):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(10)
            client.connect(str(socket_path))
            file = client.makefile("rw", encoding="utf-8", newline="\n")
            answers = []
            for payload in payloads:
                line = payload if isinstance(payload, str) else json.dumps(payload)
                file.write(line + "\n")
                file.flush()
                answers.append(json.loads(file.readline()))
            file.close()
            client.close()
            return answers

        answers = ask_over_socket([
            {"op": "ping"},
            {"op": "ask", "agent": "tester", "prompt": "ответь PONG"},
            "{сломанный json",
            {"op": "нет такой"},
        ])
        self.assertTrue(answers[0]["pong"])
        self.assertTrue(answers[1]["ok"], answers[1])
        self.assertFalse(answers[2]["ok"], "битый JSON — честная ошибка")
        self.assertIn("не разбирается", answers[2]["error"])
        self.assertFalse(answers[3]["ok"])

        stop.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(socket_path.exists(), "сокет убирается при остановке")

    def test_default_socket_path_respects_runtime_dir(self):
        import os

        with mock_env(XDG_RUNTIME_DIR="/tmp/api-runtime-42"):
            self.assertEqual(default_socket_path(), Path("/tmp/api-runtime-42/runneos/agents.sock"))
        with mock_env(XDG_RUNTIME_DIR=None):
            path = default_socket_path()
        self.assertTrue(str(path).endswith("runneos/agents.sock"), path)


class mock_env:
    """Временно подменить переменную окружения (включая удаление при None)."""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, *_):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
