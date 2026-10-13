import importlib.util
import unittest
from pathlib import Path


class GhosttyTerminalTests(unittest.TestCase):
    def setUp(self):
        script = Path(__file__).parents[1] / 'scripts/runneos-terminal.py'
        self.assertTrue(script.exists(), 'Terminal adapter is missing')
        spec = importlib.util.spec_from_file_location('terminal', script)
        self.adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.adapter)

    def test_command_arguments_are_passed_without_shell_expansion(self):
        args = ['python3', '-c', 'print("hello")', 'a; touch /tmp/unwanted', "it's quoted", 'русский']
        launch = self.adapter.prepare(['-e', *args], '/tmp/a b')
        self.assertEqual(launch, ['ghostty', '--working-directory=/tmp/a b', '-e', *args])

    def test_dms_shell_command_keeps_single_script_argument(self):
        script = 'printf "%s\\n" "a b"; printf done'
        launch = self.adapter.prepare(['-e', 'sh', '-c', script], '/tmp')
        self.assertEqual(launch[3:], ['sh', '-c', script])

    def test_plain_terminal_opens_requested_directory(self):
        launch = self.adapter.prepare(['--working-directory', '/tmp/a b'], '/tmp')
        self.assertEqual(launch, ['ghostty', '--working-directory=/tmp/a b'])

    def test_command_flag_without_command_is_rejected(self):
        with self.assertRaises(ValueError):
            self.adapter.prepare(['-e'], '/tmp')


if __name__ == '__main__':
    unittest.main()
