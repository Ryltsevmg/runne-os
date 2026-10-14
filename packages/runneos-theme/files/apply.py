#!/usr/bin/python3
"""Apply packaged Runne colors through DMS and a bounded Ghostty config edit."""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def write(path,text):
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,delete=False) as out:
        out.write(text);temporary=Path(out.name)
    temporary.replace(path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('carbon','cloud'))
    args=parser.parse_args()
    assets=Path('/usr/share/runneos/theme')
    if not (assets/'dms-theme.json').is_file():raise SystemExit('Install runneos-theme first')
    home=Path.home();config=Path(os.environ.get('XDG_CONFIG_HOME',str(home/'.config')))
    state=Path(os.environ.get('XDG_STATE_HOME',str(home/'.local/state')))
    dms=config/'DankMaterialShell/settings.json';session=state/'DankMaterialShell/session.json';ghostty=config/'ghostty/config'
    data=json.loads(dms.read_text())
    backup=state/'runneos/backups'/('theme-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.mkdir(parents=True,mode=0o700)
    for name,path in [('dms-settings.json',dms),('dms-session.json',session),('ghostty-config',ghostty)]:
        if path.exists():shutil.copy2(path,backup/name);(backup/name).chmod(0o600)
    old=ghostty.read_text() if ghostty.exists() else ''
    lines=[line for line in old.splitlines() if not line.strip().startswith('theme =')
           and line.strip()!='# RunneOS palette (managed by runneos-theme)'
           and not (line.strip().startswith('config-file =') and '/runneos/theme/ghostty-' in line)]
    lines += ['# RunneOS palette (managed by runneos-theme)',f'config-file = {assets}/ghostty-{args.mode}.conf']
    candidate=backup/'ghostty-candidate';candidate.write_text('\n'.join(lines)+'\n')
    subprocess.run(['ghostty','+validate-config','--config-file='+str(candidate)],check=True)
    # The installed DMS owns the generated GTK/Qt settings via its watcher.
    data.update(currentThemeName='custom',customThemeFile=str(assets/'dms-theme.json'),
                launcherLogoMode='custom',launcherLogoCustomPath=str(assets/'brand'/f'launcher-{args.mode}.png'),
                launcherLogoColorOverride='',launcherLogoColorInvertOnMode=False,
                launcherLogoBrightness=0.5,launcherLogoContrast=1)
    write(dms,json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    result=subprocess.run(['dms','ipc','call','theme','dark' if args.mode=='carbon' else 'light'],check=True,capture_output=True,text=True)
    if any(marker in result.stdout for marker in ('ERROR','FAILED')):raise SystemExit(result.stdout)
    write(ghostty,candidate.read_text())
    subprocess.run(['dms','ipc','call','wallpaper','set',str(assets/f'wallpaper-{args.mode}.png')],check=True,capture_output=True)
    print('Applied',args.mode,'; backup:',backup)
    print('Ghostty palette is used by new/reloaded windows; no terminal was stopped.')


if __name__=='__main__':main()
