"""Тесты адаптеров провайдеров RunneOS.

Только `unittest` и стандартная библиотека: ни сети, ни живых провайдеров, ни записей в `~/.codex`.
Живой прогон включается явно переменной окружения:

    RUNNEOS_LIVE_TESTS=1 python -m unittest discover -s tests -p 'test_agents_adapters.py' -v

Фикстуры — реальные строки CLI (происхождение и правки: `adapters/fixtures/README.md`),
подпроцесс подменяется фейком, поэтому весь разбор JSONL проверяется оффлайн.

Проверяется ровно то, что требует контракт (INTERFACE.md, раздел 5): нормализованные события,
`provider_session` из `thread.started`, `usage`, ошибки без исключений наружу, таймаут с
убийством процесса, `ProviderUnavailable` без бинаря и отсутствие секретов в событиях и логах.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
FILES_DIR = REPO_ROOT / "packages" / "runneos-agents" / "files"
if str(FILES_DIR) not in sys.path:
    sys.path.insert(0, str(FILES_DIR))

from runneos_agents.adapters import (  # noqa: E402  — импорт после правки sys.path
    available,
    build_registry,
    get_adapter,
    provider_names,
)
from runneos_agents.adapters import base as adapters_base  # noqa: E402
from runneos_agents.adapters.codex import CodexAdapter  # noqa: E402
from runneos_agents.adapters.opencode import OpencodeAdapter  # noqa: E402
from runneos_agents.adapters.stub import StubAdapter  # noqa: E402
from runneos_agents.model import Agent, Availability, ProviderUnavailable  # noqa: E402
from runneos_agents.orchestrator import Orchestrator  # noqa: E402
from runneos_agents.routing import AdapterRegistry, default_registry  # noqa: E402
from runneos_agents.store import Store  # noqa: E402

FIXTURES = FILES_DIR / "runneos_agents" / "adapters" / "fixtures"
#: Каталог задач: подпроцесс подменён, поэтому каталог нужен только как значение argv/cwd.
WORKSPACE = Path("/tmp/runneos-agents-adapters-test-ws")
FAKE_THREAD_ID = "00000000-0000-4000-8000-000000000001"
FAKE_THREAD_ID_2 = "00000000-0000-4000-8000-000000000002"
FAKE_SESSION_ID = "ses_0000000000000000000000000001"
SECRET = "sk-test"
PROMPT = "ответь ровно PONG"

EXPECTED_CODEX_USAGE = {
    "input_tokens": 14094,
    "cached_input_tokens": 12288,
    "cache_write_input_tokens": 0,
    "output_tokens": 6,
    "reasoning_output_tokens": 0,
}
EXPECTED_OPENCODE_USAGE = {
    "input_tokens": 6875,
    "output_tokens": 27,
    "reasoning_output_tokens": 0,
    "cached_input_tokens": 0,
    "cache_write_input_tokens": 0,
}


def fixture_lines(name: str) -> list[str]:
    """Строки фикстуры как есть (то, что реально печатал CLI)."""
    return [
        line
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def fixture_objects(name: str) -> list[dict]:
    """Разобранные строки фикстуры."""
    return [json.loads(line) for line in fixture_lines(name)]


# --- фейковый подпроцесс ---------------------------------------------------


class FakeStream:
    """Поток вывода фейкового процесса: строки, затем EOF (или «зависание»)."""

    def __init__(self, lines: object = (), *, hang: bool = False) -> None:
        self._lines = [
            line if str(line).endswith("\n") else f"{line}\n" for line in (lines or ())
        ]
        self._hang = bool(hang)
        self._closed = threading.Event()

    def __iter__(self) -> "FakeStream":
        return self

    def __next__(self) -> str:
        if self._lines:
            return self._lines.pop(0)
        if self._hang and not self._closed.is_set():
            self._closed.wait(timeout=30)  # процесс «висит» и не закрывает вывод
        raise StopIteration

    def close(self) -> None:
        self._closed.set()


class FakeProcess:
    """Минимальная замена `subprocess.Popen` для оффлайн-проверок."""

    def __init__(
        self,
        *,
        stdout_lines: object = (),
        stderr_lines: object = (),
        returncode: int = 0,
        hang_stdout: bool = False,
    ) -> None:
        # pid=0: настоящую группу процессов адаптер не трогает (защита `pid > 1`),
        # поэтому подмена не может сигналить чужим процессам.
        self.pid = 0
        self.stdout = FakeStream(stdout_lines, hang=hang_stdout)
        self.stderr = FakeStream(stderr_lines)
        self.returncode = int(returncode)
        self.signals: list[int] = []
        self._exited = threading.Event()
        if not hang_stdout:
            self._exited.set()

    def poll(self) -> int | None:
        return self.returncode if self._exited.is_set() else None

    def wait(self, timeout: float | None = None) -> int:
        if self._exited.wait(timeout if timeout is not None else 5.0):
            return self.returncode
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        self._exited.set()
        self.stdout.close()
        self.stderr.close()

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)


class AdapterTestCase(unittest.TestCase):
    """Общая обвязка: подменяет запуск процесса и получение версии."""

    def make_process(self, fixture: str | None = None, **kwargs: object) -> FakeProcess:
        lines = fixture_lines(fixture) if fixture else []
        return FakeProcess(stdout_lines=lines, **kwargs)

    def run_stream(
        self,
        adapter: object,
        *,
        method: str = "start",
        process: FakeProcess | None = None,
        prompt: str = PROMPT,
        session_id: str = FAKE_THREAD_ID,
        model: str | None = None,
        instructions: str = "",
        timeout: float | None = None,
        ws: Path = WORKSPACE,
        sandbox_prefix: list[str] | None = None,
    ) -> tuple[list[dict], dict, FakeProcess]:
        """Прокрутить start/resume на фейковом процессе.

        Возвращает (события, перехваченные argv/kwargs запуска, процесс).
        """
        proc = process if process is not None else FakeProcess()
        captured: dict = {}

        def fake_popen(argv: object, **kwargs: object) -> FakeProcess:
            captured["argv"] = [str(item) for item in argv]  # type: ignore[union-attr]
            captured["kwargs"] = dict(kwargs)
            return proc

        with mock.patch.object(adapters_base.subprocess, "Popen", side_effect=fake_popen), (
            mock.patch.object(adapters_base, "probe_version", return_value="fixture-1.0")
        ):
            if method == "start":
                events = list(
                    adapter.start(  # type: ignore[attr-defined]
                        ws,
                        prompt,
                        model=model,
                        instructions=instructions,
                        timeout=timeout,
                        sandbox_prefix=sandbox_prefix,
                    )
                )
            else:
                events = list(
                    adapter.resume(  # type: ignore[attr-defined]
                        ws,
                        session_id,
                        prompt,
                        model=model,
                        timeout=timeout,
                        sandbox_prefix=sandbox_prefix,
                    )
                )
        return events, captured, proc

    @staticmethod
    def kinds(events: list[dict]) -> list[str]:
        return [str(event.get("kind")) for event in events]

    @staticmethod
    def text_of(events: list[dict], kind: str) -> str:
        return "".join(
            str(event.get("text") or "") for event in events if event.get("kind") == kind
        )


# --- 1-2. разбор фикстур и маппинг в нормализованные события ---------------


class FixtureIntegrityTests(unittest.TestCase):
    """Фикстуры должны быть валидным JSONL: это источник истины для остальных тестов."""

    EXPECTED = (
        "codex_start.jsonl",
        "codex_resume_command.jsonl",
        "codex_failure.jsonl",
        "opencode_start.jsonl",
        "opencode_resume.jsonl",
        "opencode_tool.jsonl",
        "opencode_error.jsonl",
    )

    def test_every_fixture_is_jsonl(self) -> None:
        for name in self.EXPECTED:
            with self.subTest(fixture=name):
                path = FIXTURES / name
                self.assertTrue(path.is_file(), f"нет фикстуры {name}")
                rows = fixture_objects(name)
                self.assertTrue(rows, f"фикстура {name} пуста")
                for row in rows:
                    self.assertIsInstance(row, dict)
                    self.assertIn("type", row)

    def test_fixtures_have_no_secrets_or_home_paths(self) -> None:
        for name in self.EXPECTED:
            with self.subTest(fixture=name):
                text = (FIXTURES / name).read_text(encoding="utf-8")
                self.assertNotIn("sk-", text)
                self.assertNotIn(str(Path.home()), text)


class CodexFixtureTests(AdapterTestCase):
    """Новый ход, продолжение сессии и сбой codex — на реальных строках прогонов."""

    def codex(self, **kwargs: object) -> CodexAdapter:
        return CodexAdapter(binary=sys.executable, **kwargs)

    def test_start_fixture_maps_to_normalized_events(self) -> None:
        events, captured, _ = self.run_stream(
            self.codex(), process=self.make_process("codex_start.jsonl")
        )
        self.assertEqual(
            self.kinds(events),
            ["turn.started", "provider_session", "agent_message", "usage", "turn.completed"],
        )
        # provider_session берётся из thread.started
        self.assertEqual(events[1]["provider_session_id"], FAKE_THREAD_ID)
        self.assertEqual(events[2]["text"], "PONG")
        self.assertEqual(events[3]["usage"], EXPECTED_CODEX_USAGE)
        self.assertEqual(events[-1]["exit_code"], 0)
        # turn.started провайдера не дублируется
        self.assertEqual(self.kinds(events).count("turn.started"), 1)

    def test_start_argv_and_process_environment(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_HOME": "/tmp/fake-codex-home"}):
            _, captured, _ = self.run_stream(
                self.codex(), process=self.make_process("codex_start.jsonl")
            )
        argv = captured["argv"]
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1:5], ["exec", "--json", "--skip-git-repo-check", "-C"])
        self.assertEqual(argv[5], str(WORKSPACE))
        self.assertEqual(argv[6:8], ["-s", "workspace-write"])
        # Задача с ведущим дефисом безопасна только после разделителя `--`.
        self.assertEqual(argv[-2:], ["--", PROMPT])
        # stdin закрыт: иначе codex ждёт ввод и печатает «Reading additional input from stdin».
        self.assertEqual(captured["kwargs"]["stdin"], subprocess.DEVNULL)
        self.assertEqual(captured["kwargs"]["cwd"], str(WORKSPACE))
        self.assertTrue(captured["kwargs"]["start_new_session"])
        # CODEX_HOME уходит дальше как есть: адаптер окружение не подменяет.
        self.assertEqual(captured["kwargs"]["env"]["CODEX_HOME"], "/tmp/fake-codex-home")

    def test_leading_dash_prompt_goes_after_separator(self) -> None:
        _, captured, _ = self.run_stream(self.codex(), prompt="-PONG")
        self.assertEqual(captured["argv"][-2:], ["--", "-PONG"])

    def test_instructions_are_prepended_to_prompt(self) -> None:
        _, captured, _ = self.run_stream(
            self.codex(), instructions="Ты тестовый агент", prompt="привет"
        )
        self.assertEqual(captured["argv"][-1], "Ты тестовый агент\n\nпривет")

    def test_resume_fixture_maps_command_and_usage(self) -> None:
        events, captured, _ = self.run_stream(
            self.codex(),
            method="resume",
            session_id=FAKE_THREAD_ID,
            process=self.make_process("codex_resume_command.jsonl"),
        )
        self.assertEqual(
            self.kinds(events),
            ["turn.started", "agent_message", "command", "agent_message", "usage", "turn.completed"],
        )
        # Тот же thread_id: повторный provider_session не нужен.
        self.assertNotIn("provider_session", self.kinds(events))
        self.assertEqual(events[2]["text"], "/usr/bin/bash -lc 'echo PONG-RESUME'")
        self.assertEqual(self.text_of(events, "agent_message"), "Выполню команду.\nPONG")
        self.assertEqual(events[4]["usage"], {**EXPECTED_CODEX_USAGE, "input_tokens": 43059,
                                              "cached_input_tokens": 38528, "output_tokens": 64})

        argv = captured["argv"]
        self.assertEqual(argv[1:3], ["exec", "resume"])
        self.assertEqual(argv[3], FAKE_THREAD_ID)
        # У `resume` нет -C/--cd и -s/--sandbox: каталог задаёт cwd, песочницу — sandbox_mode.
        self.assertNotIn("-C", argv)
        self.assertNotIn("-s", argv)
        self.assertEqual(argv[6:8], ["-c", "sandbox_mode=workspace-write"])
        self.assertEqual(argv[-2:], ["--", PROMPT])
        self.assertEqual(captured["kwargs"]["cwd"], str(WORKSPACE))

    def test_resume_uses_new_thread_id_if_provider_forks(self) -> None:
        process = FakeProcess(
            stdout_lines=[f'{{"type":"thread.started","thread_id":"{FAKE_THREAD_ID_2}"}}']
        )
        events, _, _ = self.run_stream(
            self.codex(), method="resume", session_id=FAKE_THREAD_ID, process=process
        )
        # Провайдер вернул другую сессию — это надо сохранить, иначе resume уйдёт не туда.
        self.assertEqual(events[1]["provider_session_id"], FAKE_THREAD_ID_2)

    def test_failure_fixture_yields_single_error_and_keeps_warning(self) -> None:
        events, _, _ = self.run_stream(
            self.codex(), process=self.make_process("codex_failure.jsonl", returncode=1)
        )
        self.assertEqual(
            self.kinds(events),
            ["turn.started", "provider_session", "raw", "error", "turn.completed"],
        )
        # Предупреждение item-уровня сохранено как raw и не превращает ход в ошибку само по себе.
        raw = next(event for event in events if event["kind"] == "raw")
        self.assertEqual(raw["payload"]["item"]["type"], "error")
        self.assertIn("Model metadata", raw["payload"]["item"]["message"])
        # `error` и `turn.failed` с одним текстом схлопываются в одно событие.
        errors = [event for event in events if event["kind"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("not supported when using Codex with a ChatGPT account", errors[0]["text"])
        self.assertEqual(events[-1]["exit_code"], 1)

    def test_transient_retry_fixture_is_marked_not_fatal(self) -> None:
        """Обрыв потока с «Reconnecting…» и последующим ответом — не провал хода.

        Живой случай 2026-10-12: codex-cli 0.162.1 напечатал три конверта `error`
        «Reconnecting... N/5 (stream disconnected...)», после чего отдал ответ `42`
        и завершился кодом 0. Считать это ошибкой нельзя.
        """
        events, _, _ = self.run_stream(
            self.codex(), process=self.make_process("codex_transient_retry.jsonl", returncode=0)
        )
        errors = [event for event in events if event["kind"] == "error"]
        self.assertEqual(len(errors), 3)
        for event in errors:
            self.assertTrue(event.get("transient"), event)
            self.assertIn("Reconnecting", event["text"])
        messages = [event["text"] for event in events if event["kind"] == "agent_message"]
        self.assertEqual(messages, ["42"])
        self.assertEqual(events[-1]["exit_code"], 0)

    def test_invalid_sandbox_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.codex(sandbox="read-write")


class OpencodeFixtureTests(AdapterTestCase):
    """opencode: подтверждённый неинтерактивный JSON-режим на реальных строках прогонов."""

    def opencode(self, **kwargs: object) -> OpencodeAdapter:
        return OpencodeAdapter(binary=sys.executable, **kwargs)

    def test_start_fixture_maps_to_normalized_events(self) -> None:
        events, captured, _ = self.run_stream(
            self.opencode(), process=self.make_process("opencode_start.jsonl")
        )
        self.assertEqual(
            self.kinds(events),
            ["turn.started", "provider_session", "raw", "agent_message", "turn.completed"],
        )
        self.assertEqual(events[1]["provider_session_id"], FAKE_SESSION_ID)
        self.assertEqual(events[3]["text"], "PONG")
        self.assertEqual(
            captured["argv"],
            [sys.executable, "run", "--standalone", "--format", "json", "--", PROMPT],
        )
        # У `opencode run` нет аргумента каталога: воркспейс передаётся только через cwd.
        self.assertEqual(captured["kwargs"]["cwd"], str(WORKSPACE))

    def test_resume_fixture_reuses_session_without_duplicating_it(self) -> None:
        events, captured, _ = self.run_stream(
            self.opencode(),
            method="resume",
            session_id=FAKE_SESSION_ID,
            process=self.make_process("opencode_resume.jsonl"),
        )
        self.assertEqual(
            self.kinds(events), ["turn.started", "raw", "agent_message", "turn.completed"]
        )
        self.assertEqual(events[2]["text"], "PONG2")
        self.assertEqual(
            captured["argv"],
            [
                sys.executable,
                "run",
                "--standalone",
                "--format",
                "json",
                "--session",
                FAKE_SESSION_ID,
                "--",
                PROMPT,
            ],
        )

    def test_model_flag_is_passed_when_requested(self) -> None:
        _, captured, _ = self.run_stream(self.opencode(), model="openai/gpt-5.5")
        argv = captured["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "openai/gpt-5.5")

    def test_tool_call_and_step_usage(self) -> None:
        events, _, _ = self.run_stream(
            self.opencode(), process=self.make_process("opencode_tool.jsonl")
        )
        self.assertEqual(
            self.kinds(events),
            [
                "turn.started",
                "provider_session",
                "raw",  # step_start
                "raw",  # step_start
                "command",  # tool_use
                "raw",  # step_start
                "agent_message",
                "usage",
                "turn.completed",
            ],
        )
        self.assertEqual(self.text_of(events, "command"), "read /workspace/agent/probe.txt")
        self.assertEqual(self.text_of(events, "agent_message"), "PONG3")
        # usage приходит по шагам; в журнал отдаётся один итог в общих для пакета ключах.
        self.assertEqual([event for event in events if event["kind"] == "usage"][0]["usage"],
                         EXPECTED_OPENCODE_USAGE)
        # ...и он идёт до turn.completed, чтобы оркестратор взял именно итог.
        self.assertLess(
            self.kinds(events).index("usage"), self.kinds(events).index("turn.completed")
        )

    def test_error_envelope_becomes_error_event(self) -> None:
        events, _, _ = self.run_stream(
            self.opencode(), process=self.make_process("opencode_error.jsonl", returncode=1)
        )
        self.assertEqual(
            self.kinds(events), ["turn.started", "provider_session", "error", "turn.completed"]
        )
        self.assertEqual(self.text_of(events, "error"), "Model unavailable: no/such-model")
        self.assertEqual(events[-1]["exit_code"], 1)

    def test_unknown_event_type_is_kept_as_raw(self) -> None:
        process = FakeProcess(
            stdout_lines=[
                '{"type":"session.updated","sessionID":"ses-1","part":{"type":"future"}}'
            ]
        )
        events, _, _ = self.run_stream(self.opencode(), process=process)
        self.assertEqual(self.kinds(events), ["turn.started", "provider_session", "raw", "turn.completed"])
        self.assertEqual(events[2]["payload"]["type"], "session.updated")


# --- 3. недоступный провайдер ----------------------------------------------


class ProviderAvailabilityTests(AdapterTestCase):
    """Отсутствие бинаря — честное состояние, а не падение и не выдуманный ответ."""

    def test_codex_without_binary_is_unavailable(self) -> None:
        adapter = CodexAdapter(binary="/nonexistent/codex-not-here")
        state = adapter.availability()
        self.assertIsInstance(state, Availability)
        self.assertEqual(state.provider, "codex")
        self.assertFalse(state.available)
        self.assertIn("не найден", state.reason)
        self.assertIsNone(state.binary)
        self.assertIsNone(state.version)

    def test_start_and_resume_raise_provider_unavailable(self) -> None:
        adapter = CodexAdapter(binary="/nonexistent/codex-not-here")
        with self.assertRaises(ProviderUnavailable) as start_error:
            adapter.start(WORKSPACE, PROMPT)
        self.assertEqual(start_error.exception.provider, "codex")
        with self.assertRaises(ProviderUnavailable):
            adapter.resume(WORKSPACE, FAKE_THREAD_ID, PROMPT)

    def test_opencode_without_binary_is_unavailable(self) -> None:
        adapter = OpencodeAdapter(binary="/nonexistent/opencode-not-here")
        state = adapter.availability()
        self.assertFalse(state.available)
        self.assertEqual(state.provider, "opencode")
        with self.assertRaises(ProviderUnavailable):
            adapter.start(WORKSPACE, PROMPT)

    def test_search_ignores_machine_paths_when_asked(self) -> None:
        # Пустой PATH и никаких стандартных каталогов: поиск не зависит от этой машины.
        with mock.patch.dict(os.environ, {"PATH": ""}):
            adapter = CodexAdapter(search_paths=())
            self.assertIsNone(adapters_base.find_binary("codex", search_paths=()))
            state = adapter.availability()
        self.assertFalse(state.available)
        self.assertIn("не найден", state.reason)

    def test_available_binary_reports_version_and_path(self) -> None:
        adapter = CodexAdapter(binary=sys.executable)
        state = adapter.availability()
        self.assertTrue(state.available)
        self.assertEqual(state.binary, sys.executable)
        self.assertEqual(state.reason, "")
        self.assertTrue(str(state.version).startswith("Python"), state.version)

    def test_resume_without_session_id_is_rejected(self) -> None:
        adapter = CodexAdapter(binary=sys.executable)
        with self.assertRaises(ValueError):
            adapter.resume(WORKSPACE, "", PROMPT)


# --- 3a. авторизация провайдера (INTERFACE v1.4) -----------------------------


class ProviderAuthorizationTests(AdapterTestCase):
    """`authorization()` спрашивает состояние входа у самого CLI, не у каталогов провайдера."""

    @staticmethod
    def run_result(returncode: int, stdout: str = "", stderr: str = "") -> mock.Mock:
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_codex_logged_in(self) -> None:
        adapter = CodexAdapter(binary="/usr/bin/true")
        with mock.patch.object(
            adapters_base.subprocess,
            "run",
            return_value=self.run_result(0, "Logged in using ChatGPT\n"),
        ) as run:
            state = adapter.authorization()
        self.assertTrue(state.authorized)
        self.assertEqual(state.provider, "codex")
        self.assertEqual(state.how, "codex login")
        self.assertIn("Logged in", state.detail)
        self.assertEqual(
            run.call_args.args[0], ["/usr/bin/true", "login", "status"], "проверка штатной командой"
        )

    def test_codex_not_logged_in_is_recognized_before_substring(self) -> None:
        """«Not logged in» содержит «logged in»: отрицательная форма проверяется первой."""
        adapter = CodexAdapter(binary="/usr/bin/true")
        with mock.patch.object(
            adapters_base.subprocess,
            "run",
            return_value=self.run_result(1, "", "Not logged in"),
        ):
            state = adapter.authorization()
        self.assertFalse(state.authorized)

    def test_codex_other_nonzero_exit_is_not_authorized(self) -> None:
        adapter = CodexAdapter(binary="/usr/bin/true")
        with mock.patch.object(
            adapters_base.subprocess,
            "run",
            return_value=self.run_result(3, "", "unexpected failure"),
        ):
            state = adapter.authorization()
        self.assertFalse(state.authorized)
        self.assertIn("unexpected failure", state.detail)

    def test_codex_unknown_answer_is_honest_unknown(self) -> None:
        adapter = CodexAdapter(binary="/usr/bin/true")
        with mock.patch.object(
            adapters_base.subprocess,
            "run",
            return_value=self.run_result(0, "какой-то новый текст без узнаваемых слов"),
        ):
            state = adapter.authorization()
        self.assertIsNone(state.authorized, "неузнаваемый ответ — не «нет» и не «да»")
        self.assertIn("неизвестный ответ", state.detail)

    def test_auth_check_failure_is_unknown_not_error(self) -> None:
        adapter = CodexAdapter(binary="/usr/bin/true")
        with mock.patch.object(
            adapters_base.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=10),
        ):
            state = adapter.authorization()
        self.assertIsNone(state.authorized)
        self.assertIn("не выполнена", state.detail)

    def test_missing_binary_is_unknown_with_reason(self) -> None:
        adapter = CodexAdapter(binary="/nonexistent/codex-not-here")
        state = adapter.authorization()
        self.assertIsNone(state.authorized)
        self.assertIn("не найден", state.detail)
        self.assertEqual(state.how, "codex login")

    def test_opencode_stored_integration_is_authorized(self) -> None:
        adapter = OpencodeAdapter(binary="/usr/bin/true")
        with mock.patch.object(
            adapters_base.subprocess,
            "run",
            return_value=self.run_result(0, "OpenAI  OpenAI                      stored\n"),
        ) as run:
            state = adapter.authorization()
        self.assertTrue(state.authorized)
        self.assertEqual(state.how, "opencode auth login")
        self.assertEqual(
            run.call_args.args[0], ["/usr/bin/true", "auth", "list"]
        )

    def test_opencode_without_integrations_is_not_authorized(self) -> None:
        adapter = OpencodeAdapter(binary="/usr/bin/true")
        with mock.patch.object(
            adapters_base.subprocess, "run", return_value=self.run_result(0, "")
        ):
            state = adapter.authorization()
        self.assertFalse(state.authorized)
        self.assertIn("авторизованных интеграций нет", state.detail)

    def test_opencode_list_failure_is_unknown(self) -> None:
        adapter = OpencodeAdapter(binary="/usr/bin/true")
        with mock.patch.object(
            adapters_base.subprocess, "run", return_value=self.run_result(2, "", "boom")
        ):
            state = adapter.authorization()
        self.assertIsNone(state.authorized)
        self.assertIn("код 2", state.detail)

    def test_base_without_auth_args_is_unknown(self) -> None:
        """Провайдер без штатной команды проверки — честное «неизвестно», без процессов."""
        adapter = CodexAdapter(binary="/usr/bin/true")
        adapter.auth_args = ()
        with mock.patch.object(adapters_base.subprocess, "run") as run:
            state = adapter.authorization()
        self.assertIsNone(state.authorized)
        run.assert_not_called()

    def test_registry_authorization_covers_known_providers_and_never_raises(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": ""}):
            registry = AdapterRegistry()
            rows = {row.provider: row for row in registry.authorization()}
        self.assertEqual(sorted(rows), ["codex", "opencode"])
        for row in rows.values():
            self.assertIsNone(row.authorized, row)

    def test_stub_carries_authorization(self) -> None:
        adapter = StubAdapter(available=True, authorized=False, auth_detail="нет входа")
        state = adapter.authorization()
        self.assertFalse(state.authorized)
        self.assertEqual(state.detail, "нет входа")


# --- 3c. каталоги записи провайдера (INTERFACE v1.6, слой bwrap) --------------


class ProviderDataDirsTests(AdapterTestCase):
    """`data_dirs()` — что провайдеру нужно писать вне воркспейса (для bwrap-слоя)."""

    def test_codex_needs_its_home(self) -> None:
        dirs = CodexAdapter().data_dirs()
        self.assertEqual(dirs, [Path.home() / ".codex"])

    def test_opencode_needs_xdg_dirs(self) -> None:
        dirs = OpencodeAdapter().data_dirs()
        home = Path.home()
        self.assertEqual(
            dirs,
            [
                home / ".config" / "opencode",
                home / ".local" / "share" / "opencode",
                home / ".cache" / "opencode",
                home / ".local" / "state" / "opencode",
            ],
        )

    def test_stub_has_no_dirs(self) -> None:
        self.assertEqual(StubAdapter().data_dirs(), [])


# --- 3d. префикс песочницы (INTERFACE v1.5) ----------------------------------


class SandboxPrefixTests(AdapterTestCase):
    """`sandbox_prefix` оборачивает argv провайдера, cwd и события не меняются."""

    PREFIX = ["systemd-run", "--user", "--scope", "-pMemoryMax=2G", "-pTasksMax=128"]

    def test_start_argv_is_prefixed(self) -> None:
        adapter = CodexAdapter(binary=sys.executable)
        _, captured, _ = self.run_stream(adapter, sandbox_prefix=self.PREFIX)
        argv = captured["argv"]
        self.assertEqual(argv[: len(self.PREFIX)], self.PREFIX)
        # Провайдер идёт сразу после префикса, со своим обычным началом.
        self.assertEqual(argv[len(self.PREFIX) : len(self.PREFIX) + 2], [sys.executable, "exec"])
        # `--` и задача остаются в хвосте: префикс их не сдвигает.
        self.assertEqual(argv[-2:], ["--", PROMPT])
        self.assertEqual(captured["kwargs"]["cwd"], str(WORKSPACE))

    def test_resume_argv_is_prefixed(self) -> None:
        adapter = CodexAdapter(binary=sys.executable)
        _, captured, _ = self.run_stream(adapter, method="resume", sandbox_prefix=self.PREFIX)
        argv = captured["argv"]
        self.assertEqual(argv[: len(self.PREFIX)], self.PREFIX)
        self.assertIn("resume", argv)
        self.assertEqual(captured["kwargs"]["cwd"], str(WORKSPACE))

    def test_no_prefix_keeps_argv_unchanged(self) -> None:
        adapter = CodexAdapter(binary=sys.executable)
        _, captured, _ = self.run_stream(adapter)
        self.assertEqual(captured["argv"][0], sys.executable)

    def test_opencode_events_survive_prefix(self) -> None:
        adapter = OpencodeAdapter(binary=sys.executable)
        events, captured, _ = self.run_stream(
            adapter,
            process=self.make_process("opencode_start.jsonl"),
            sandbox_prefix=self.PREFIX,
        )
        argv = captured["argv"]
        self.assertEqual(argv[: len(self.PREFIX)], self.PREFIX)
        self.assertEqual(argv[len(self.PREFIX) :][0], sys.executable)
        kinds = [event["kind"] for event in events]
        self.assertIn("agent_message", kinds)

    def test_cleanup_stops_the_scope_unit(self) -> None:
        """После хода scope-юнит останавливается по имени из префикса: payload не сиротеет."""
        adapter = CodexAdapter(binary=sys.executable)
        prefix = [
            "systemd-run", "--user", "--scope", "--unit=runneos-agents-ab12cd34ef56",
            "-pMemoryMax=2G", "-pTasksMax=128",
        ]
        with mock.patch.object(
            adapters_base.subprocess, "run", return_value=mock.Mock(returncode=0)
        ) as systemctl_run:
            self.run_stream(adapter, sandbox_prefix=prefix)
        stopped = [
            call.args[0]
            for call in systemctl_run.call_args_list
            if call.args and call.args[0][:3] == ["systemctl", "--user", "stop"]
        ]
        self.assertEqual(stopped, [["systemctl", "--user", "stop", "runneos-agents-ab12cd34ef56.scope"]])

    def test_cleanup_skipped_without_unit_name(self) -> None:
        adapter = CodexAdapter(binary=sys.executable)
        with mock.patch.object(
            adapters_base.subprocess, "run", return_value=mock.Mock(returncode=0)
        ) as systemctl_run:
            self.run_stream(adapter, sandbox_prefix=["systemd-run", "--user", "--scope"])
        self.assertEqual(systemctl_run.call_args_list, [])


# --- 4-5. ненулевой код возврата, таймаут, сбой запуска --------------------


class ProcessFailureTests(AdapterTestCase):
    """Ошибки процесса становятся событиями; наружу исключения не летят."""

    def codex(self) -> CodexAdapter:
        return CodexAdapter(binary=sys.executable)

    def test_nonzero_exit_yields_error_event_without_raising(self) -> None:
        process = FakeProcess(
            stdout_lines=[
                f'{{"type":"thread.started","thread_id":"{FAKE_THREAD_ID}"}}',
                '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"частично"}}',
            ],
            stderr_lines=["провайдер: превышен лимит запросов"],
            returncode=1,
        )
        events, _, _ = self.run_stream(self.codex(), process=process)  # не бросает
        self.assertEqual(
            self.kinds(events), ["turn.started", "provider_session", "agent_message", "error",
                                 "turn.completed"]
        )
        error = self.text_of(events, "error")
        self.assertIn("кодом 1", error)
        self.assertIn("превышен лимит запросов", error)
        self.assertEqual(events[-1]["exit_code"], 1)

    def test_nonzero_exit_does_not_duplicate_envelope_error(self) -> None:
        process = FakeProcess(
            stdout_lines=['{"type":"error","message":"провайдер отказал"}'],
            stderr_lines=["подробности на стороне провайдера"],
            returncode=1,
        )
        events, _, _ = self.run_stream(self.codex(), process=process)
        errors = [event for event in events if event["kind"] == "error"]
        self.assertEqual(len(errors), 1)
        # Текст провайдера точнее общего «код возврата»: он и остаётся ошибкой хода.
        self.assertEqual(errors[0]["text"], "провайдер отказал")
        self.assertEqual(events[-1]["exit_code"], 1)

    def test_timeout_kills_process_and_reports_error(self) -> None:
        process = FakeProcess(
            stdout_lines=[f'{{"type":"thread.started","thread_id":"{FAKE_THREAD_ID}"}}'],
            hang_stdout=True,
        )
        started = time.monotonic()
        events, _, _ = self.run_stream(self.codex(), process=process, timeout=0.3)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10.0, "таймаут не сработал: генератор ждал процесс")
        self.assertTrue(
            signal.SIGTERM in process.signals or signal.SIGKILL in process.signals,
            f"процесс не убит: {process.signals}",
        )
        self.assertIn("error", self.kinds(events))
        self.assertIn("таймаут", self.text_of(events, "error"))
        # Ход завершён, но кода возврата у убитого процесса нет.
        self.assertEqual(events[-1]["kind"], "turn.completed")
        self.assertIsNone(events[-1]["exit_code"])

    def test_spawn_error_becomes_error_event(self) -> None:
        adapter = self.codex()
        with mock.patch.object(
            adapters_base.subprocess, "Popen", side_effect=OSError("Exec format error")
        ), mock.patch.object(adapters_base, "probe_version", return_value="fixture-1.0"):
            events = list(adapter.start(WORKSPACE, PROMPT))
        self.assertEqual(self.kinds(events), ["turn.started", "error", "turn.completed"])
        self.assertIn("Exec format error", self.text_of(events, "error"))

    def test_non_json_stdout_line_is_kept_as_raw(self) -> None:
        process = FakeProcess(
            stdout_lines=[
                "предупреждение провайдера, не JSON",
                f'{{"type":"thread.started","thread_id":"{FAKE_THREAD_ID}"}}',
            ]
        )
        events, _, _ = self.run_stream(self.codex(), process=process)
        self.assertIn("raw", self.kinds(events))
        raw = next(event for event in events if event["kind"] == "raw")
        self.assertEqual(raw["payload"]["stdout"], "предупреждение провайдера, не JSON")


# --- 6. секреты -------------------------------------------------------------


class SecretRedactionTests(AdapterTestCase):
    """Секреты не попадают ни в события, ни в логи, ни в аргументы, которые адаптер пишет."""

    def test_env_secret_absent_from_events_and_logs(self) -> None:
        adapter = CodexAdapter(binary=sys.executable, env={"OPENAI_API_KEY": SECRET})
        process = FakeProcess(
            stdout_lines=[
                f'{{"type":"thread.started","thread_id":"{FAKE_THREAD_ID}"}}',
                '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"готово"}}',
                '{"type":"item.completed","item":{"id":"item_1","type":"future_kind",'
                f'"text":"сырое событие с {SECRET}"}}}}',
            ],
            stderr_lines=[f"авторизация отклонена: {SECRET}"],
            returncode=1,
        )
        with self.assertLogs("runneos_agents.adapters", level="DEBUG") as logs:
            events, captured, _ = self.run_stream(adapter, process=process, model=SECRET)

        # 1. Ни одно событие не содержит значения секрета (включая raw и текст ошибки).
        blob = json.dumps(events, ensure_ascii=False)
        self.assertNotIn(SECRET, blob)
        self.assertIn(adapters_base.MASK, blob)
        # 2. Ни одна записанная адаптером строка лога.
        self.assertNotIn(SECRET, "\n".join(logs.output))
        # 3. При этом секрет реально был и в окружении процесса, и в argv (модель = секрет):
        #    проверка не вырождена, скрывать было что.
        self.assertEqual(captured["kwargs"]["env"]["OPENAI_API_KEY"], SECRET)
        self.assertIn(SECRET, captured["argv"])

    def test_env_secret_absent_when_provider_is_unavailable(self) -> None:
        adapter = CodexAdapter(binary="/nonexistent/codex-not-here", env={"API_TOKEN": SECRET})
        state = adapter.availability()
        self.assertNotIn(SECRET, json.dumps(state.__dict__, ensure_ascii=False, default=str))
        with self.assertRaises(ProviderUnavailable) as error:
            adapter.start(WORKSPACE, PROMPT)
        self.assertNotIn(SECRET, str(error.exception))

    def test_collect_secrets_filters_by_name_and_length(self) -> None:
        env = {
            "PATH": "/usr/bin",
            "SHORT_KEY": "abc",  # слишком короткое значение
            "OPENAI_API_KEY": SECRET,
            "GITHUB_TOKEN": "ghp_" + "a" * 30,
        }
        secrets = adapters_base.collect_secrets(env)
        self.assertIn(SECRET, secrets)
        self.assertIn("ghp_" + "a" * 30, secrets)
        self.assertNotIn("abc", secrets)
        self.assertNotIn("/usr/bin", secrets)

    def test_known_token_shapes_are_masked_even_without_env(self) -> None:
        text = "ключ sk-abcdef123456 и заголовок Bearer abcdefghijklmnop"
        masked = adapters_base.redact(text)
        self.assertNotIn("sk-abcdef123456", masked)
        self.assertNotIn("abcdefghijklmnop", masked)
        self.assertIn(adapters_base.MASK, masked)

    def test_redact_object_walks_nested_structures(self) -> None:
        payload = {"items": [{"text": SECRET}], "nested": {"deep": [SECRET, 1, True]}, "n": 5}
        masked = adapters_base.redact_object(payload, (SECRET,))
        self.assertNotIn(SECRET, json.dumps(masked, ensure_ascii=False))
        self.assertEqual(masked["n"], 5)  # не-строки не трогаем

    def test_redact_argv_masks_values(self) -> None:
        argv = ["codex", "exec", "-m", SECRET, "--", "привет"]
        masked = adapters_base.redact_argv(argv, (SECRET,))
        self.assertNotIn(SECRET, masked)
        self.assertEqual(masked[-1], "привет")


# --- заглушка ---------------------------------------------------------------


class StubAdapterTests(unittest.TestCase):
    """Фиктивный адаптер: детерминированные события для тестов ядра и панели."""

    def test_availability_and_events(self) -> None:
        adapter = StubAdapter(reply="готово", provider_session_id="stub-1")
        state = adapter.availability()
        self.assertTrue(state.available)
        self.assertEqual(state.provider, "stub")
        events = list(adapter.start(WORKSPACE, "привет"))
        self.assertEqual(
            [event["kind"] for event in events],
            ["provider_session", "turn.started", "agent_message", "turn.completed"],
        )
        self.assertEqual(events[0]["provider_session_id"], "stub-1")
        self.assertEqual(events[2]["text"], "готово")

    def test_unavailable_stub_raises(self) -> None:
        adapter = StubAdapter(available=False, reason="нет ключа провайдера")
        state = adapter.availability()
        self.assertFalse(state.available)
        self.assertEqual(state.reason, "нет ключа провайдера")
        with self.assertRaises(ProviderUnavailable) as error:
            adapter.start(WORKSPACE, "привет")
        self.assertEqual(error.exception.reason, "нет ключа провайдера")
        with self.assertRaises(ProviderUnavailable):
            adapter.resume(WORKSPACE, "stub-1", "привет")

    def test_calls_are_recorded(self) -> None:
        adapter = StubAdapter()
        list(adapter.start(WORKSPACE, "первая", model="m-1", instructions="роль", timeout=5))
        list(adapter.resume(WORKSPACE, "stub-9", "вторая"))
        self.assertEqual([call["method"] for call in adapter.calls], ["start", "resume"])
        self.assertEqual(adapter.calls[0]["prompt"], "первая")
        self.assertEqual(adapter.calls[0]["instructions"], "роль")
        self.assertEqual(adapter.calls[0]["model"], "m-1")
        self.assertEqual(adapter.calls[0]["timeout"], 5)
        self.assertEqual(adapter.calls[1]["provider_session_id"], "stub-9")
        self.assertEqual(adapter.last_call["prompt"], "вторая")

    def test_custom_events_replace_scenario(self) -> None:
        adapter = StubAdapter(events=[{"kind": "agent_message", "text": "свой сценарий"}])
        events = list(adapter.start(WORKSPACE, "привет"))
        self.assertEqual(events, [{"kind": "agent_message", "text": "свой сценарий"}])

    def test_error_scenario_is_reported(self) -> None:
        adapter = StubAdapter(error="провайдер недоступен")
        kinds = [event["kind"] for event in adapter.start(WORKSPACE, "привет")]
        self.assertEqual(kinds, ["provider_session", "turn.started", "error", "agent_message",
                                 "turn.completed"])


class OrchestratorRoundTripTests(unittest.TestCase):
    """События адаптера должны доезжать до ядра: лента, provider_session_id, usage, ok."""

    def test_stub_round_trip_through_orchestrator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "agents.db")
            try:
                store.upsert_agent(Agent(id="tester", name="Тестер", provider="stub"))
                stub = StubAdapter(
                    reply="готово",
                    provider_session_id="stub-1",
                    usage={"input_tokens": 3, "output_tokens": 2},
                )
                result = Orchestrator(store, adapters=stub.as_registry()).ask("tester", "привет")
                self.assertTrue(result.ok)
                self.assertEqual(result.text, "готово")
                self.assertEqual(result.provider_session_id, "stub-1")
                self.assertEqual(result.usage, {"input_tokens": 3, "output_tokens": 2})

                session = store.latest_open_session("tester")
                kinds = [event.kind for event in store.list_events(session.id)]
                self.assertIn("agent_message", kinds)
                self.assertIn("provider_session", kinds)
                self.assertIn("user_message", kinds)
                roles = [message.role for message in store.list_messages(session.id)]
                self.assertEqual(roles, ["user", "agent"])
            finally:
                store.close()

    def test_stub_error_makes_turn_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "agents.db")
            try:
                store.upsert_agent(Agent(id="tester", name="Тестер", provider="stub"))
                stub = StubAdapter(error="провайдер отказал", reply=None)
                result = Orchestrator(store, adapters=stub.as_registry()).ask("tester", "привет")
                self.assertFalse(result.ok)
                self.assertEqual(result.error, "провайдер отказал")
            finally:
                store.close()

    def test_unavailable_provider_does_not_start_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "agents.db")
            try:
                store.upsert_agent(Agent(id="tester", name="Тестер", provider="stub"))
                stub = StubAdapter(available=False, reason="нет ключа")
                result = Orchestrator(store, adapters=stub.as_registry()).ask("tester", "привет")
                self.assertFalse(result.ok)
                self.assertEqual(result.error, "нет ключа")
                self.assertEqual(stub.calls, [])  # процесс не запускался
            finally:
                store.close()


# --- реестр -----------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    """Реестр: имена провайдеров как в ядре, честная недоступность для чужих имён."""

    def test_build_registry_registers_known_providers(self) -> None:
        registry = build_registry()
        self.assertIsInstance(registry, AdapterRegistry)
        self.assertEqual(registry.providers(), ["codex", "opencode"])
        self.assertEqual(provider_names(), ("codex", "opencode"))
        self.assertEqual(registry.get("codex").provider, "codex")
        self.assertEqual(registry.get("opencode").provider, "opencode")

    def test_core_routing_picks_up_adapters(self) -> None:
        registry = default_registry()
        self.assertEqual(registry.providers(), ["codex", "opencode"])
        states = {state.provider: state for state in registry.availability()}
        self.assertEqual(set(states), {"codex", "opencode"})
        for state in states.values():
            self.assertIsInstance(state, Availability)
            self.assertTrue(state.reason or state.available)

    def test_unknown_provider_is_unavailable_not_crash(self) -> None:
        state = default_registry().get("нет-такого").availability()
        self.assertFalse(state.available)
        with self.assertRaises(ProviderUnavailable):
            get_adapter("нет-такого")

    def test_available_lists_every_configured_provider(self) -> None:
        states = available()
        self.assertEqual([state.provider for state in states], ["codex", "opencode"])

    def test_get_adapter_returns_working_adapter(self) -> None:
        adapter = get_adapter("codex")
        self.assertEqual(adapter.provider, "codex")

    def test_availability_of_adapters_never_raises(self) -> None:
        registry = default_registry()
        with mock.patch.dict(os.environ, {"PATH": ""}):
            states = registry.availability()
        self.assertEqual(len(states), 2)
        for state in states:
            self.assertIsInstance(state.available, bool)


class ContractSurfaceTests(AdapterTestCase):
    """Сигнатуры из раздела 5 контракта заморожены: проверяем их буквально."""

    def adapters_for_contract(self) -> tuple[object, ...]:
        return (
            CodexAdapter(binary=sys.executable),
            OpencodeAdapter(binary=sys.executable),
            StubAdapter(),
        )

    def test_availability_is_the_core_dataclass(self) -> None:
        expected = ["provider", "available", "reason", "version", "binary"]
        self.assertEqual([field.name for field in dataclasses.fields(Availability)], expected)
        state = StubAdapter().availability()
        self.assertEqual(
            [getattr(state, name) for name in expected],
            ["stub", True, "", "stub-0", None],
        )
        # ProviderUnavailable живёт в ядре: адаптеры бросают именно его.
        self.assertIs(
            sys.modules[ProviderUnavailable.__module__].ProviderUnavailable, ProviderUnavailable
        )

    def test_start_signature(self) -> None:
        for adapter in self.adapters_for_contract():
            with self.subTest(provider=adapter.provider):  # type: ignore[attr-defined]
                signature = inspect.signature(adapter.start)  # type: ignore[attr-defined]
                self.assertEqual(
                    list(signature.parameters),
                    ["ws", "prompt", "model", "instructions", "timeout", "sandbox_prefix"],
                )
                for name, default in (
                    ("model", None),
                    ("instructions", ""),
                    ("timeout", None),
                    ("sandbox_prefix", None),
                ):
                    parameter = signature.parameters[name]
                    self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
                    self.assertEqual(parameter.default, default)

    def test_resume_signature(self) -> None:
        for adapter in self.adapters_for_contract():
            with self.subTest(provider=adapter.provider):  # type: ignore[attr-defined]
                signature = inspect.signature(adapter.resume)  # type: ignore[attr-defined]
                self.assertEqual(
                    list(signature.parameters),
                    ["ws", "provider_session_id", "prompt", "model", "timeout", "sandbox_prefix"],
                )
                for name in ("model", "timeout", "sandbox_prefix"):
                    parameter = signature.parameters[name]
                    self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
                    self.assertIsNone(parameter.default)

    def test_availability_takes_no_arguments(self) -> None:
        for adapter in self.adapters_for_contract():
            with self.subTest(provider=adapter.provider):  # type: ignore[attr-defined]
                self.assertEqual(list(inspect.signature(adapter.availability).parameters), [])
                self.assertIsInstance(adapter.provider, str)  # type: ignore[attr-defined]

    def test_data_dirs_take_no_arguments(self) -> None:
        for adapter in self.adapters_for_contract():
            with self.subTest(provider=adapter.provider):  # type: ignore[attr-defined]
                self.assertEqual(list(inspect.signature(adapter.data_dirs).parameters), [])
                self.assertIsInstance(adapter.data_dirs(), list)  # type: ignore[attr-defined]

    def test_normalized_event_kinds_stay_in_contract(self) -> None:
        """Адаптеры отдают только виды из раздела 5 плюс turn.completed из словаря Event.kind."""
        allowed = {
            "turn.started",
            "agent_message",
            "reasoning",
            "command",
            "file_change",
            "usage",
            "provider_session",
            "error",
            "raw",
            "turn.completed",
        }
        for name in FixtureIntegrityTests.EXPECTED:
            for adapter in (CodexAdapter(binary=sys.executable), OpencodeAdapter(binary=sys.executable)):
                with self.subTest(fixture=name, provider=adapter.provider):
                    events, _, _ = self.run_stream(adapter, process=self.make_process(name))
                    self.assertTrue(set(self.kinds(events)) <= allowed, self.kinds(events))


# --- живой прогон (по умолчанию выключён) -----------------------------------


@unittest.skipUnless(
    os.environ.get("RUNNEOS_LIVE_TESTS") == "1",
    "живой прогон провайдера: включить RUNNEOS_LIVE_TESTS=1",
)
class LiveProviderTests(unittest.TestCase):
    """Настоящие CLI: короткий запрос, проверка thread_id и текста ответа."""

    def test_codex_start_and_resume(self) -> None:
        adapter = CodexAdapter()
        state = adapter.availability()
        if not state.available:
            self.skipTest(f"codex недоступен: {state.reason}")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first = list(adapter.start(workspace, "ответь ровно PONG", timeout=180))
            text = "".join(
                str(event.get("text") or "") for event in first if event["kind"] == "agent_message"
            )
            self.assertIn("PONG", text)
            sessions = [
                event["provider_session_id"]
                for event in first
                if event["kind"] == "provider_session"
            ]
            self.assertTrue(sessions, f"нет provider_session: {first}")
            self.assertFalse([event for event in first if event["kind"] == "error"], first)

            second = list(adapter.resume(workspace, sessions[0], "ответь ровно PONG2", timeout=180))
            text2 = "".join(
                str(event.get("text") or "") for event in second if event["kind"] == "agent_message"
            )
            self.assertIn("PONG2", text2)
            self.assertEqual(second[-1]["exit_code"], 0)

    def test_opencode_start_and_resume(self) -> None:
        adapter = OpencodeAdapter()
        state = adapter.availability()
        if not state.available:
            self.skipTest(f"opencode недоступен: {state.reason}")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first = list(adapter.start(workspace, "ответь ровно PONG", timeout=180))
            text = "".join(
                str(event.get("text") or "") for event in first if event["kind"] == "agent_message"
            )
            self.assertIn("PONG", text)
            sessions = [
                event["provider_session_id"]
                for event in first
                if event["kind"] == "provider_session"
            ]
            self.assertTrue(sessions, f"нет provider_session: {first}")

            second = list(adapter.resume(workspace, sessions[0], "ответь ровно PONG2", timeout=180))
            text2 = "".join(
                str(event.get("text") or "") for event in second if event["kind"] == "agent_message"
            )
            self.assertIn("PONG2", text2)


if __name__ == "__main__":
    unittest.main()
