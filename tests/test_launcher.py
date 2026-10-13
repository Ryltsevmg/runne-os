import importlib.util
from pathlib import Path
import unittest
import subprocess
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('launcher',Path(__file__).parents[1]/'packages/runneos-launcher/files/launcher.py')
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_hostile_ids_rejected(self):
        for value in ('--help','../bad.desktop','foo;touch.desktop','https://example.com/x.desktop',''):
            with self.subTest(value=value),self.assertRaises(ValueError):launcher.desktop_id(value)

    def test_desktop_and_startup_id_both_supported(self):
        self.assertEqual(launcher.candidates('bitwarden.desktop','Bitwarden'),{'bitwarden','Bitwarden'})

    def test_titles_never_used_as_identity(self):
        self.assertIsNone(launcher.choose_window([{'id':1,'title':'org.runneos.AI','app_id':'different'}],{'org.runneos.AI'}))

    def test_focused_window_preferred_over_recent(self):
        rows=[{'id':1,'app_id':'app','is_focused':True},{'id':2,'app_id':'app','focus_timestamp':{'secs':100}}]
        self.assertEqual(launcher.choose_window(rows,{'app'})['id'],1)

    def test_most_recent_matching_window_selected(self):
        rows=[{'id':1,'app_id':'app','focus_timestamp':{'secs':1}},{'id':2,'app_id':'app','focus_timestamp':{'secs':2}}]
        self.assertEqual(launcher.choose_window(rows,{'app'})['id'],2)

    def test_case_sensitive_app_ids_not_guessed(self):
        self.assertIsNone(launcher.choose_window([{'id':1,'app_id':'APP'}],{'app'}))

    def test_scope_predicate_can_refuse_ambiguous_window(self):
        self.assertIsNone(launcher.choose_window([{'id':1,'app_id':'app'}],{'app'},lambda row:False))

    def test_unknown_flatpak_pid_never_claims_scope(self):
        self.assertFalse(launcher.flatpak_scope({'pid':None},'system','org.example.App'))

    def test_flatpak_runtime_not_launched_as_application(self):
        response=subprocess.CompletedProcess([],0,b'runtime/org.example.Platform/x86_64/stable\n',b'')
        with patch.object(launcher.subprocess,'run',return_value=response):
            with self.assertRaises(ValueError):launcher.launch(flatpak='org.example.Platform')


if __name__=='__main__':unittest.main()
