"""Тесты онбординга агентов (runneos-agents-firstboot): логика без дисплея и статика пакета.

Диалог — тонкий слой над CLI агентов: состояния берёт из `provider auth-status --json`,
вход отдаёт штатному CLI провайдера в терминале, однократность держит маркером в
XDG_STATE_HOME. Здесь проверяется ровно то, что можно проверить без сеанса: чистая
логика, файл юнита, сборка пакета и присутствие в списках ISO/установщика.
Живой первый вход в DMS — приёмка на реальной машине (в QEMU сеанс не поднимается).

Запуск: PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_firstboot_agents.py' -v
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "packages" / "runneos-agents-firstboot"
SCRIPT = PACKAGE / "files" / "runneos-agents-firstboot"
UNIT = PACKAGE / "files" / "runneos-agents-firstboot.service"


def load_module(name: str = "runneos_agents_firstboot"):
    """Загрузить диалог как модуль: скрипт без расширения .py."""
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


MODULE = load_module()


class MarkerTests(unittest.TestCase):
    """Маркер однократности: путь в XDG_STATE_HOME, запись и сброс."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_home = Path(self._tmp.name)

    def test_marker_path_respects_state_home(self):
        path = MODULE.marker_path(self.state_home)
        self.assertEqual(
            path,
            self.state_home / "runneos" / "agents-firstboot" / "done",
            "путь маркера совпадает с условием юнита",
        )

    def test_write_and_reset_marker(self):
        self.assertFalse(MODULE.marker_exists(self.state_home))
        self.assertTrue(MODULE.write_marker(self.state_home))
        self.assertTrue(MODULE.marker_exists(self.state_home))
        self.assertTrue(MODULE.reset_marker(self.state_home))
        self.assertFalse(MODULE.marker_exists(self.state_home))
        self.assertFalse(MODULE.reset_marker(self.state_home), "повторный сброс честно говорит «не было»")

    def test_write_marker_creates_private_directory(self):
        MODULE.write_marker(self.state_home)
        mode = (self.state_home / "runneos" / "agents-firstboot").stat().st_mode
        self.assertEqual(mode & 0o077, 0, "каталог состояния должен быть закрыт от других")


class CollectStatusTests(unittest.TestCase):
    """Состояния провайдеров берутся из CLI агентов; сбой — причина, а не падение."""

    def test_parses_json_lines(self):
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "provider": "codex",
                        "binary_present": True,
                        "authorized": False,
                        "detail": "Not logged in",
                        "how": "codex login",
                    }
                ),
                "мусорная строка",
                json.dumps({"provider": "opencode", "binary_present": False, "authorized": None}),
            ]
        )
        with mock.patch.object(
            subprocess, "run", return_value=mock.Mock(returncode=0, stdout=stdout, stderr="")
        ):
            rows, error = MODULE.collect_status()
        self.assertEqual(error, "")
        self.assertEqual([row["provider"] for row in rows], ["codex", "opencode"])
        self.assertEqual(rows[0]["authorized"], False)
        self.assertIsNone(rows[1]["authorized"])

    def test_cli_failure_is_reported(self):
        with mock.patch.object(
            subprocess,
            "run",
            return_value=mock.Mock(returncode=1, stdout="", stderr="нет базы"),
        ):
            rows, error = MODULE.collect_status()
        self.assertEqual(rows, [])
        self.assertIn("код 1", error)
        self.assertIn("нет базы", error)

    def test_missing_cli_is_honest(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError()):
            rows, error = MODULE.collect_status()
        self.assertEqual(rows, [])
        self.assertIn("не найден", error)

    def test_timeout_is_reported(self):
        with mock.patch.object(
            subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="cli", timeout=15)
        ):
            rows, error = MODULE.collect_status()
        self.assertEqual(rows, [])
        self.assertIn("не ответил", error)


class RowViewTests(unittest.TestCase):
    """Что показывать по строке auth-status и когда предлагать «Подключить»."""

    def test_unauthorized_with_how_is_connectable(self):
        state, connectable, subtitle = MODULE.row_view(
            {
                "provider": "codex",
                "binary_present": True,
                "authorized": False,
                "detail": "Not logged in",
                "how": "codex login",
            }
        )
        self.assertEqual(state, "не авторизован")
        self.assertTrue(connectable)
        self.assertIn("codex login", subtitle)
        self.assertIn("Not logged in", subtitle)

    def test_authorized_has_no_button(self):
        state, connectable, _ = MODULE.row_view(
            {"provider": "codex", "binary_present": True, "authorized": True, "detail": "Logged in"}
        )
        self.assertEqual(state, "подключён")
        self.assertFalse(connectable)

    def test_unknown_is_not_connectable(self):
        state, connectable, _ = MODULE.row_view(
            {"provider": "codex", "binary_present": True, "authorized": None, "detail": "сбой"}
        )
        self.assertEqual(state, "неизвестно")
        self.assertFalse(connectable)

    def test_missing_binary_is_not_connectable(self):
        state, connectable, subtitle = MODULE.row_view(
            {"provider": "opencode", "binary_present": False, "authorized": None, "detail": "не найден"}
        )
        self.assertEqual(state, "не найден")
        self.assertFalse(connectable)
        self.assertIn("не найден", subtitle)

    def test_connect_argv_runs_terminal(self):
        argv = MODULE.connect_argv({"how": "codex login"})
        self.assertEqual(argv, [MODULE.TERMINAL, "-e", "codex", "login"])
        self.assertIsNone(MODULE.connect_argv({}), "без команды входа подключать нечем")


