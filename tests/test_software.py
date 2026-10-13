import importlib.util
import sys
import tempfile
import unittest
from unittest.mock import patch
import io
import json
import subprocess
from pathlib import Path

FILES = Path(__file__).parents[1] / 'packages/runneos-software/files'
sys.path.insert(0, str(FILES))


class CatalogTests(unittest.TestCase):
    def test_metadata_locale_screenshot_and_identity(self):
        from catalog import parse_appstream
        xml = '''<components origin="test"><component type="desktop-application">
        <id>org.example.Editor</id><pkgname>example-editor</pkgname><name>Editor</name>
        <name xml:lang="ru">Редактор</name><summary>Write text</summary>
        <description><p>Safe &amp; useful</p></description><icon type="stock">text-editor</icon>
        <categories><category>Development</category></categories>
        <screenshots><screenshot><image type="source">https://example.org/screen.png</image></screenshot></screenshots>
        </component></components>'''
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'apps.xml'; path.write_text(xml)
            apps=parse_appstream(path,'arch','extra',locale='ru')
        self.assertEqual(len(apps),1)
        self.assertEqual(apps[0].name,'Редактор')
        self.assertEqual(apps[0].package,'example-editor')
        self.assertEqual(apps[0].screenshots,['https://example.org/screen.png'])
        self.assertEqual(apps[0].description,'Safe & useful')

    def test_installed_local_copy_preferred_to_old_catalog(self):
        from catalog import App,merge_apps
        apps=merge_apps([App('ai.opencode.opencode','OpenCode',source='flatpak',package='ai.opencode.opencode'),
                         App('ai.opencode.desktop','OpenCode',source='local',installed=True)])
        self.assertEqual(len(apps),1)
        self.assertEqual(apps[0].source,'local')
        self.assertTrue(apps[0].installed)
        self.assertEqual(len(apps[0].alternatives),1)

    def test_installed_flatpak_scopes_are_not_lost(self):
        from catalog import App,merge_apps
        apps=merge_apps([App('org.example.App','Example',source='flatpak',scope='system',installed=True),
                         App('org.example.App','Example',source='flatpak',scope='user',installed=True)])
        self.assertEqual(len(apps),2)
        self.assertNotEqual(apps[0].key,apps[1].key)


