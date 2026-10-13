"""Тесты модели агентов RunneOS: чистые структуры, без ввода-вывода.

Запуск: PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_agents_*.py' -v
"""

import sys
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
REPO = PACKAGE_DIR.parents[2] if len(PACKAGE_DIR.parents) > 2 else PACKAGE_DIR

from runneos_agents.model import (  # noqa: E402
    Agent,
    Availability,
    Event,
    Message,
    ProviderUnavailable,
    Session,
    TurnResult,
    new_agent_id,
    now_iso,
    validate_agent_id,
)


class AgentTests(unittest.TestCase):
    def test_minimal_agent(self):
        agent = Agent(id="tester", name="Тестер")
        self.assertEqual(agent.provider, "codex")
        self.assertEqual(agent.state, "idle")
        self.assertIsNone(agent.workspace)
        self.assertEqual(agent.role, "")

    def test_invalid_id_rejected(self):
        for bad in ("", "Tester", "тестер", "-tester", "a" * 65, "tester space"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    Agent(id=bad, name="Тестер")

    def test_invalid_state_rejected(self):
        with self.assertRaises(ValueError):
            Agent(id="tester", name="Тестер", state="работает")

    def test_empty_name_rejected(self):
        with self.assertRaises(ValueError):
            Agent(id="tester", name="   ")

    def test_replace_keeps_created_at(self):
        agent = Agent(id="tester", name="Тестер", created_at="2026-10-12T00:00:00Z")
        changed = agent.replace(state="running")
        self.assertEqual(changed.created_at, agent.created_at)
        self.assertEqual(changed.state, "running")
        self.assertEqual(agent.state, "idle")


class SessionTests(unittest.TestCase):
    def test_resumable_needs_provider_id(self):
        session = Session(id="s1", agent_id="tester")
        self.assertFalse(session.resumable)
        session = Session(id="s1", agent_id="tester", provider_session_id="thread-1")
        self.assertTrue(session.resumable)

    def test_error_state_not_resumable(self):
        session = Session(
            id="s1", agent_id="tester", provider_session_id="thread-1", state="error"
        )
        self.assertFalse(session.resumable)

    def test_invalid_state_rejected(self):
        with self.assertRaises(ValueError):
            Session(id="s1", agent_id="tester", state="закрыта")


class MessageAndEventTests(unittest.TestCase):
    def test_unknown_message_role_rejected(self):
        with self.assertRaises(ValueError):
            Message(session_id="s1", role="assistant", text="привет")

    def test_event_needs_kind(self):
        with self.assertRaises(ValueError):
            Event(session_id="s1", kind="  ")

    def test_unknown_value_stays_none(self):
        """Правило проекта: неизвестное значение — None, а не 0."""
        result = TurnResult(ok=False, session_id="s1", error="нет провайдера")
        self.assertIsNone(result.usage)
        self.assertIsNone(result.provider_session_id)
        self.assertIsNone(result.exit_code)
        self.assertEqual(result.text, "")
        self.assertTrue(result.failed)


class NamingTests(unittest.TestCase):
    def test_slug_from_latin(self):
        self.assertEqual(new_agent_id("Review Bot"), "review-bot")
        self.assertEqual(new_agent_id("  A  B  "), "a-b")

    def test_slug_from_cyrillic_is_stable(self):
        first = new_agent_id("Проверяющий агент")
        second = new_agent_id("Проверяющий агент")
        self.assertEqual(first, second)
        validate_agent_id(first)
        self.assertTrue(first.startswith("agent-"))

    def test_validate_rejects_bad(self):
        for bad in ("", "A", "a" * 65, "a_b"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_agent_id(bad)


class AvailabilityTests(unittest.TestCase):
    def test_unavailable_keeps_reason(self):
        item = Availability(provider="opencode", available=False, reason="нет режима")
        self.assertFalse(item.available)
        self.assertEqual(item.reason, "нет режима")
        self.assertIsNone(item.version)

    def test_provider_unavailable_carries_provider(self):
        error = ProviderUnavailable("codex", "бинарь не найден")
        self.assertEqual(error.provider, "codex")
        self.assertEqual(error.reason, "бинарь не найден")
        self.assertIn("бинарь не найден", str(error))


class TimeTests(unittest.TestCase):
    def test_now_iso_is_utc_z(self):
        stamp = now_iso()
        self.assertTrue(stamp.endswith("Z"), stamp)
        self.assertEqual(len(stamp), 20, stamp)


if __name__ == "__main__":
    unittest.main()
