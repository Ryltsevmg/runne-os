"""Real GTK tests: python tests/test_software_gui.py -v (isolated session)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

GUI=Path(__file__).parents[1]/'packages/runneos-software/files/gui.py'


@unittest.skipUnless(os.environ.get('RUNNEOS_GUI_TEST')=='1','requires a private D-Bus session and Wayland display')
class SoftwareGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['GTK_A11Y']='none'
        os.environ['GIO_USE_VFS']='local'

    def invoke(self,*args,timeout=60):
        return subprocess.run([sys.executable,str(GUI),*args],capture_output=True,text=True,timeout=timeout)

    def test_smoke_success_and_explicit_timeout_failure(self):
        success=self.invoke('--smoke-test')
        self.assertEqual(success.returncode,0,success.stderr+success.stdout)
        state=json.loads(success.stdout)
        self.assertTrue(state['ok']);self.assertTrue(state['search_visible'])
        self.assertGreaterEqual(state['columns'],2 if state['width']>=900 else 1)
        failure=self.invoke('--smoke-test','--smoke-timeout','0')
        self.assertEqual(failure.returncode,1)
        self.assertFalse(json.loads(failure.stdout)['ok'])

    def test_page_arguments_reach_running_instance(self):
        from gi.repository import Gio,GLib
        bus=Gio.bus_get_sync(Gio.BusType.SESSION,None)
        log=tempfile.TemporaryFile(mode='w+')
        primary=subprocess.Popen([sys.executable,str(GUI)],stdout=log,stderr=log)
        try:
            deadline=time.monotonic()+60
            # Starting --status before registration can accidentally become the
            # primary instance. Wait for the bus name without starting GTK.
            while time.monotonic()<deadline:
                owned=bus.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus',
                    'org.freedesktop.DBus','NameHasOwner',GLib.Variant('(s)',('org.runneos.Software',)),
                    None,Gio.DBusCallFlags.NONE,2000,None).unpack()[0]
                if owned:break
                if primary.poll() is not None:
                    log.seek(0);self.fail(log.read())
                time.sleep(0.1)
            while time.monotonic()<deadline:
                state=self.invoke('--status')
                if state.returncode==0 and json.loads(state.stdout)['catalog_ready']:break
                time.sleep(0.1)
            else:
                log.seek(0);self.fail('Primary GTK application did not become ready: '+log.read())
            for page in ('installed','updates','catalog'):
                self.assertEqual(self.invoke('--page',page).returncode,0)
                state=json.loads(self.invoke('--status').stdout)
                self.assertEqual(state['page'],page)
                self.assertIsNone(primary.poll(),'Primary instance exited')
            invalid=self.invoke('--page','invalid')
            self.assertEqual(invalid.returncode,2)
            self.assertEqual(json.loads(self.invoke('--status').stdout)['page'],'catalog')
        finally:
            primary.terminate();primary.wait(timeout=10);log.close()


if __name__=='__main__':
    if os.environ.get('RUNNEOS_GUI_TEST')!='1':
        # Set the environment before the private bus daemon starts, so its
        # activated portal/a11y services cannot reuse the desktop's sockets.
        with tempfile.TemporaryDirectory(prefix='runneos-software-gui-',dir='/tmp/opencode') as runtime:
            env=os.environ.copy();display=env.get('WAYLAND_DISPLAY','wayland-0')
            env.update(RUNNEOS_GUI_TEST='1',GTK_A11Y='none',GIO_USE_VFS='local',
                       XDG_RUNTIME_DIR=runtime,PYTHONDONTWRITEBYTECODE='1')
            env['WAYLAND_DISPLAY']=display if display.startswith('/') else str(Path(os.environ['XDG_RUNTIME_DIR'])/display)
            result=subprocess.run(['dbus-run-session','--',sys.executable,str(Path(__file__).resolve()),*sys.argv[1:]],env=env).returncode
            # Documents portal can leave its own FUSE mount after the bus exits.
            subprocess.run(['fusermount3','-uz',str(Path(runtime)/'doc')],capture_output=True)
        raise SystemExit(result)
    unittest.main()
