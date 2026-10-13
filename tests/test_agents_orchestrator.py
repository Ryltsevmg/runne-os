"""Тесты оркестратора: ход агента, файловый журнал прогона, честные отказы.

Провайдер подменяется фейковым адаптером: реальные CLI здесь не запускаются.

Запуск: PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_agents_*.py' -v
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "runneos-agents" / "files"))

from runneos_agents.model import Agent, Availability, ProviderUnavailable  # noqa: E402
from runneos_agents.orchestrator import Orchestrator, RunLog  # noqa: E402
from runneos_agents.routing import AdapterRegistry  # noqa: E402
from runneos_agents.store import Store  # noqa: E402
from runneos_agents.workspace import ensure_workspace  # noqa: E402


class FakeAdapter:
    """Адаптер-двойник: помнит, как его позвали, и отдаёт предсказуемые события."""

    provider = "codex"

    def __init__(
        self,
        *,
        available: bool = True,
        text: str = "PONG",
        fail: bool = False,
        transient: int = 0,
        exit_code: int = 0,
    ):
        self._available = available
        self._text = text
        self._fail = fail
        self._transient = transient
        self._exit_code = exit_code
        self.calls: list[tuple] = []

    def availability(self):
        return Availability(
            provider=self.provider,
            available=self._available,
            reason="" if self._available else "тестовый провайдер недоступен",
            version="fake-1.0",
        )

    def start(self, ws, prompt, *, model=None, instructions="", timeout=None):
        self.calls.append(("start", Path(ws), prompt, model))
        if self._fail:
            raise ProviderUnavailable(self.provider, "запуск не удался")
        yield {"kind": "turn.started"}
        yield {"kind": "provider_session", "provider_session_id": "thread-fake"}
        for index in range(self._transient):
            yield {
                "kind": "error",
                "text": f"Reconnecting... {index + 2}/5 (stream disconnected before completion)",
                "transient": True,
            }
        if self._text:
            yield {"kind": "agent_message", "text": self._text}
        yield {"kind": "usage", "usage": {"input_tokens": 10, "output_tokens": 2}}
        yield {"kind": "turn.completed", "exit_code": self._exit_code}

    def resume(self, ws, provider_session_id, prompt, *, model=None, timeout=None):
        self.calls.append(("resume", provider_session_id, prompt))
        for index in range(self._transient):
            yield {
                "kind": "error",
                "text": f"Reconnecting... {index + 2}/5 (stream disconnected before completion)",
                "transient": True,
            }
        if self._text:
            yield {"kind": "agent_message", "text": f"продолжение: {prompt}"}
        yield {"kind": "turn.completed", "exit_code": self._exit_code}


class OrchestratorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = Store(self.root / "agents.db")
        self.addCleanup(self.store.close)
        self.store.upsert_agent(Agent(id="tester", name="Тестер", role="проверка"))
        self.registry = AdapterRegistry()

    def orchestrator(self, adapter: FakeAdapter | None = None) -> Orchestrator:
        if adapter is not None:
            self.registry.register(adapter)
        return Orchestrator(self.store, self.registry)

    def workspace(self, agent_id: str = "tester") -> Path:
        return self.root / "workspaces" / agent_id


class TurnTests(OrchestratorTestCase):
    def test_successful_turn_records_everything(self):
        adapter = FakeAdapter()
        result = self.orchestrator(adapter).ask("tester", "ответь PONG")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.text, "PONG")
        self.assertEqual(result.provider_session_id, "thread-fake")
        self.assertEqual(result.usage, {"input_tokens": 10, "output_tokens": 2})
        self.assertEqual(result.exit_code, 0)
        session = self.store.get_session(result.session_id)
        self.assertEqual(session.provider_session_id, "thread-fake")
        self.assertEqual(self.store.get_agent("tester").state, "idle")
        roles = [(message.role, message.text) for message in self.store.list_messages(result.session_id)]
        self.assertEqual(roles, [("user", "ответь PONG"), ("agent", "PONG")])

    def test_second_turn_resumes_same_session(self):
        adapter = FakeAdapter()
        orchestrator = self.orchestrator(adapter)
        first = orchestrator.ask("tester", "первый")
        second = orchestrator.ask("tester", "второй")
        self.assertEqual(first.session_id, second.session_id)
        self.assertIn(("resume", "thread-fake", "второй"), adapter.calls)
        self.assertEqual(len(self.store.list_sessions("tester")), 1)

    def test_new_session_flag_starts_another_session(self):
        adapter = FakeAdapter()
        orchestrator = self.orchestrator(adapter)
        first = orchestrator.ask("tester", "первый")
        second = orchestrator.ask("tester", "второй", new_session=True)
        self.assertNotEqual(first.session_id, second.session_id)
        self.assertEqual(len(self.store.list_sessions("tester")), 2)
        self.assertIn(("start", self.workspace(), "второй", None), adapter.calls)

    def test_unknown_agent_is_honest_failure(self):
        result = self.orchestrator(FakeAdapter()).ask("нет-такого", "привет")
        self.assertFalse(result.ok)
        self.assertIn("неизвестный агент", result.error)
        self.assertEqual(result.session_id, "")

    def test_empty_prompt_rejected_without_running_provider(self):
        adapter = FakeAdapter()
        result = self.orchestrator(adapter).ask("tester", "   ")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "пустая задача")
        self.assertEqual(adapter.calls, [])

    def test_unavailable_provider_does_not_start_process(self):
        adapter = FakeAdapter(available=False)
        result = self.orchestrator(adapter).ask("tester", "привет")
        self.assertFalse(result.ok)
        self.assertIn("тестовый провайдер недоступен", result.error)
        self.assertEqual(adapter.calls, [], "процесс не должен запускаться")
        self.assertEqual(self.store.get_agent("tester").state, "unavailable")
        kinds = [event.kind for event in self.store.list_events(result.session_id)]
        self.assertIn("error", kinds)

    def test_provider_unavailable_raised_inside_stream(self):
        adapter = FakeAdapter(fail=True)
        result = self.orchestrator(adapter).ask("tester", "привет")
        self.assertFalse(result.ok)
        self.assertIn("запуск не удался", result.error)
        self.assertEqual(self.store.get_agent("tester").state, "error")
        self.assertEqual(self.store.get_session(result.session_id).state, "error")

    def test_adapter_crash_becomes_error_not_traceback(self):
        class Exploding(FakeAdapter):
            def start(self, *args, **kwargs):
                raise RuntimeError("внутренняя поломка")
                yield  # pragma: no cover

        result = self.orchestrator(Exploding()).ask("tester", "привет")
        self.assertFalse(result.ok)
        self.assertIn("сбой провайдера", result.error)
        self.assertIn("RuntimeError", result.error)

    def test_session_of_another_agent_rejected(self):
        self.store.upsert_agent(Agent(id="other", name="Другой"))
        other_session = self.store.create_session("other")
        result = self.orchestrator(FakeAdapter()).ask("tester", "привет", session_id=other_session.id)
        self.assertFalse(result.ok)
        self.assertIn("другому агенту", result.error)

    def test_transient_errors_do_not_fail_a_recovered_turn(self):
        """Наблюдение 2026-10-12: обрыв потока у codex («Reconnecting…»), затем успешный ответ.

        Ход обязан считаться успешным: ответ получен, код возврата 0.
        """
        adapter = FakeAdapter(transient=3, text="42")
        result = self.orchestrator(adapter).ask("tester", "назови число 42")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.text, "42")
        self.assertIsNone(result.error)
        finished = self.store.list_events(result.session_id)[-1]
        self.assertEqual(finished.kind, "turn.finished")
        self.assertEqual(finished.payload["outcome"], "ok")
        self.assertEqual(len(finished.payload["transient_errors"]), 3)
        self.assertEqual(self.store.get_agent("tester").state, "idle")
        self.assertEqual(self.store.get_session(result.session_id).state, "open")

    def test_permanent_error_still_fails_the_turn(self):
        class PermanentFailure(FakeAdapter):
            def start(self, *args, **kwargs):
                yield {"kind": "error", "text": "model is not supported"}
                yield {"kind": "turn.completed", "exit_code": 1}

        result = self.orchestrator(PermanentFailure()).ask("tester", "привет")
        self.assertFalse(result.ok)
        self.assertIn("not supported", result.error)
        self.assertEqual(self.store.get_agent("tester").state, "error")
        self.assertEqual(self.store.get_session(result.session_id).state, "error")

    def test_silent_provider_without_answer_is_failure(self):
        result = self.orchestrator(FakeAdapter(text="")).ask("tester", "привет")
        self.assertFalse(result.ok)
        self.assertIn("не дав ответа", result.error)

    def test_nonzero_exit_without_answer_reports_code(self):
        result = self.orchestrator(FakeAdapter(text="", exit_code=7)).ask("tester", "привет")
        self.assertFalse(result.ok)
        self.assertIn("кодом 7", result.error)

    def test_journal_starts_with_user_message(self):
        result = self.orchestrator(FakeAdapter()).ask("tester", "привет")
        events = self.store.list_events(result.session_id)
        self.assertEqual(events[0].kind, "user_message")
        self.assertEqual(events[-1].kind, "turn.finished")
        self.assertEqual(events[-1].payload["ok"], True)


class RunLogTests(OrchestratorTestCase):
    def test_file_log_written_next_to_workspace(self):
        result = self.orchestrator(FakeAdapter()).ask("tester", "привет")
        log_path = self.workspace() / "runs" / f"{result.session_id}.jsonl"
        self.assertTrue(log_path.exists(), log_path)
        lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([line["kind"] for line in lines][0], "user_message")
        self.assertEqual(lines[-1]["kind"], "turn.finished")
        self.assertEqual([line["seq"] for line in lines], list(range(1, len(lines) + 1)))

    def test_unavailable_provider_is_also_logged(self):
        result = self.orchestrator(FakeAdapter(available=False)).ask("tester", "привет")
        log_path = self.workspace() / "runs" / f"{result.session_id}.jsonl"
        kinds = [json.loads(line)["kind"] for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(kinds, ["user_message", "error"])

    def test_secrets_are_not_written_to_file_log(self):
        log = RunLog(self.workspace(), "session-with-secrets")
        from runneos_agents.model import Event

        log.write(
            Event(
                session_id="session-with-secrets",
                kind="raw",
                payload={
                    "authorization": "Bearer sk-live-123",
                    "OPENAI_API_KEY": "sk-live-123",
                    "nested": {"api_token": "tok-1", "text": "обычный текст"},
                    "text": "видимый текст",
                },
                seq=1,
                created_at="2026-10-12T00:00:00Z",
            )
        )
        content = log.path.read_text(encoding="utf-8")
        self.assertNotIn("sk-live-123", content)
        self.assertNotIn("tok-1", content)
        self.assertIn("видимый текст", content)
        payload = json.loads(content.splitlines()[0])["payload"]
        self.assertEqual(payload["authorization"], "<скрыто>")
        self.assertEqual(payload["nested"]["api_token"], "<скрыто>")
        self.assertEqual(payload["nested"]["text"], "обычный текст")

    def test_long_text_is_trimmed(self):
        log = RunLog(self.workspace(), "session-long")
        from runneos_agents.model import Event

        log.write(Event(session_id="session-long", kind="raw", payload={"text": "я" * 9000}, seq=1))
        payload = json.loads(log.path.read_text(encoding="utf-8").splitlines()[0])["payload"]
        self.assertLess(len(payload["text"]), 9000)
        self.assertIn("обрезано", payload["text"])

    def test_unwritable_log_does_not_break_turn(self):
        """Журнал — удобство: если файл не пишется, ход всё равно проходит."""
        adapter = FakeAdapter()
        orchestrator = self.orchestrator(adapter)
        with unittest.mock.patch.object(RunLog, "write", return_value=False):
            result = orchestrator.ask("tester", "привет")
        self.assertTrue(result.ok, result.error)

    def test_workspace_gets_manifest_and_git(self):
        self.orchestrator(FakeAdapter()).ask("tester", "привет")
        manifest = json.loads((self.workspace() / "agent.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["agent_id"], "tester")
        self.assertEqual(manifest["provider"], "codex")

    def test_workspace_path_recorded_on_agent(self):
        self.orchestrator(FakeAdapter()).ask("tester", "привет")
        agent = self.store.get_agent("tester")
        self.assertEqual(Path(agent.workspace), self.workspace())


class RoutingTests(unittest.TestCase):
    def test_unknown_provider_is_honestly_unavailable(self):
        registry = AdapterRegistry()
        availability = registry.get("nope").availability()
        self.assertFalse(availability.available)
        self.assertIn("неизвестный провайдер", availability.reason)

    def test_registry_includes_known_providers(self):
        providers = [item.provider for item in AdapterRegistry().availability()]
        self.assertEqual(providers, ["codex", "opencode"])

    def test_broken_adapter_does_not_break_provider_list(self):
        class Broken:
            provider = "codex"

            def availability(self):
                raise RuntimeError("поломка")

        registry = AdapterRegistry()
        registry.register(Broken())
        rows = {item.provider: item for item in registry.availability()}
        self.assertFalse(rows["codex"].available)
        self.assertIn("RuntimeError", rows["codex"].reason)


class NoProviderEnvironmentTests(OrchestratorTestCase):
    """Система без провайдеров: честный отказ, процесс не запускается, ОС не затронута.

    Проверяется отдельным процессом с урезанным PATH: так тест не зависит от того, установлены
    ли на машине codex/opencode, и не может случайно запустить настоящий CLI.
    """

    def test_real_adapters_report_unavailable_and_refuse_to_run(self):
        script = textwrap.dedent(
            f"""
            import json, sys
            sys.path.insert(0, {str(REPO / "packages" / "runneos-agents" / "files")!r})
            from runneos_agents.model import Agent
            from runneos_agents.orchestrator import Orchestrator
            from runneos_agents.store import Store

            store = Store({str(self.root / "noprov.db")!r})
            store.upsert_agent(Agent(id="tester", name="Тестер", provider="codex"))
            orchestrator = Orchestrator(store)
            providers = [
                {{"provider": item.provider, "available": item.available, "reason": item.reason}}
                for item in orchestrator.providers()
            ]
            result = orchestrator.ask("tester", "ответь ровно PONG")
            events = [event.kind for event in store.list_events(result.session_id)]
            print(json.dumps({{
                "providers": providers,
                "ok": result.ok,
                "error": result.error,
                "agent_state": store.get_agent("tester").state,
                "events": events,
            }}, ensure_ascii=False))
            """
        )
        environment = dict(os.environ)
        environment["PATH"] = str(self.root / "пустой-path")
        # HOME тоже подменяем: адаптер ищет бинарь и в ~/.local/bin, а он у пользователя
        # как раз там. Без подмены тест зависел бы от того, установлен ли codex на машине.
        environment["HOME"] = str(self.root / "пустой-home")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment.pop("RUNNEOS_AGENTS_ADAPTER_PATH", None)
        environment.pop("XDG_DATA_HOME", None)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=environment,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(sorted(item["provider"] for item in payload["providers"]), ["codex", "opencode"])
        for item in payload["providers"]:
            self.assertFalse(item["available"], item)
            self.assertIn("не найден", item["reason"])
        self.assertFalse(payload["ok"])
        self.assertIn("не найден", payload["error"])
        self.assertNotIn("Traceback", payload["error"])
        self.assertEqual(payload["agent_state"], "unavailable")
        self.assertEqual(payload["events"], ["user_message", "error"])

    def test_read_only_commands_do_not_need_providers(self):
        """`provider list` и `doctor` работают и без провайдеров — это не ошибка команды."""
        self.store.upsert_agent(Agent(id="tester", name="Тестер", provider="nonexistent"))
        orchestrator = self.orchestrator(FakeAdapter())
        rows = {item.provider: item for item in orchestrator.providers()}
        self.assertTrue(rows["codex"].available)
        self.assertFalse(rows["opencode"].available)


class WorkspaceHelperTests(OrchestratorTestCase):
    def test_ensure_workspace_is_idempotent_and_keeps_files(self):
        agent = self.store.get_agent("tester")
        workspace = ensure_workspace(agent.replace(workspace=self.workspace()), self.root)
        marker = workspace.path / "моя-работа.txt"
        marker.write_text("не удалять", encoding="utf-8")
        again = ensure_workspace(agent.replace(workspace=self.workspace()), self.root)
        self.assertTrue(marker.exists())
        self.assertEqual(again.read_manifest()["agent_id"], "tester")

class PolicyTests(unittest.TestCase):
    def test_default_is_fail_closed(self):
        from runneos_agents.policy import Policy

        policy = Policy()
        self.assertFalse(policy.check_command("rm -rf /").allowed)
        self.assertEqual(policy.check_command("rm -rf /").rule, "default-deny")

    def test_allowed_read_only_command(self):
        from runneos_agents.policy import Policy

        policy = Policy.default_for(Path("/tmp/workspace"))
        self.assertTrue(policy.check_command("ls -la").allowed)
        self.assertTrue(policy.check_command("git status").allowed)
        self.assertFalse(policy.check_command("git status --short; rm -rf /").allowed)

    def test_path_outside_workspace_refused(self):
        from runneos_agents.policy import Policy

        policy = Policy.default_for(Path("/tmp/agent-workspace"))
        Path("/tmp/agent-workspace").mkdir(parents=True, exist_ok=True)
        self.assertTrue(policy.check_path("/tmp/agent-workspace/файл").allowed)
        self.assertFalse(policy.check_path("/etc/passwd").allowed)
        self.assertTrue(Policy().check_path("/etc/passwd").rule == "no-workspace")


if __name__ == "__main__":
    unittest.main()
