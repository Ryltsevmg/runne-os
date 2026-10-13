import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

FILES = Path(__file__).parents[1]/'packages/runneos-theme/files'
spec = importlib.util.spec_from_file_location('theme', FILES/'generate.py')
theme = importlib.util.module_from_spec(spec)
spec.loader.exec_module(theme)


class ThemeTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((FILES/'tokens.json').read_text())

    def test_text_pairs_meet_aa(self):
        theme.validate(self.data)

    def test_known_contrast_reference(self):
        self.assertAlmostEqual(theme.contrast('#000000', '#FFFFFF'), 21)

    def test_low_contrast_is_rejected(self):
        self.data['modes']['carbon']['muted'] = '#333333'
        with self.assertRaises(ValueError):theme.validate(self.data)

    def test_executable_css_payload_rejected(self):
        self.data['modes']['cloud']['surface'] = 'url(https://example.com)'
        with self.assertRaises(ValueError):theme.validate(self.data)

    def test_css_contains_both_modes_and_accessible_accent(self):
        css = theme.css(self.data)
        self.assertIn('data-theme="cloud"', css)
        self.assertIn('--accent-text:', css)
        self.assertIn('--link: #9BB0FF', css)

    def test_dms_theme_is_data_with_two_modes(self):
        data = theme.dms(self.data)
        self.assertEqual(set(data), {'light', 'dark'})
        for colors in data.values():
            self.assertGreaterEqual(theme.contrast(colors['primary'], colors['primaryText']), 4.5)

    def test_generation_deterministic_and_no_ghostty_commands(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            theme.generate(FILES/'tokens.json', path)
            before = {p.name:p.read_bytes() for p in path.iterdir()}
            theme.generate(FILES/'tokens.json', path)
            self.assertEqual(before, {p.name:p.read_bytes() for p in path.iterdir()})
            for mode in ('carbon', 'cloud'):
                fragment = (path/f'ghostty-{mode}.conf').read_text()
                self.assertNotIn('command', fragment)
                self.assertNotIn('keybind', fragment)


if __name__ == '__main__':unittest.main()
