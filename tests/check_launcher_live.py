#!/usr/bin/python3
"""Isolated disposable GTK app verifies cold launch, repeat focus and new-window."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

FILE=Path(__file__).parents[1]/'packages/runneos-launcher/files/launcher.py'
spec=importlib.util.spec_from_file_location('launch_check',FILE)
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)


def main():
    identity='org.runneos.LauncherCheck'+uuid.uuid4().hex
    original=json.loads(subprocess.check_output(['niri','msg','--json','focused-window'],text=True))
    def matching():return [w for w in launcher.windows() if w.get('app_id')==identity]
    def wait_count(count):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            rows=matching()
            if len(rows)==count:return rows
            time.sleep(.1)
        raise AssertionError(f'Expected {count} test windows')
    with tempfile.TemporaryDirectory(prefix='runneos-launch-check-',dir='/tmp/opencode' if os.access('/tmp/opencode',os.W_OK) else None) as directory:
        root=Path(directory);script=root/'fixture.py';desktop=root/(identity+'.desktop')
        script.write_text('import gi\ngi.require_version("Gtk","4.0")\nfrom gi.repository import Gtk,Gio,GLib\n'
                          f'app=Gtk.Application(application_id={identity!r},flags=Gio.ApplicationFlags.NON_UNIQUE)\n'
                          'app.connect("activate",lambda app:Gtk.ApplicationWindow(application=app,title="RunneOS launcher fixture",default_width=400,default_height=240).present())\n'
                          'GLib.timeout_add_seconds(60,lambda:(app.quit(),False)[1])\napp.run()\n')
        desktop.write_text(f'[Desktop Entry]\nType=Application\nName=RunneOS temporary launch test\nExec={sys.executable} {script}\nStartupWMClass={identity}\nActions=new-window;\n\n[Desktop Action new-window]\nName=New Window\nExec={sys.executable} {script}\n')
        try:
            result=launcher.launch(filename=str(desktop));first=wait_count(1)[0]['id']
            assert result['action']=='launch_requested'
            for _ in range(3):
                result=launcher.launch(filename=str(desktop));assert result=={'action':'focused','windowId':first}
            assert len(matching())==1
            result=launcher.launch(filename=str(desktop),new_window=True);assert result['action']=='launch_requested'
            wait_count(2)
            print(json.dumps({'ok':True,'coldLaunchWindows':1,'repeatLaunches':3,'repeatWindowCount':1,'explicitNewWindowCount':2}))
        finally:
            for window in matching():subprocess.run(['niri','msg','action','close-window','--id',str(window['id'])],check=False,capture_output=True)
            if original:subprocess.run(['niri','msg','action','focus-window','--id',str(original['id'])],check=False,capture_output=True)


if __name__=='__main__':main()
