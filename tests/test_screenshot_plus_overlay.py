"""Сторож русификации DMS-плагина screenshotPlus: overlay-переводы, установщик, пакет."""
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TRANSLATIONS = REPO / "packages" / "runneos-config" / "files" / "dms-overlays" / "screenshotPlus" / "translations"
INSTALLER = REPO / "scripts" / "install-dms-plugins.py"
PKGBUILD = REPO / "packages" / "runneos-config" / "PKGBUILD"
LOCK = REPO / "packages" / "runneos-config" / "files" / "dms-plugins.lock.json"

# Коммит upstream, на котором сняты английские термины; при перепривязке
# lock-файла каталог нужно сверить заново.
PINNED_COMMIT = "af9a87dc9b3c9b27a23eec963a2a75ef037f1828"

# Все термины I18n.trFor("screenshotPlus", ...) из исходников pinned-коммита.
TERMS = [
    "Toolbar",
    "Default style",
    "Color",
    "Size",
    "Output",
    "Copy to clipboard",
    "Save to file",
    "Pin to screen",
    "Save directory",
    "File name",
    "date(1) format without the extension, e.g. screenshot_%Y-%m-%d_%H-%M-%S",
    "Notify when done",
    "Backend",
    "Select",
    "Rectangle",
    "Ellipse",
    "Line",
    "Arrow",
    "Pen",
    "Highlighter",
    "Text",
    "Mosaic",
    "Number",
    "Color / size",
    "Undo (Ctrl+Z)",
    "Redo (Ctrl+Shift+Z)",
    "Save to file (Ctrl+S)",
    "Copy to clipboard (Enter / Space)",
    "Cancel (Esc)",
    "Pin to screen (Ctrl+P)",
    "Close (Esc)",
    "Custom color",
    "Drag to select an area  ·  Esc to cancel",
    "Annotation color",
    "Saved and copied to clipboard",
    "Saved",
    "Copied to clipboard",
    "Could not save to",
    "cli grabs the screen with dms screenshot and works with any Quickshell. screencopy shows the frame about three times sooner but crashes current stock Quickshell when the overlay closes (quickshell#1094, fix pending); use it only with a Quickshell that includes the fix.",
    "Clear all annotations",
    "F  whole screen  ·  < >  previous regions",
    "F  whole screen",
]


def load_catalog():
    return json.loads((TRANSLATIONS / "ru_RU.json").read_text())


class TestScreenshotPlusOverlay(unittest.TestCase):
    def test_russian_catalogs_exist_and_match(self):
        ru_ru = json.loads((TRANSLATIONS / "ru_RU.json").read_text())
        ru = json.loads((TRANSLATIONS / "ru.json").read_text())
        self.assertEqual(ru_ru, ru, "ru.json должен повторять ru_RU.json для локали без региона")

    def test_catalog_covers_every_upstream_term(self):
        catalog = load_catalog()
        self.assertEqual(sorted(catalog), sorted(TERMS))
        for term in TERMS:
            self.assertIn(term, catalog)
            self.assertEqual(list(catalog[term]), [term], f"контекст {term!r} должен быть {term!r}: {term!r}")
            value = catalog[term][term]
            self.assertTrue(value and value.strip())

    def test_notification_strings_are_russian(self):
        catalog = load_catalog()
        expectations = {
            "Saved": "Сохранено",
            "Copied to clipboard": "Скопировано в буфер обмена",
            "Saved and copied to clipboard": "Сохранено и скопировано в буфер обмена",
        }
        for term, expected in expectations.items():
            self.assertEqual(catalog[term][term], expected)
        self.assertTrue(catalog["Could not save to"]["Could not save to"].startswith("Не удалось сохранить"))

    def test_translated_values_are_not_english_sources(self):
        catalog = load_catalog()
        for term in TERMS:
            value = catalog[term][term]
            self.assertNotEqual(value, term, f"термин {term!r} остался непереведённым")
            self.assertTrue(any("\u0400" <= ch <= "\u04FF" for ch in value), f"в переводе {term!r} нет кириллицы")

    def test_installer_applies_overlay_to_plugin(self):
        text = INSTALLER.read_text()
        self.assertIn("dms-overlays/screenshotPlus/translations", text)
        self.assertIn("plugins/screenshotPlus/translations", text)
        for name in ("ru_RU.json", "ru.json"):
            self.assertIn(name, text)
        compile(text, str(INSTALLER), "exec")

    def test_pkgbuild_ships_overlay(self):
        pkgbuild = PKGBUILD.read_text()
        self.assertIn("files/dms-overlays/screenshotPlus/translations/ru_RU.json", pkgbuild)
        self.assertIn("usr/share/runneos/dms/dms-overlays/screenshotPlus/translations/ru_RU.json", pkgbuild)
        self.assertIn("files/dms-overlays/screenshotPlus/translations/ru.json", pkgbuild)

    def test_overlay_matches_pinned_plugin_commit(self):
        lock = json.loads(LOCK.read_text())
        self.assertEqual(lock["plugins"]["screenshotPlus"]["commit"], PINNED_COMMIT)


if __name__ == "__main__":
    unittest.main()
