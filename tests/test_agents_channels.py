"""Тесты межагентных каналов (v1.8): передача задачи — запись в журнал + явный --run.

Провайдеры подменяются двойниками; реальные CLI здесь не запускаются.

Запуск: PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_agents_channels.py' -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "runneos-agents" / "files"))

from runneos_agents.channels import delegate  # noqa: E402
from runneos_agents.model import Agent  # noqa: E402
from runneos_agents.orchestrator import Orchestrator  # noqa: E402
from runneos_agents.routing import AdapterRegistry  # noqa: E402
from runneos_agents.store import Store  # noqa: E402

sys.path.insert(0, str(REPO / "tests"))
from test_agents_orchestrator import FakeAdapter  # noqa: E402  — тот же двойник


class ChannelsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = Store(self.root / "agents.db")
        self.addCleanup(self.store.close)
        self.store.upsert_agent(Agent(id="tester", name="Отправитель"))
        self.store.upsert_agent(Agent(id="helper", name="Получатель", role="помощь"))
        self.registry = AdapterRegistry()
        self.adapter = FakeAdapter()
        self.registry.register(self.adapter)

    def orchestrator(self) -> Orchestrator:
        return Orchestrator(self.store, self.registry)

    def events(self, session_id, kind):
        return [event for event in self.store.list_events(session_id) if event.kind == kind]

    def messages(self, session_id):
        return self.store.list_messages(session_id)


class DelegateTests(ChannelsTestCase):
    def test_handoff_records_in_target_journal(self):
        result = delegate(
            self.store, self.orchestrator(),
            from_agent="tester", to_agent="helper", prompt="собери отчёт")
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.handoff_id)
        self.assertTrue(result.target_session_id)
        received = self.events(result.target_session_id, "task.received")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].payload["from"], "tester")
        self.assertEqual(received[0].payload["handoff_id"], result.handoff_id)
        roles = [(message.role, message.text) for message in self.messages(result.target_session_id)]
        self.assertEqual(roles[0], ("system", roles[0][1]))
        self.assertIn("tester", roles[0][1])
        self.assertIn("собери отчёт", roles[0][1])
        self.assertIsNone(result.turn, "без --run ход не выполняется")
        self.assertEqual(self.adapter.calls, [])

    def test_sender_gets_task_sent_trace_when_open_session_exists(self):
        sender_session = self.store.create_session("tester")
        result = delegate(
            self.store, self.orchestrator(),
            from_agent="tester", to_agent="helper", prompt="привет")
        self.assertTrue(result.ok, result.error)
        sent = self.events(sender_session.id, "task.sent")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].payload["to"], "helper")
        self.assertEqual(sent[0].payload["handoff_id"], result.handoff_id)

    def test_new_session_flag_forces_fresh_target_session(self):
        first = delegate(self.store, self.orchestrator(),
                         from_agent="tester", to_agent="helper", prompt="первая")
        second = delegate(self.store, self.orchestrator(),
                          from_agent="tester", to_agent="helper", prompt="вторая", new_session=True)
        self.assertNotEqual(first.target_session_id, second.target_session_id)
        again = delegate(self.store, self.orchestrator(),
                         from_agent="tester", to_agent="helper", prompt="третья")
        self.assertEqual(again.target_session_id, second.target_session_id,
                         "без --new передача продолжает открытую сессию получателя")

    def test_honest_refusals(self):
        cases = [
            ({"from_agent": "tester", "to_agent": "tester", "prompt": "x"}, "самому себе"),
            ({"from_agent": "нет", "to_agent": "helper", "prompt": "x"}, "отправитель"),
            ({"from_agent": "tester", "to_agent": "нет", "prompt": "x"}, "целевой агент"),
            ({"from_agent": "tester", "to_agent": "helper", "prompt": "  "}, "пустая задача"),
        ]
        for kwargs, fragment in cases:
            with self.subTest(fragment=fragment):
                result = delegate(self.store, self.orchestrator(), **kwargs)
                self.assertFalse(result.ok)
                self.assertIn(fragment, result.error)
                self.assertEqual(result.handoff_id, "")

    def test_run_executes_target_turn_through_the_gates(self):
        result = delegate(
            self.store, self.orchestrator(),
            from_agent="tester", to_agent="helper", prompt="ответь PONG", run=True)
        self.assertTrue(result.ok, result.error)
        self.assertIsNotNone(result.turn)
        self.assertEqual(result.turn.text, "PONG")
        # Ход получателя: события идут в его сессию передачи, провайдер вызван один раз.
        self.assertEqual(len(self.adapter.calls), 1)
        kinds = [event.kind for event in self.store.list_events(result.target_session_id)]
        self.assertIn("agent_message", kinds)
        self.assertIn("turn.finished", kinds)

    def test_target_policy_gates_the_delegated_turn(self):
        """Политика получателя действует на переданную задачу (это его ход)."""
        agent = self.store.get_agent("helper")
        from runneos_agents.workspace import ensure_workspace

        workspace = ensure_workspace(agent.replace(workspace=self.root / "workspaces" / "helper"), self.root)
        manifest_path = workspace.path / "agent.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["policy"] = {"enforce": True, "rules": [
            {"command": "codex", "allow": False, "name": "no-codex"}]}
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        result = delegate(
            self.store, self.orchestrator(),
            from_agent="tester", to_agent="helper", prompt="привет", run=True)
        self.assertFalse(result.ok)
        self.assertIn("no-codex", result.error)
        self.assertEqual(self.adapter.calls, [], "процесс не должен запускаться")
        self.assertEqual(len(self.events(result.target_session_id, "task.received")), 1,
                         "передача записана, даже если ход запрещён политикой")


class SendCliTests(ChannelsTestCase):
    """CLI `send`: коды возврата и JSON (адаптеры подменены через ADAPTER_PATH)."""

    def setUp(self):
        super().setUp()
        import os
        import subprocess
        import textwrap

        self.os = os
        self.subprocess = subprocess
        self.cli = REPO / "packages" / "runneos-agents" / "files" / "runneos-agents"
        self._tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp2.cleanup)
        adapters = Path(self._tmp2.name) / "fake_adapters.py"
        adapters.write_text(textwrap.dedent(
            '''
            from runneos_agents.model import Availability


            class FakeCodex:
                provider = "codex"

                def availability(self):
                    return Availability(provider="codex", available=True, version="fake-0.0")

                def authorization(self):
                    from runneos_agents.model import Authorization
                    return Authorization(provider="codex", authorized=True, detail="", how="codex login")

                def start(self, ws, prompt, *, model=None, instructions="", timeout=None, sandbox_prefix=None):
                    yield {"kind": "turn.started"}
                    yield {"kind": "provider_session", "provider_session_id": "fake-thread"}
                    yield {"kind": "agent_message", "text": "PONG: " + prompt}
                    yield {"kind": "turn.completed", "exit_code": 0}

                def resume(self, ws, provider_session_id, prompt, *, model=None, timeout=None, sandbox_prefix=None):
                    yield {"kind": "agent_message", "text": "продолжение"}
                    yield {"kind": "turn.completed", "exit_code": 0}


            def build_registry():
                from runneos_agents.routing import AdapterRegistry
                registry = AdapterRegistry()
                registry.register(FakeCodex())
                return registry
            '''), encoding="utf-8")
        self.env = dict(self.os.environ)
        # Отдельный корень для CLI-процессов: агентов создаёт сам CLI,
        # чтобы пути базы совпадали с теми, что видит subprocess.
        self.cli_root = Path(self._tmp2.name) / "data"
        self.env["XDG_DATA_HOME"] = str(self.cli_root)
        self.env["RUNNEOS_AGENTS_ADAPTER_PATH"] = str(adapters)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        for name, slug in (("Отправитель", "tester"), ("Получатель", "helper")):
            done = self.run_cli("agent", "add", name, "--id", slug)
            assert done.returncode == 0, done.stderr

    def run_cli(self, *args):
        return self.subprocess.run(
            [sys.executable, str(self.cli), *args],
            capture_output=True, text=True, env=self.env, timeout=120)

    def target_events(self, session_id):
        """Журнал из базы, которую видит CLI (не из shared-store теста)."""
        from runneos_agents.store import Store

        with Store(self.cli_root / "runneos" / "agents" / "agents.db") as store:
            return [event.kind for event in store.list_events(session_id)]

    def test_send_without_run_records_only(self):
        done = self.run_cli("send", "tester", "helper", "подготовь данные", "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout.splitlines()[0])
        self.assertTrue(payload["ok"])
        self.assertNotIn("turn", payload, "без --run хода нет")
        self.assertIn("task.received", self.target_events(payload["target_session_id"]))

    def test_send_with_run_executes_through_fake_provider(self):
        done = self.run_cli("send", "tester", "helper", "ответь PONG", "--run", "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout.splitlines()[0])
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["turn"]["ok"])
        self.assertIn("PONG", payload["turn"]["text"])
        self.assertIn("agent_message", self.target_events(payload["target_session_id"]))

    def test_send_unknown_target_is_error(self):
        done = self.run_cli("send", "tester", "нет", "привет")
        self.assertEqual(done.returncode, 1)
        self.assertIn("целевой агент", done.stderr)
        self.assertNotIn("Traceback", done.stderr)


if __name__ == "__main__":
    unittest.main()
