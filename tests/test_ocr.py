"""Проверки пакета runneos-ocr: скрипт, отмена выбора, языки и PKGBUILD."""
import ast
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "packages" / "runneos-ocr" / "files" / "ocr.py"
PKGDIR = ROOT / "packages" / "runneos-ocr"
DEPENDS = ("grim", "slurp", "tesseract", "tesseract-data-rus", "tesseract-data-eng",
           "wl-clipboard", "libnotify", "python")

spec = importlib.util.spec_from_file_location("ocr", SCRIPT)
ocr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ocr)


class OcrScriptTests(unittest.TestCase):
    def test_script_is_valid_python(self):
        source = SCRIPT.read_text()
        compile(source, str(SCRIPT), "exec")  # байткод-компиляция без записи pyc
        self.assertTrue(ast.parse(source))

    def test_external_calls_use_subprocess_without_shell(self):
        source = SCRIPT.read_text()
        self.assertNotIn("shell=", source)
        self.assertNotIn("os.system", source)
        self.assertNotIn("os.popen", source)
        for node in ast.walk(ast.parse(source)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"):
                self.assertNotIn("shell", [kw.arg for kw in node.keywords], ast.dump(node))

    def test_select_region_returns_none_when_user_cancels(self):
        cancelled = subprocess.CompletedProcess(["slurp"], 1, stdout="", stderr="escaped")
        with patch.object(ocr.subprocess, "run", return_value=cancelled):
            self.assertIsNone(ocr.select_region())

    def test_select_region_rejects_malformed_geometry(self):
        weird = subprocess.CompletedProcess(["slurp"], 0, stdout="; rm -rf /; echo\n", stderr="")
        with patch.object(ocr.subprocess, "run", return_value=weird):
            self.assertIsNone(ocr.select_region())

    def test_cancelled_selection_is_quiet_success(self):
        with patch.object(ocr, "select_region", return_value=None) as select, \
                patch.object(ocr, "notify") as notify, \
                patch.object(ocr, "copy_text") as copy_text, \
                patch.object(sys, "argv", ["runneos-ocr"]):
            self.assertEqual(ocr.main(), 0)
        select.assert_called_once_with()
        notify.assert_not_called()
        copy_text.assert_not_called()

    def test_default_language_is_rus_and_eng(self):
        self.assertEqual(ocr.DEFAULT_LANG, "rus+eng")

    def test_plain_run_needs_no_flags_and_recognizes_rus_eng(self):
        with patch.object(ocr, "select_region", return_value="10,20 300x200") as select, \
                patch.object(ocr, "grab", return_value=b"png") as grab, \
                patch.object(ocr, "recognize", return_value="Текст") as recognize, \
                patch.object(ocr, "copy_text") as copy_text, \
                patch.object(ocr, "notify") as notify, \
                patch.object(sys, "argv", ["runneos-ocr"]):
            self.assertEqual(ocr.main(), 0)
        select.assert_called_once_with()
        grab.assert_called_once_with("10,20 300x200")
        recognize.assert_called_once_with(b"png", "rus+eng")
        copy_text.assert_called_once_with("Текст")
        notify.assert_called_once_with(f"Распознано {len('Текст')} символов")

    def test_lang_flag_overrides_default(self):
        with patch.object(ocr, "select_region", return_value="0,0 5x5"), \
                patch.object(ocr, "grab", return_value=b"png"), \
                patch.object(ocr, "recognize", return_value="Text") as recognize, \
                patch.object(ocr, "copy_text"), patch.object(ocr, "notify"), \
                patch.object(sys, "argv", ["runneos-ocr", "--lang", "eng"]):
            self.assertEqual(ocr.main(), 0)
        recognize.assert_called_once_with(b"png", "eng")

    def test_empty_recognition_keeps_clipboard_intact(self):
        with patch.object(ocr, "select_region", return_value="0,0 5x5"), \
                patch.object(ocr, "grab", return_value=b"png"), \
                patch.object(ocr, "recognize", return_value="  "), \
                patch.object(ocr, "copy_text") as copy_text, \
                patch.object(ocr, "notify") as notify, \
                patch.object(sys, "argv", ["runneos-ocr"]):
            self.assertEqual(ocr.main(), 0)
        copy_text.assert_not_called()
        notify.assert_called_once_with("Текст не распознан")

    def test_recognition_failure_reports_error_without_notification(self):
        with patch.object(ocr, "select_region", return_value="0,0 5x5"), \
                patch.object(ocr, "grab", side_effect=RuntimeError("grim не смог снять выбранную область")), \
                patch.object(ocr, "notify") as notify, \
                patch.object(sys, "argv", ["runneos-ocr"]):
            self.assertEqual(ocr.main(), 1)
        notify.assert_not_called()

    def test_grim_uses_region_argument_not_shell_string(self):
        with patch.object(ocr.subprocess, "run", return_value=subprocess.CompletedProcess(["grim"], 0, stdout=b"png")) as run:
            ocr.grab("10,20 300x200")
        self.assertEqual(run.call_args.args[0], ["grim", "-g", "10,20 300x200", "-"])

    def test_help_works_without_wayland(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--lang", result.stdout)
        self.assertIn("rus+eng", result.stdout)


class OcrPackageTests(unittest.TestCase):
    def setUp(self):
        self.pkgbuild = (PKGDIR / "PKGBUILD").read_text()

    def test_pkgbuild_declares_all_dependencies(self):
        for package in DEPENDS:
            with self.subTest(package=package):
                self.assertIn(f"'{package}'", self.pkgbuild)

    def test_pkgbuild_installs_script_as_runneos_ocr(self):
        self.assertIn("files/ocr.py", self.pkgbuild)
        self.assertIn("/usr/bin/runneos-ocr", self.pkgbuild)
        self.assertIn("license=('MIT')", self.pkgbuild)
        self.assertIn("arch=('any')", self.pkgbuild)

    def test_pkgbuild_check_runs_repository_tests(self):
        self.assertIn("test_ocr.py", self.pkgbuild)

    def test_license_is_mit_for_runneos(self):
        license_text = (PKGDIR / "LICENSE").read_text()
        self.assertIn("MIT License", license_text)
        self.assertIn("RunneOS contributors", license_text)


if __name__ == "__main__":
    unittest.main()
