"""Инвентаризация документации пакета: числа и пути должны совпадать с фактами.

Тест-«сторож»: документация пакета уже один раз разошлась с кодом (аудит 2026-10-12 нашёл
устаревшие числа и неверный путь журналов прогонов). Здесь проверяется то, что можно проверить
машиной: количество тестов, наличие упомянутых файлов, пути, которые цитирует документация.

Запуск: PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_agents_*.py' -v
"""

import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "packages" / "runneos-agents"
FILES = PACKAGE / "files"

DOCS = {
    "README": PACKAGE / "README.md",
    "INTERFACE": PACKAGE / "INTERFACE.md",
    "agents.md": REPO / ".agents" / "skills" / "runneos" / "references" / "agents.md",
    "архитектура": REPO / "docs" / "agents-architecture.md",
    "протокол": REPO / "docs" / "results-agents-2026-10-12.md",
}

#: Устаревшие числа, которые уже были в документах и не должны вернуться.
STALE_NUMBERS = ("181 тест", "190 тест", "26 файлов", "71.4 KiB", "215.3 KiB")


def live_test_count() -> int:
    """Сколько тестовых методов объявлено в файлах test_agents_*.py."""
    total = 0
    for path in sorted((REPO / "tests").glob("test_agents_*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("def test_"):
                total += 1
    return total


class DocsAgainstRealityTests(unittest.TestCase):
    def test_docs_do_not_keep_stale_numbers(self):
        """Устаревшие числа из прошлых редакций не должны остаться ни в одном документе."""
        for name, path in DOCS.items():
            text = path.read_text(encoding="utf-8")
            for stale in STALE_NUMBERS:
                self.assertNotIn(stale, text, f"{name} содержит устаревшее «{stale}»")

    def test_interface_lists_every_test_file(self):
        """Контракт перечисляет все файлы тестов пакета — иначе он снова отстанет."""
        interface = DOCS["INTERFACE"].read_text(encoding="utf-8")
        for path in sorted((REPO / "tests").glob("test_agents_*.py")):
            self.assertIn(path.name, interface, f"в контракте нет {path.name}")

    def test_interface_describes_real_layout(self):
        """Раскладка файлов в контракте соответствует дереву."""
        interface = DOCS["INTERFACE"].read_text(encoding="utf-8")
        for relative in (
            "runneos_agents/model.py",
            "runneos_agents/store.py",
            "runneos_agents/orchestrator.py",
            "runneos_agents/routing.py",
            "runneos_agents/policy.py",
            "runneos_agents/workspace.py",
            "runneos_agents/adapters/__init__.py",
            "runneos_agents/adapters/base.py",
            "runneos_agents/adapters/codex.py",
            "runneos_agents/adapters/opencode.py",
            "runneos-agents",
            "runneos-agents-panel",
            "dms-plugin",
        ):
            self.assertTrue((FILES / relative).exists(), f"нет файла {relative}")
            self.assertIn(Path(relative).name, interface, f"в контракте не упомянут {relative}")

    def test_docs_describe_real_runs_location(self):
        """Журнал прогона пишется в воркспейс агента, а не в корень данных."""
        implementation = (FILES / "runneos_agents" / "orchestrator.py").read_text(encoding="utf-8")
        self.assertIn('Path(workspace) / "runs" / f"{session_id}.jsonl"', implementation)
        for name in ("README", "INTERFACE", "архитектура"):
            text = DOCS[name].read_text(encoding="utf-8")
            self.assertIn("<workspace>/runs/", text, f"{name} не описывает фактический путь журнала")

    def test_docs_do_not_promise_cli_event_stream(self):
        """У CLI нет стрима событий: контракт не должен его обещать."""
        text = DOCS["INTERFACE"].read_text(encoding="utf-8")
        self.assertNotIn("стрим событий в stderr", text)
        self.assertIn("Стрима событий нет", text)

    def test_docs_mark_policy_as_not_connected(self):
        """Правила не подключены к рантайму — документация обязана это говорить."""
        for name in ("README", "INTERFACE", "agents.md", "архитектура"):
            text = DOCS[name].read_text(encoding="utf-8").lower()
            self.assertTrue(
                "не подключен" in text or "не подключены" in text,
                f"{name} не сообщает, что policy не подключён к рантайму",
            )

    def test_widget_docs_do_not_claim_dms_reads_package_path(self):
        """DMS не читает /usr/share/dms-plugins напрямую: это нельзя утверждать."""
        for name, path in DOCS.items():
            text = path.read_text(encoding="utf-8")
            for forbidden in (
                "ставит туда, откуда DMS",
                "DMS читает из /usr/share",
            ):
                self.assertNotIn(forbidden, text, f"{name} содержит неверное утверждение")

    def test_provider_docs_match_adapter_reality(self):
        """README не должен называть подтверждённый режим opencode неподтверждённым."""
        readme = DOCS["README"].read_text(encoding="utf-8")
        self.assertNotIn("адаптер отдаёт честное «не подтверждён»", readme)
        adapter = (FILES / "runneos_agents" / "adapters" / "opencode.py").read_text(encoding="utf-8")
        self.assertIn("--standalone", adapter)
        self.assertIn("--auto", readme, "README обязан называть настоящее ограничение (--auto)")

    def test_license_claim_about_intelligence_is_marked_unverified(self):
        """Текст лицензии CopilotKit Intelligence не публичен — нельзя писать «несвободный» как факт."""
        for name in ("README", "agents.md"):
            text = DOCS[name].read_text(encoding="utf-8")
            self.assertNotIn("несвободный CopilotKit Intelligence", text, name)
            self.assertNotIn("несвободным CopilotKit Intelligence", text, name)
        protocol = DOCS["протокол"].read_text(encoding="utf-8")
        self.assertNotIn("обязательным компонентом идёт несвободный", protocol)

    def test_no_dangling_package_url(self):
        """URL пакета не должен вести в несуществующий репозиторий без пометки."""
        pkgbuild = (PACKAGE / "PKGBUILD").read_text(encoding="utf-8")
        match = re.search(r"^url='([^']+)'", pkgbuild, re.MULTILINE)
        self.assertIsNotNone(match, "в PKGBUILD нет url")
        url = match.group(1)
        self.assertFalse(
            url.endswith("github.com/runneos/runneos"),
            "url ведёт в несуществующий репозиторий (проверено: HTTP 404)",
        )


class DocsInventoryReportTests(unittest.TestCase):
    def test_protocol_mentions_its_own_measurements(self):
        """Протокол обязан опираться на факты: содержать команду прогона и число тестов.

        Число не сверяется дословно (оно меняется вместе с набором), но протокол должен
        называть хотя бы одно измеренное значение и не противоречить сам себе.
        """
        count = live_test_count()
        self.assertGreater(count, 0)
        protocol = DOCS["протокол"].read_text(encoding="utf-8")
        self.assertIn("unittest discover", protocol, "в протоколе нет команды прогона тестов")
        numbers = {int(value) for value in re.findall(r"\b(\d{3})\s+тест", protocol)}
        self.assertTrue(numbers, "в протоколе нет ни одного измеренного числа тестов")
        self.assertLessEqual(
            max(numbers),
            count + 40,
            "в протоколе число тестов заметно больше фактического — похоже на устаревшее значение",
        )


if __name__ == "__main__":
    unittest.main()
