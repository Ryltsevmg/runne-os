"""Сторож пакета runneos-keyring: ключ цел, отпечаток и UID совпадают."""
import unittest
from pathlib import Path

KEYRING_DIR = Path(__file__).resolve().parent.parent / "packages" / "runneos-keyring" / "files"
FPR = "3174AC28EBBBD790813B4FBC00B5A884A68486E9"
UID = "RunneOS packages <packages@runneos.local>"


def show_keys(path: Path) -> str:
    import subprocess

    return subprocess.run(
        ["gpg", "--show-keys", str(path)], capture_output=True, text=True, check=True
    ).stdout


class TestKeyringPayload(unittest.TestCase):
    def test_binary_keyring_contains_expected_key(self):
        out = show_keys(KEYRING_DIR / "runneos.gpg")
        self.assertIn(FPR, out)
        self.assertIn(UID, out)

    def test_armored_copy_same_key(self):
        out = show_keys(KEYRING_DIR / "runneos.asc")
        self.assertIn(FPR, out)
        self.assertIn(UID, out)

    def test_install_script_populates_keyring(self):
        install = (KEYRING_DIR.parent / "runneos-keyring.install").read_text()
        self.assertIn("pacman-key --populate runneos", install)
        self.assertIn("post_upgrade", install)

    def test_trusted_list_pins_fingerprint(self):
        trusted = (KEYRING_DIR / "runneos-trusted").read_text().split()
        self.assertIn(FPR, trusted)

    def test_pkgbuild_pins_fingerprint(self):
        pkgbuild = (KEYRING_DIR.parent / "PKGBUILD").read_text()
        self.assertIn(FPR, pkgbuild)


if __name__ == "__main__":
    unittest.main()
