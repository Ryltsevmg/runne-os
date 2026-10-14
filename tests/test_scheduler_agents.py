"""Тесты планировщика расписаний агентов (runneos-agents-scheduler).

Логика без процессов: созревание задач, конфиг с правами 0600, исполнение через
подменный CLI, уведомления через подменный notify-send.

Запуск: PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_scheduler_agents.py' -v
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "packages" / "runneos-agents-scheduler" / "files" / "runneos-agents-scheduler"


def load_module(name: str = "runneos_agents_scheduler"):
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


MODULE = load_module()

NOW = datetime(2026, 10, 13, 10, 0, 0)


def schedule(**overrides) -> dict:
    base = {
        "id": "probe",
        "agent": "helper",
        "when": {"every_minutes": 30},
        "prompt": "проверь",
        "notify": "failure",
        "enabled": True,
        "last_run": None,
        "last_status": None,
    }
    base.update(overrides)
    return base


class ParseWhenTests(unittest.TestCase):
    def test_understands_both_forms(self):
        self.assertEqual(MODULE.parse_when("every 30m"), {"every_minutes": 30})
        self.assertEqual(MODULE.parse_when("daily 09:30"), {"daily": "09:30"})

    def test_rejects_garbage(self):
        for bad in ("", "every", "every 0m", "daily 25:00", "hourly", "daily 9:70"):
            self.assertIsNone(MODULE.parse_when(bad), bad)


class DueTests(unittest.TestCase):
    def test_every_interval_needs_elapsed_time(self):
        last = NOW.replace(minute=35)
        self.assertTrue(MODULE.is_due(schedule(last_run=last.isoformat()), NOW.replace(minute=5, hour=11)))
        self.assertFalse(MODULE.is_due(schedule(last_run=last.isoformat()), NOW.replace(hour=11, minute=4)))
        self.assertTrue(MODULE.is_due(schedule(last_run=None), NOW), "первый запуск — сразу")

    def test_disabled_never_due(self):
        self.assertFalse(MODULE.is_due(schedule(enabled=False), NOW))

    def test_daily_boundary(self):
        task = schedule(when={"daily": "09:30"})
        self.assertTrue(MODULE.is_due(task, NOW), "после 09:30 — дозрела")
        self.assertFalse(MODULE.is_due(task, NOW.replace(hour=9, minute=29)), "до рубежа — нет")
        ran_today = schedule(when={"daily": "09:30"}, last_run=NOW.replace(hour=9, minute=31).isoformat())
        self.assertFalse(MODULE.is_due(ran_today, NOW), "сегодня уже выполнялась")
        yesterday = schedule(when={"daily": "09:30"}, last_run=NOW.replace(day=12, hour=9, minute=31).isoformat())
        self.assertTrue(MODULE.is_due(yesterday, NOW), "вчера — снова дозрела")

    def test_unknown_when_is_never_due(self):
        self.assertFalse(MODULE.is_due(schedule(when={"crontab": "*"}), NOW))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = Path(self._tmp.name) / "runneos" / "agents-schedules.json"

    def test_roundtrip_and_private_mode(self):
        self.assertEqual(MODULE.load_schedules(self.config), ([], ""))
        self.assertTrue(MODULE.save_schedules([schedule()], self.config))
        rows, error = MODULE.load_schedules(self.config)
        self.assertEqual(error, "")
        self.assertEqual(rows[0]["id"], "probe")
        mode = self.config.stat().st_mode
        self.assertEqual(mode & 0o077, 0, "конфиг со статусами не должен быть читаем другим")

    def test_broken_config_is_honest_error(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_text("{не json", encoding="utf-8")
        rows, error = MODULE.load_schedules(self.config)
        self.assertEqual(rows, [])
        self.assertIn("не разбирается", error)


class RunDueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = Path(self._tmp.name) / "schedules.json"
        self.notifications: list[tuple] = []

    def notify(self, *args):
        """Ловит (notifier, title, body) как их зовёт run_due."""
        self.notifications.append(args)

    def fake_cli(self, ok=True, text="готово", error=None):
        """Подменный runneos-agents: печатает JSON-ответ ask."""
        payload = {"ok": ok, "text": text, "error": error}
        script = Path(self._tmp.name) / "fake-cli"
        script.write_text(
            "#!/bin/sh\necho '" + json.dumps(payload) + "'\n", encoding="utf-8"
        )
        script.chmod(0o755)
        return script

    def test_runs_due_and_records_status(self):
        MODULE.save_schedules([schedule(id="дозревшая"), schedule(id="свежая", last_run=NOW.isoformat())],
                              self.config)
        with mock.patch.object(MODULE, "_notify", self.notify), \
                mock.patch.object(MODULE, "CLI", str(self.fake_cli())):
            results = MODULE.run_due(now=NOW, config=self.config, notifier="notify-send")
        self.assertEqual([item["id"] for item in results], ["дозревшая"])
        rows, _ = MODULE.load_schedules(self.config)
        statuses = {row["id"]: row["last_status"] for row in rows}
        self.assertEqual(statuses["дозревшая"], "ok")
        self.assertIsNone(statuses["свежая"], "недозревшая не трогается")
        self.assertEqual(self.notifications, [], "неудач нет — failure-режим молчит")

    def test_failure_notifies_in_failure_mode(self):
        MODULE.save_schedules([schedule(id="падшая")], self.config)
        with mock.patch.object(MODULE, "_notify", self.notify), \
                mock.patch.object(MODULE, "CLI", str(self.fake_cli(ok=False, error="провайдер молчит"))):
            results = MODULE.run_due(now=NOW, config=self.config, notifier="notify-send")
        self.assertFalse(results[0]["ok"])
        self.assertEqual(len(self.notifications), 1)
        self.assertIn("провайдер молчит", self.notifications[0][2])

    def test_always_notify_mode(self):
        MODULE.save_schedules([schedule(id="весёлая", notify="always")], self.config)
        with mock.patch.object(MODULE, "_notify", self.notify), \
                mock.patch.object(MODULE, "CLI", str(self.fake_cli(text="всё хорошо"))):
            MODULE.run_due(now=NOW, config=self.config, notifier="notify-send")
        self.assertEqual(len(self.notifications), 1)

    def test_broken_config_notifies_and_does_not_raise(self):
        self.config.write_text("мусор", encoding="utf-8")
        with mock.patch.object(MODULE, "_notify", self.notify):
            results = MODULE.run_due(now=NOW, config=self.config, notifier="notify-send")
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertIn("не разбирается", results[0]["error"])
        self.assertEqual(len(self.notifications), 1)


class PackageConsistencyTests(unittest.TestCase):
    """Юниты и сборка соответствуют фактам пакета."""

    def test_units_exist_and_timer_is_opt_in(self):
        package = REPO / "packages" / "runneos-agents-scheduler"
        service = (package / "files" / "runneos-agents-scheduler.service").read_text(encoding="utf-8")
        timer = (package / "files" / "runneos-agents-scheduler.timer").read_text(encoding="utf-8")
        self.assertIn("ExecStart=/usr/bin/runneos-agents-scheduler run", service)
        self.assertIn("TimeoutStartSec=0", service)
        self.assertIn("OnUnitActiveSec=5min", timer)
        self.assertIn("[Install]", timer)
        pkgbuild = (package / "PKGBUILD").read_text(encoding="utf-8")
        self.assertNotIn("graphical-session.target.wants", pkgbuild,
                         "таймер не включается за пользователя — это opt-in")
        self.assertIn("runneos-agents>=0.7.0", pkgbuild)
        self.assertIn("test_scheduler_agents.py", pkgbuild)

    def test_version_matches(self):
        pkgbuild = (REPO / "packages" / "runneos-agents-scheduler" / "PKGBUILD").read_text(encoding="utf-8")
        match = __import__("re").search(r"^pkgver=(\S+)", pkgbuild, __import__("re").MULTILINE)
        self.assertEqual(match.group(1), MODULE.__version__)

    def test_smoke_test_runs_headless(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = dict(os.environ)
            environment["XDG_CONFIG_HOME"] = tmp
            done = subprocess.run(
                [sys.executable, str(SCRIPT), "--smoke-test", "--config",
                 str(Path(tmp) / "schedules.json")],
                capture_output=True, text=True, env=environment, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout.splitlines()[-1])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schedules"], 0)


if __name__ == "__main__":
    unittest.main()
