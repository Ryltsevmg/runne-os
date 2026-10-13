"""Тесты командной строки runneos-agents: коды возврата, JSON, честные отказы.

Провайдеры подменяются тестовым набором адаптеров через RUNNEOS_AGENTS_ADAPTER_PATH,
поэтому реальные CLI (codex) в этих тестах не запускаются.

Запуск: PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_agents_*.py' -v
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

def _package_dir() -> Path:
    """Каталог с python-пакетом runneos_agents: дерево исходников или установка в системе."""
    here = Path(__file__).resolve()
    for candidate in (
        here.parent.parent / "packages" / "runneos-agents" / "files",
        here.parent.parent / "files",
        Path("/usr/share/runneos/agents"),
    ):
        if (candidate / "runneos_agents" / "__init__.py").exists():
            return candidate
    raise RuntimeError("не найден пакет runneos_agents")


PACKAGE_DIR = _package_dir()
sys.path.insert(0, str(PACKAGE_DIR))
CLI = PACKAGE_DIR / "runneos-agents"

FAKE_ADAPTERS = textwrap.dedent(
    '''
    """Тестовый адаптер: отвечает заранее известным текстом, без внешних процессов."""
    from runneos_agents.model import Availability, ProviderUnavailable


    class FakeCodex:
        provider = "codex"

        def availability(self):
            return Availability(provider="codex", available=True, version="fake-0.0", binary="/bin/false")

        def start(self, ws, prompt, *, model=None, instructions="", timeout=None):
            yield {"kind": "turn.started"}
            yield {"kind": "provider_session", "provider_session_id": "fake-thread-1"}
            yield {"kind": "agent_message", "text": "PONG: " + prompt}
            yield {"kind": "usage", "usage": {"input_tokens": 1, "output_tokens": 2}}
            yield {"kind": "turn.completed", "exit_code": 0}

        def resume(self, ws, provider_session_id, prompt, *, model=None, timeout=None):
            yield {"kind": "agent_message", "text": "продолжение через " + provider_session_id}
            yield {"kind": "turn.completed", "exit_code": 0}


    class BrokenOpenCode:
        provider = "opencode"

        def availability(self):
            return Availability(provider="opencode", available=False, reason="нет режима без TUI")

        def start(self, *args, **kwargs):
            raise ProviderUnavailable("opencode", "нет режима без TUI")
            yield

        def resume(self, *args, **kwargs):
            raise ProviderUnavailable("opencode", "нет режима без TUI")
            yield


    def build_registry():
        from runneos_agents.routing import AdapterRegistry
        registry = AdapterRegistry()
        registry.register(FakeCodex())
        registry.register(BrokenOpenCode())
        return registry
    '''
)


class CliTestCase(unittest.TestCase):
    """Общая обвязка: временный корень данных, подменённые адаптеры, запуск CLI."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data_home = self.tmp / "data"
        self.env = dict(os.environ)
        self.env["XDG_DATA_HOME"] = str(self.data_home)
        self.env.pop("RUNNEOS_AGENTS_HOME", None)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        adapters = self.tmp / "fake_adapters.py"
        adapters.write_text(FAKE_ADAPTERS, encoding="utf-8")
        self.env["RUNNEOS_AGENTS_ADAPTER_PATH"] = str(adapters)

    # --- помощники -----------------------------------------------------

    def run_cli(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            capture_output=True,
            text=True,
            env=env or self.env,
            timeout=120,
        )

    def json_lines(self, result: subprocess.CompletedProcess) -> list[dict]:
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    def add_agent(self, name: str = "tester", *extra: str) -> subprocess.CompletedProcess:
        return self.run_cli("agent", "add", name, *extra)

    def root_dir(self) -> Path:
        return self.data_home / "runneos" / "agents"

    def table_counts(self) -> dict:
        """Строки в четырёх таблицах базы — глазами самого файла, а не Store."""
        connection = sqlite3.connect(str(self.root_dir() / "agents.db"))
        try:
            return {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("agents", "sessions", "messages", "events")
            }
        finally:
            connection.close()


