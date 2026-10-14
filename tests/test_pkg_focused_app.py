"""Сторож плагина runneosFocusedApp: манифест, установщик, монтаж в пакет."""
import json
import subprocess
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "packages" / "runneos-config" / "files" / "dms-plugins" / "RunneosFocusedApp"
INSTALLER = Path(__file__).resolve().parent.parent / "packages" / "runneos-config" / "files" / "runneos-focused-app-install"
PKGBUILD = Path(__file__).resolve().parent.parent / "packages" / "runneos-config" / "PKGBUILD"


class TestFocusedAppPlugin(unittest.TestCase):
    def test_manifest_matches_component_and_id(self):
        manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text())
        self.assertEqual(manifest["id"], "runneosFocusedApp")
        self.assertEqual(manifest["type"], "widget")
        self.assertIn("dankbar-widget", manifest["capabilities"])
        component = manifest["component"].removeprefix("./")
        self.assertIn(component, [p.name for p in PLUGIN_DIR.iterdir()])

    def test_widget_uses_left_focus_right_popout(self):
        qml = (PLUGIN_DIR / "RunneosFocusedApp.qml").read_text()
        self.assertIn("NiriService.focusWindow", qml)
        self.assertIn("pillClickAction", qml)
        self.assertIn("pillRightClickAction", qml)
        self.assertIn("triggerPopout", qml)

    def test_installer_swaps_builtin_widget(self):
        text = INSTALLER.read_text()
        self.assertIn("runneosFocusedApp", text)
        self.assertIn("focusedWindow", text)
        self.assertEqual(subprocess.run(["bash", "-n", str(INSTALLER)]).returncode, 0)

    def test_pkgbuild_ships_plugin_and_installer(self):
        pkgbuild = PKGBUILD.read_text()
        self.assertIn("RunneosFocusedApp", pkgbuild)
        self.assertIn("runneos-focused-app-install", pkgbuild)


if __name__ == "__main__":
    unittest.main()