class OperationTests(unittest.TestCase):
    def test_update_has_snapshot_before_pacman_and_no_partial_upgrade(self):
        from operations import update_commands
        steps=update_commands()
        self.assertEqual(steps[0][1],'/usr/bin/snapper')
        self.assertEqual(steps[1][1],'/usr/bin/pacman')
        self.assertIn('-Syu',steps[1])
        self.assertNotIn('-Sy',steps[1])
        self.assertEqual(sum(step[0]=='flatpak' for step in steps),2)
        self.assertEqual(steps[0][steps[0].index('--cleanup-algorithm')+1],'number')

    def test_manual_and_npm_operations_are_not_implemented(self):
        from catalog import App
        from operations import commands
        for source in ('local','npm'):
            for action in ('install','remove'):
                with self.subTest(source=source,action=action), self.assertRaises(ValueError):
                    commands(action,App('id','Test',source=source,package='example'))

    def test_retained_tools_cannot_be_removed(self):
        from catalog import App
        from operations import commands
        for name in ('warp-terminal','opencode','opencode-desktop','tailscale','ktailctl','network-manager-applet'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                commands('remove',App(name,name,source='arch',package=name))

    def test_hostile_package_names_rejected(self):
        from catalog import App
        from operations import commands
        for name in ['--overwrite','x;touch /tmp/evil','../bad','', 'a\nb']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                commands('install',App('id','Test',source='arch',package=name))

    def test_remove_keeps_data_and_scope(self):
        from catalog import App
        from operations import commands
        command=commands('remove',App('id','Test',source='arch',package='test'))[0]
        self.assertIn('-R',command)
        self.assertNotIn('-Rns',command)
        command=commands('remove',App('org.example.App','Test',source='flatpak',package='org.example.App',scope='user'))[0]
        self.assertIn('--user',command)
        self.assertNotIn('--delete-data',command)

    def test_protected_environment_cannot_be_removed(self):
        from catalog import App
        from operations import commands
        for name in ['niri','dms-shell','pacman','runneos-software','networkmanager']:
            with self.assertRaises(ValueError):
                commands('remove',App(name,name,source='arch',package=name))

    def test_failed_step_stops_sequence(self):
        from operations import OperationRunner
        with tempfile.TemporaryDirectory() as directory:
            marker=Path(directory)/'must-not-exist'
            result=OperationRunner(state_dir=Path(directory)/'state').run([[sys.executable,'-c','raise SystemExit(7)'],
                [sys.executable,'-c',f'from pathlib import Path; Path({str(marker)!r}).touch()']],lambda text: None)
            self.assertEqual(result,7)
            self.assertFalse(marker.exists())


class BackendTests(unittest.TestCase):
    def test_empty_yay_query_is_success_but_diagnostics_are_errors(self):
        from backend import run
        with patch('backend.subprocess.run',return_value=subprocess.CompletedProcess(['yay'],1,'','')):
            self.assertEqual(run(['yay','-Qua'],empty_success=(1,)),'')
        for out,err in [('', 'network failed'),('unexpected response','')]:
            with patch('backend.subprocess.run',return_value=subprocess.CompletedProcess(['yay'],1,out,err)):
                with self.assertRaises(RuntimeError):run(['yay','-Qua'],empty_success=(1,))

    def test_aur_exact_match_ranked_before_truncation(self):
        from backend import Backend
        rows=[{'Name':f'test-addon-{n}','NumVotes':100} for n in range(100)]
        rows += [{'Name':'test','NumVotes':0},{'Name':'unrelated','Description':'test','NumVotes':1000}]
        with patch('backend.urllib.request.urlopen',return_value=io.StringIO(json.dumps({'results':rows}))):
            apps=Backend().search_aur(' TEST ')
        self.assertEqual(apps[0].package,'test')
        self.assertEqual(len(apps),80)
        self.assertTrue(apps[1].package.startswith('test-'))

    def test_failed_update_check_is_not_up_to_date(self):
        from backend import Backend
        def failing(argv,**kwargs):
            if argv[0]=='checkupdates':raise RuntimeError('network unavailable')
            return ''
        with patch('backend.run',side_effect=failing):
            updates,errors=Backend().updates()
        self.assertEqual(updates,[])
        self.assertTrue(errors)
        self.assertIn('Arch',errors[0])

    def test_missing_aur_helper_is_reported_not_crashed(self):
        # yay is an optional dependency (optdepends): without it the AUR source
        # must degrade to an honest status instead of an errno traceback.
        from backend import Backend
        def helper_missing(argv,**kwargs):
            if argv[0]=='yay':raise FileNotFoundError(2,'No such file or directory','yay')
            return ''
        with patch('backend.run',side_effect=helper_missing):
            updates,errors=Backend().updates()
        self.assertEqual(updates,[])
        self.assertTrue(any('yay не установлен' in error for error in errors),errors)

    def test_hidden_desktop_entries_are_not_user_apps(self):
        from backend import read_desktop
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'hidden.desktop'
            path.write_text('[Desktop Entry]\nType=Application\nName=Hidden\nNoDisplay=true\n')
            self.assertIsNone(read_desktop(path))
            path.write_text('[Desktop Entry]\nType=Application\nName=Editor\nName[ru]=Редактор\nExec=editor %F\n')
            app=read_desktop(path)
            self.assertEqual(app.name,'Редактор')
            self.assertEqual(app.desktop,str(path))


class PanelTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('panel',FILES/'install-panel.py')
        self.panel=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.panel)

    def test_pin_preserves_widgets_and_is_idempotent(self):
        settings={'barConfigs':[{'enabled':False,'leftWidgets':['clock']},
                               {'leftWidgets':['launcherButton','workspaceSwitcher'],'rightWidgets':['systemTray']}]}
        session={'anotherPlugin':{'enabled':True},'unrelated':True}
        self.panel.configure(settings,session);self.panel.configure(settings,session)
        self.assertEqual(settings['barConfigs'][0]['leftWidgets'],['clock'])
        self.assertEqual(settings['barConfigs'][1]['leftWidgets'],['launcherButton','runneosSoftware','workspaceSwitcher'])
        self.assertEqual(settings['barConfigs'][1]['rightWidgets'],['systemTray'])
        self.assertEqual(session['runneosSoftware'],{'enabled':True})
        self.assertEqual(session['anotherPlugin'],{'enabled':True})
        self.assertTrue(session['unrelated'])

    def test_existing_object_widget_not_duplicated(self):
        settings={'barConfigs':[{'rightWidgets':[{'id':'runneosSoftware'}]}]}
        self.panel.configure(settings,{})
        self.assertNotIn('leftWidgets',settings['barConfigs'][0])

    def test_disabled_panel_is_not_changed(self):
        settings={'barConfigs':[{'enabled':False}]};session={}
        with self.assertRaises(ValueError):self.panel.configure(settings,session)
        self.assertEqual(session,{})


if __name__ == '__main__': unittest.main()