class ReadOnlyCommandTests(CliTestCase):
    """Read-only команды не должны создавать данные пользователя как побочный эффект."""

    def test_provider_list_creates_no_data(self):
        result = self.run_cli("provider", "list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.root_dir().exists(), f"создан корень данных: {self.root_dir()}")

    def test_doctor_creates_no_data(self):
        result = self.run_cli("doctor")
        self.assertEqual(result.returncode, 1, "doctor без данных обязан честно сообщить об этом")
        self.assertFalse(self.root_dir().exists(), f"создан корень данных: {self.root_dir()}")

    def test_agent_list_creates_root_only_after_init(self):
        """`agent list` читает базу: без init он не должен оставлять после себя каталог."""
        result = self.run_cli("agent", "list")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.root_dir().exists(), f"создан корень данных: {self.root_dir()}")


class EmptyDataExitCodeTests(CliTestCase):
    """Осознанное решение о кодах возврата: пустой список — не ошибка.

    `agent list` без данных отвечает 0 и приглашением создать агента: «агентов
    нет» — честное состояние, а не сбой. `sessions`, `log` и `agent show`
    адресуют конкретный объект: без данных (до init) это ошибка с кодом 1 и
    подсказкой выполнить init. Тесты закрепляют текущее поведение, чтобы смена
    решения падала явно, а не проходила молча.
    """

    def test_agent_list_without_data_is_not_an_error(self):
        result = self.run_cli("agent", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Агентов пока нет", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_agent_list_after_init_without_agents_is_not_an_error(self):
        self.assertEqual(self.run_cli("init").returncode, 0)
        result = self.run_cli("agent", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Агентов пока нет", result.stdout)

    def test_agent_list_json_without_data_is_empty_and_ok(self):
        result = self.run_cli("agent", "list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_sessions_without_data_is_error_with_hint(self):
        result = self.run_cli("sessions", "tester")
        self.assertEqual(result.returncode, 1)
        self.assertIn("данных ещё нет", result.stderr)
        self.assertIn("runneos-agents init", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_log_without_data_is_error_with_hint(self):
        result = self.run_cli("log", "нет-такой-сессии")
        self.assertEqual(result.returncode, 1)
        self.assertIn("данных ещё нет", result.stderr)
        self.assertIn("runneos-agents init", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_agent_show_without_data_is_error_with_hint(self):
        result = self.run_cli("agent", "show", "нет-такого")
        self.assertEqual(result.returncode, 1)
        self.assertIn("данных ещё нет", result.stderr)
        self.assertIn("runneos-agents init", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class InitTests(CliTestCase):
    def test_init_creates_root_and_db(self):
        result = self.run_cli("init", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self.json_lines(result)[0]
        self.assertTrue(Path(payload["db"]).exists())
        self.assertTrue((Path(payload["root"]) / "workspaces").is_dir())

    def test_init_is_idempotent(self):
        self.assertEqual(self.run_cli("init").returncode, 0)
        self.assertEqual(self.run_cli("init").returncode, 0)


class ProviderTests(CliTestCase):
    def test_provider_list_uses_injected_adapters(self):
        result = self.run_cli("provider", "list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = {row["provider"]: row for row in self.json_lines(result)}
        self.assertTrue(rows["codex"]["available"])
        self.assertEqual(rows["codex"]["version"], "fake-0.0")
        self.assertFalse(rows["opencode"]["available"])
        self.assertIn("TUI", rows["opencode"]["reason"])

    def test_json_flag_before_subcommand(self):
        result = self.run_cli("--json", "provider", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.json_lines(result))


class AgentTests(CliTestCase):
    def test_add_creates_workspace_and_manifest(self):
        result = self.add_agent("Тестер", "--role", "проверка", "--id", "tester", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self.json_lines(result)[0]
        workspace = Path(payload["workspace"])
        self.assertTrue(workspace.is_dir())
        manifest = json.loads((workspace / "agent.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["agent_id"], "tester")
        self.assertEqual(manifest["role"], "проверка")

    def test_add_keeps_existing_created_at(self):
        first = self.json_lines(self.add_agent("Тестер", "--id", "tester", "--json"))[0]
        second = self.json_lines(
            self.add_agent("Тестер 2", "--id", "tester", "--role", "новая", "--json")
        )[0]
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertEqual(second["role"], "новая")

    def test_list_human_and_json(self):
        self.add_agent("Тестер", "--id", "tester")
        human = self.run_cli("agent", "list")
        self.assertEqual(human.returncode, 0)
        self.assertIn("tester", human.stdout)
        rows = self.json_lines(self.run_cli("agent", "list", "--json"))
        self.assertEqual(rows[0]["id"], "tester")
        self.assertTrue(rows[0]["workspace_exists"])

    def test_list_empty_is_not_error(self):
        result = self.run_cli("agent", "list")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Агентов пока нет", result.stdout)

    def test_show_unknown_agent_is_error_without_traceback(self):
        self.run_cli("init")
        result = self.run_cli("agent", "show", "нет-такого")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("неизвестный агент", result.stderr)

    def test_show_before_init_explains_what_to_do(self):
        result = self.run_cli("agent", "show", "нет-такого")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("runneos-agents init", result.stderr)

    def test_remove_reports_kept_workspace(self):
        workspace = self.json_lines(self.add_agent("Тестер", "--id", "tester", "--json"))[0]["workspace"]
        result = self.run_cli("agent", "remove", "tester", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.json_lines(result)[0]["removed"])
        self.assertTrue(Path(workspace).is_dir(), "файлы воркспейса не должны удаляться")
        self.assertEqual(self.run_cli("agent", "list", "--json").stdout.strip(), "")

    def test_invalid_name_for_slug_falls_back(self):
        result = self.add_agent("Проверяющий агент", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.json_lines(result)[0]["id"].startswith("agent-"))


class RemoveCascadeTests(CliTestCase):
    """`agent remove` через CLI: каскад доходит до всех четырёх таблиц.

    Путь Store.remove_agent покрыт отдельно, но поломка именно CLI-ветки
    (удаление не вызвано, каскад отключён, часть таблиц забыта) иначе не видна:
    команда отвечает 0, а сессии, сообщения и журнал остаются в базе.
    """

    def test_remove_deletes_sessions_messages_and_events_but_keeps_workspace(self):
        workspace = Path(
            self.json_lines(self.add_agent("Тестер", "--id", "tester", "--json"))[0]["workspace"]
        )
        turn = self.json_lines(self.run_cli("ask", "tester", "привет", "--json"))[0]
        self.assertTrue(turn["ok"], turn)
        before = self.table_counts()
        self.assertEqual(before["agents"], 1)
        self.assertGreater(before["sessions"], 0, "ask обязан создать сессию")
        self.assertGreater(before["messages"], 0, "ask обязан сохранить сообщения")
        self.assertGreater(before["events"], 0, "ask обязан записать журнал")

        result = self.run_cli("agent", "remove", "tester", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.json_lines(result)[0]["removed"])
        self.assertEqual(
            self.table_counts(),
            {"agents": 0, "sessions": 0, "messages": 0, "events": 0},
            "каскад обязан очистить все четыре таблицы",
        )
        self.assertTrue((workspace / "agent.json").is_file(), "файлы воркспейса не удаляются")
        self.assertEqual(self.run_cli("agent", "list", "--json").stdout.strip(), "")
        self.assertEqual(self.run_cli("agent", "show", "tester").returncode, 1)

    def test_remove_without_sessions_still_reports_removed(self):
        self.add_agent("Тестер", "--id", "tester")
        result = self.run_cli("agent", "remove", "tester")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.table_counts()["agents"], 0)

    def test_remove_unknown_agent_is_error(self):
        self.run_cli("init")
        result = self.run_cli("agent", "remove", "нет-такого")
        self.assertEqual(result.returncode, 1)
        self.assertIn("неизвестный агент", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class AskTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.add_agent("Тестер", "--id", "tester")

    def test_ask_runs_through_adapter(self):
        result = self.run_cli("ask", "tester", "привет", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self.json_lines(result)[0]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["text"], "PONG: привет")
        self.assertEqual(payload["provider_session_id"], "fake-thread-1")
        self.assertEqual(payload["usage"]["output_tokens"], 2)

    def test_second_ask_resumes_same_session(self):
        first = self.json_lines(self.run_cli("ask", "tester", "первый", "--json"))[0]
        second = self.json_lines(self.run_cli("ask", "tester", "второй", "--json"))[0]
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertIn("продолжение через fake-thread-1", second["text"])

    def test_new_flag_starts_another_session(self):
        first = self.json_lines(self.run_cli("ask", "tester", "первый", "--json"))[0]
        second = self.json_lines(self.run_cli("ask", "tester", "второй", "--new", "--json"))[0]
        self.assertNotEqual(first["session_id"], second["session_id"])
        sessions = self.json_lines(self.run_cli("sessions", "tester", "--json"))
        self.assertEqual(len(sessions), 2)

    def test_unavailable_provider_returns_one_without_traceback(self):
        self.add_agent("Без провайдера", "--id", "noprov", "--provider", "opencode")
        result = self.run_cli("ask", "noprov", "привет")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("TUI", result.stderr)

    def test_unknown_agent_is_error(self):
        result = self.run_cli("ask", "нет-такого", "привет")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)

    def test_empty_prompt_is_error(self):
        result = self.run_cli("ask", "tester", "   ")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)

    def test_journal_records_the_turn(self):
        payload = self.json_lines(self.run_cli("ask", "tester", "привет", "--json"))[0]
        kinds = [row["kind"] for row in self.json_lines(self.run_cli("log", payload["session_id"], "--json"))]
        self.assertEqual(kinds[0], "user_message")
        self.assertIn("agent_message", kinds)
        self.assertEqual(kinds[-1], "turn.finished")


class SessionsAndLogTests(CliTestCase):
    def test_sessions_unknown_agent(self):
        result = self.run_cli("sessions", "нет-такого")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)

    def test_sessions_empty(self):
        self.add_agent("Тестер", "--id", "tester")
        result = self.run_cli("sessions", "tester")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Сессий пока нет", result.stdout)

    def test_log_unknown_session(self):
        result = self.run_cli("log", "нет-такой")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)


class DoctorTests(CliTestCase):
    def test_doctor_after_init(self):
        self.run_cli("init")
        self.add_agent("Тестер", "--id", "tester")
        result = self.run_cli("doctor", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self.json_lines(result)[0]
        checks = {check["name"]: check for check in payload["checks"]}
        self.assertTrue(checks["корень данных"]["ok"])
        self.assertTrue(checks["база"]["ok"])
        self.assertTrue(checks["воркспейсы агентов"]["ok"])
        self.assertTrue(checks["провайдер codex"]["ok"])

    def test_doctor_without_data_reports_honestly(self):
        env = dict(self.env)
        env["XDG_DATA_HOME"] = str(self.tmp / "пустой")
        result = self.run_cli("doctor", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("нет каталога", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class UsageTests(CliTestCase):
    def test_no_command_is_usage_error(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)

    def test_unknown_command_is_usage_error(self):
        result = self.run_cli("полететь")
        self.assertEqual(result.returncode, 2)

    def test_version(self):
        result = self.run_cli("--version")
        self.assertEqual(result.returncode, 0)
        self.assertIn("runneos-agents", result.stdout)


if __name__ == "__main__":
    unittest.main()
