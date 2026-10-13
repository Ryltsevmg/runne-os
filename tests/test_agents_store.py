"""Тесты хранилища агентов RunneOS: схема, журнал, каскады, изоляция данных.

Запуск: PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_agents_*.py' -v
"""

import sqlite3
import sys
import tempfile
import threading
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

from runneos_agents.model import Agent, now_iso  # noqa: E402
from runneos_agents.store import SCHEMA_VERSION, Store, default_root  # noqa: E402


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = Store(self.root / "agents.db")
        self.addCleanup(self.store.close)
        self.store.upsert_agent(Agent(id="tester", name="Тестер", role="проверка"))

    def test_schema_version_recorded(self):
        self.assertEqual(self.store.stats()["schema_version"], SCHEMA_VERSION)

    def test_root_created(self):
        self.assertTrue((self.root / "agents.db").exists())
        self.assertTrue(self.root.is_dir())


class AgentStoreTests(StoreTestCase):
    def test_upsert_keeps_created_at(self):
        first = self.store.get_agent("tester")
        again = self.store.upsert_agent(Agent(id="tester", name="Тестер 2", role="новая"))
        self.assertEqual(again.created_at, first.created_at)
        self.assertEqual(again.name, "Тестер 2")
        self.assertEqual(again.role, "новая")

    def test_list_sorted_by_name(self):
        self.store.upsert_agent(Agent(id="alpha", name="Альфа"))
        self.store.upsert_agent(Agent(id="beta", name="Бета"))
        names = [agent.id for agent in self.store.list_agents()]
        self.assertEqual(names, ["alpha", "beta", "tester"])

    def test_unknown_agent_is_none(self):
        self.assertIsNone(self.store.get_agent("нет-такого"))

    def test_state_change_rejects_unknown_state(self):
        with self.assertRaises(ValueError):
            self.store.set_agent_state("tester", "летит")
        self.store.set_agent_state("tester", "running")
        self.assertEqual(self.store.get_agent("tester").state, "running")

    def test_remove_agent_cascades(self):
        session = self.store.create_session("tester")
        self.store.add_message(session.id, "user", "привет")
        self.store.append_event(session.id, "user_message", {"text": "привет"})
        self.assertTrue(self.store.remove_agent("tester"))
        self.assertIsNone(self.store.get_session(session.id))
        self.assertEqual(self.store.list_messages(session.id), [])
        self.assertEqual(self.store.list_events(session.id), [])
        self.assertFalse(self.store.remove_agent("tester"))


class SessionStoreTests(StoreTestCase):
    def test_session_for_unknown_agent_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_session("нет-такого")

    def test_latest_open_session_ignores_closed(self):
        first = self.store.create_session("tester", "первая")
        self.store.set_session_state(first.id, "closed")
        second = self.store.create_session("tester", "вторая")
        self.assertEqual(self.store.latest_open_session("tester").id, second.id)

    def test_provider_session_id_persisted(self):
        session = self.store.create_session("tester")
        self.store.set_provider_session_id(session.id, "thread-42")
        stored = self.store.get_session(session.id)
        self.assertEqual(stored.provider_session_id, "thread-42")
        self.assertTrue(stored.resumable)

    def test_list_sessions_respects_agent(self):
        self.store.upsert_agent(Agent(id="other", name="Другой"))
        self.store.create_session("tester")
        self.store.create_session("other")
        self.assertEqual(len(self.store.list_sessions("tester")), 1)
        self.assertEqual(len(self.store.list_sessions()), 2)


class JournalTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.session = self.store.create_session("tester")

    def test_sequence_is_monotonic(self):
        for index in range(5):
            event = self.store.append_event(self.session.id, "test", {"n": index})
            self.assertEqual(event.seq, index + 1)
        self.assertEqual([e.seq for e in self.store.list_events(self.session.id)], [1, 2, 3, 4, 5])

    def test_events_after_returns_tail(self):
        for index in range(4):
            self.store.append_event(self.session.id, "test", {"n": index})
        tail = self.store.events_after(self.session.id, 2)
        self.assertEqual([event.seq for event in tail], [3, 4])

    def test_payload_round_trip(self):
        self.store.append_event(self.session.id, "agent_message", {"text": "PONG", "n": 1})
        event = self.store.list_events(self.session.id)[0]
        self.assertEqual(event.payload["text"], "PONG")
        self.assertEqual(event.payload["n"], 1)

    def test_sequence_unique_under_concurrency(self):
        """Два писателя не должны получить одинаковый seq."""
        errors: list[BaseException] = []

        def writer():
            try:
                other = Store(self.store.path)
                try:
                    for index in range(10):
                        other.append_event(self.session.id, "concurrent", {"n": index})
                finally:
                    other.close()
            except BaseException as error:  # noqa: BLE001 — тест фиксирует любую ошибку
                errors.append(error)

        threads = [threading.Thread(target=writer) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        events = self.store.list_events(self.session.id, limit=100)
        self.assertEqual(sorted(event.seq for event in events), list(range(1, 21)))

    def test_message_links_to_event(self):
        event = self.store.append_event(self.session.id, "agent_message", {"text": "PONG"})
        message = self.store.add_message(self.session.id, "agent", "PONG", event_id=event.id)
        self.assertEqual(message.event_id, event.id)
        self.assertEqual(len(self.store.list_messages(self.session.id)), 1)

    def test_journal_is_per_session(self):
        other = self.store.create_session("tester", "другая")
        self.store.append_event(self.session.id, "test", {})
        self.store.append_event(other.id, "test", {})
        self.assertEqual(len(self.store.list_events(self.session.id)), 1)
        self.assertEqual(len(self.store.list_events(other.id)), 1)

    def test_foreign_keys_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append_event("нет-такой-сессии", "test", {})


class SessionScopedSequenceTests(StoreTestCase):
    """seq нумеруется внутри сессии, а не глобально по таблице events.

    Контракт INTERFACE.md: `append_event` берёт max+1 ВНУТРИ сессии, поэтому у
    каждой сессии своя нумерация с 1. Глобальный счётчик тоже даёт монотонные и
    уникальные внутри сессии номера — но вторая сессия продолжила бы нумерацию
    первой, и курсор events_after/журнал UI поехали бы.
    """

    def setUp(self):
        super().setUp()
        self.session = self.store.create_session("tester", "первая")

    def test_each_session_starts_numbering_from_one(self):
        other = self.store.create_session("tester", "вторая")
        self.assertEqual(
            [self.store.append_event(self.session.id, "test", {"n": index}).seq for index in range(3)],
            [1, 2, 3],
        )
        self.assertEqual(
            [self.store.append_event(other.id, "test", {"n": index}).seq for index in range(2)],
            [1, 2],
        )
        self.assertEqual([event.seq for event in self.store.list_events(self.session.id)], [1, 2, 3])
        self.assertEqual([event.seq for event in self.store.list_events(other.id)], [1, 2])

    def test_numbering_restarts_for_another_agents_session(self):
        """Нумерация не зависит ни от агента, ни от уже накопленных событий."""
        self.store.upsert_agent(Agent(id="other", name="Другой"))
        other_agent_session = self.store.create_session("other")
        for index in range(4):
            self.store.append_event(self.session.id, "test", {"n": index})
        self.assertEqual(self.store.append_event(other_agent_session.id, "test", {}).seq, 1)
        self.assertEqual(self.store.append_event(other_agent_session.id, "test", {}).seq, 2)

    def test_interleaved_sessions_keep_independent_sequences(self):
        other = self.store.create_session("tester", "вторая")
        written = [
            (self.session.id, 1),
            (other.id, 1),
            (self.session.id, 2),
            (other.id, 2),
        ]
        self.assertEqual(
            [self.store.append_event(session_id, "test", {}).seq for session_id, _ in written],
            [expected for _, expected in written],
        )
        rows = self.store.connection.execute(
            "SELECT session_id, seq FROM events ORDER BY id"
        ).fetchall()
        self.assertEqual(
            sorted((row["session_id"], row["seq"]) for row in rows),
            sorted((session_id, expected) for session_id, expected in written),
        )


class DefaultsTests(unittest.TestCase):
    def test_default_root_follows_xdg(self):
        import os

        previous = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = "/tmp/runneos-agents-test-xdg"
        try:
            self.assertEqual(str(default_root()), "/tmp/runneos-agents-test-xdg/runneos/agents")
        finally:
            if previous is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = previous

    def test_explicit_home_wins(self):
        import os

        previous = os.environ.get("RUNNEOS_AGENTS_HOME")
        os.environ["RUNNEOS_AGENTS_HOME"] = "/tmp/runneos-agents-home"
        try:
            self.assertEqual(str(default_root()), "/tmp/runneos-agents-home")
        finally:
            if previous is None:
                os.environ.pop("RUNNEOS_AGENTS_HOME", None)
            else:
                os.environ["RUNNEOS_AGENTS_HOME"] = previous

    def test_timestamp_format(self):
        self.assertTrue(now_iso().endswith("Z"))


if __name__ == "__main__":
    unittest.main()