class SmokePayloadTests(unittest.TestCase):
    def test_payload_shape(self):
        with mock.patch.object(
            MODULE,
            "collect_status",
            return_value=([{"provider": "codex", "authorized": True}], ""),
        ):
            payload = MODULE.smoke_payload()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["error"], "")
        self.assertIn("path", payload["marker"])
        self.assertIn("exists", payload["marker"])
        self.assertEqual(payload["providers"][0]["provider"], "codex")

    def test_payload_reports_error(self):
        with mock.patch.object(MODULE, "collect_status", return_value=([], "CLI не найден")):
            payload = MODULE.smoke_payload()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "CLI не найден")


class PackageConsistencyTests(unittest.TestCase):
    """Юнит, сборка и списки ISO должны соответствовать фактам пакета."""

    def test_unit_uses_marker_condition_and_graphical_session(self):
        text = UNIT.read_text(encoding="utf-8")
        self.assertIn("ConditionPathExists=!%h/.local/state/runneos/agents-firstboot/done", text)
        self.assertIn("After=graphical-session.target dms.service", text)
        self.assertIn("ExecStart=/usr/bin/runneos-agents-firstboot", text)
        self.assertIn("WantedBy=graphical-session.target", text)
        self.assertIn("TimeoutStartSec=0", text, "диалог не должен быть убит по таймауту")

    def test_unit_marker_matches_code(self):
        """Путь маркера в юните и в коде — одно и то же место."""
        unit_text = UNIT.read_text(encoding="utf-8")
        unit_marker = re.search(r"ConditionPathExists=!(\S+)", unit_text).group(1)
        # %h/.local/state/... в коде — это Path.home()/".local/state"/MARKER_RELATIVE.
        relative = "/".join(MODULE.MARKER_RELATIVE.parts)
        self.assertTrue(unit_marker.startswith("%h/.local/state/"), unit_marker)
        self.assertTrue(unit_marker.endswith(relative), f"{unit_marker} не кончается {relative}")

    def test_pkgbuild_installs_bin_unit_and_autostart_symlink(self):
        text = (PACKAGE / "PKGBUILD").read_text(encoding="utf-8")
        self.assertIn("pkgname=runneos-agents-firstboot", text)
        self.assertIn("/usr/bin/runneos-agents-firstboot", text)
        self.assertIn("/usr/lib/systemd/user/runneos-agents-firstboot.service", text)
        self.assertIn(
            "graphical-session.target.wants/runneos-agents-firstboot.service", text,
            "юнит обязан быть включён для сеансов пользователей",
        )
        self.assertIn("runneos-agents>=0.3.0", text, "онбординг держится на auth-status из 0.3.0")
        self.assertIn("'runneos-config'", text, "runneos-terminal даёт runneos-config, отдельного пакета нет")
        self.assertIn("test_firstboot_agents.py", text)

    def test_pkgbuild_version_matches_script(self):
        pkgbuild = (PACKAGE / "PKGBUILD").read_text(encoding="utf-8")
        match = re.search(r"^pkgver=(\S+)", pkgbuild, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), MODULE.__version__)

    def test_package_is_in_iso_and_installer_lists(self):
        iso = (REPO / "iso" / "packages.x86_64").read_text(encoding="utf-8")
        self.assertIn("runneos-agents-firstboot", iso)
        installer = (REPO / "installer" / "user_configuration.json").read_text(encoding="utf-8")
        self.assertIn("runneos-agents-firstboot", installer)

    def test_script_smoke_test_runs_headless(self):
        """--smoke-test без дисплея даёт JSON и код 0/1 — это проверяемо на серийной консоли."""
        with tempfile.TemporaryDirectory() as tmp:
            environment = dict(os.environ)
            environment["XDG_STATE_HOME"] = tmp
            environment.pop("DISPLAY", None)
            environment.pop("WAYLAND_DISPLAY", None)
            done = subprocess.run(
                [sys.executable, str(SCRIPT), "--smoke-test", "--cli", "нет-такого-cli"],
                capture_output=True,
                text=True,
                env=environment,
                timeout=60,
            )
        self.assertIn(done.returncode, (0, 1))
        payload = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertFalse(payload["ok"])
        self.assertIn("не найден", payload["error"])
        self.assertIn("path", payload["marker"])


if __name__ == "__main__":
    unittest.main()
