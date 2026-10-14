#!/usr/bin/python3
"""Install the packaged DMS launcher without replacing user settings."""
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

PLUGIN_ID='runneosSoftware'


def configure(settings,plugin_settings):
    bar=next((b for b in settings.get('barConfigs',[]) if b.get('enabled',True)),None)
    if bar is None:
        raise ValueError('Включите панель DMS перед добавлением кнопки.')
    if not any((w if isinstance(w,str) else w.get('id'))==PLUGIN_ID
               for key in ('leftWidgets','centerWidgets','rightWidgets') for w in bar.get(key,[])):
        widgets=bar.setdefault('leftWidgets',[])
        widgets.insert(1 if widgets else 0,PLUGIN_ID)
    plugin_settings.setdefault(PLUGIN_ID,{})['enabled']=True


def write_json(path,data):
    with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,delete=False) as output:
        json.dump(data,output,ensure_ascii=False,indent=2);output.write('\n')
        temporary=Path(output.name)
    temporary.replace(path)


def main():
    home=Path.home()
    state=Path(os.environ.get('XDG_STATE_HOME',str(home/'.local/state')))
    config=Path(os.environ.get('XDG_CONFIG_HOME',str(home/'.config')))
    settings_path=config/'DankMaterialShell/settings.json'
    plugin_settings_path=config/'DankMaterialShell/plugin_settings.json'
    # Arrays cannot be set through DMS 1.6.3 IPC. Use the supported file watchers,
    # but only after verifying the live shell and the existing files.
    subprocess.run(['dms','ipc','call','settings','get','barConfigs'],check=True,capture_output=True)
    settings=json.loads(settings_path.read_text())
    plugin_settings=json.loads(plugin_settings_path.read_text()) if plugin_settings_path.exists() else {}
    configure(settings,plugin_settings)
    assets=Path(__file__).resolve().parent/'dms-plugin'
    if not assets.exists():assets=Path('/usr/share/runneos/software/dms-plugin')
    plugin=config/'DankMaterialShell/plugins'/PLUGIN_ID
    if (plugin/'plugin.json').exists():
        manifest=json.loads((plugin/'plugin.json').read_text())
        if manifest.get('author')!='RunneOS':raise ValueError('Plugin directory belongs to another plugin')
    backup=state/'runneos/backups'/('software-panel-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.mkdir(parents=True,mode=0o700)
    for path in (settings_path,plugin_settings_path):
        if path.exists():shutil.copy2(path,backup/path.name);(backup/path.name).chmod(0o600)
    plugin.mkdir(parents=True,exist_ok=True)
    for name in ('plugin.json','SoftwareLauncher.qml'):shutil.copy2(assets/name,plugin/name)
    write_json(plugin_settings_path,plugin_settings)
    subprocess.run(['dms','ipc','call','plugin-scan','scan'],check=True,capture_output=True)
    # Discovery is asynchronous even when IPC has already returned OK.
    deadline=time.monotonic()+10
    while True:
        listing=subprocess.run(['dms','ipc','call','plugin-scan','list'],check=True,capture_output=True,text=True).stdout
        if any(line.startswith(PLUGIN_ID+'\t') for line in listing.splitlines()):break
        if time.monotonic()>=deadline:raise RuntimeError('DMS plugin discovery timed out; backup: '+str(backup))
        time.sleep(0.2)
    for argv in (['plugins','enable',PLUGIN_ID],['plugins','reload',PLUGIN_ID]):
        result=subprocess.run(['dms','ipc','call',*argv],check=True,capture_output=True,text=True)
        if any(marker in result.stdout for marker in ('ERROR','FAILED','NOT_FOUND')):
            raise RuntimeError(result.stdout)
    write_json(settings_path,settings)
    print('Кнопка добавлена. Резервная копия:',backup)


if __name__=='__main__':main()
