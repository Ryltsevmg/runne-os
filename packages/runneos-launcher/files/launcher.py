#!/usr/bin/python3
"""Open a desktop application or focus its exact existing Niri window."""
import argparse
import configparser
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


def desktop_id(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]*\.desktop',value):
        raise ValueError('Некорректный desktop ID')
    return value


def app_id(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',value):raise ValueError('Некорректный Flatpak ID')
    return value


def candidates(identity,startup=None):
    result={identity.removesuffix('.desktop')}
    if startup:result.add(startup)
    return result


def choose_window(windows,identities,predicate=lambda row:True):
    rows=[w for w in windows if w.get('app_id') in identities and isinstance(w.get('id'),int) and predicate(w)]
    def rank(row):
        stamp=row.get('focus_timestamp') or {}
        return (bool(row.get('is_focused')),stamp.get('secs',0),stamp.get('nanos',0),row['id'])
    return max(rows,key=rank) if rows else None


def windows():
    try:
        result=subprocess.run(['niri','msg','--json','windows'],capture_output=True,text=True,timeout=3)
        if result.returncode:return []
        data=json.loads(result.stdout)
        return data if isinstance(data,list) else []
    except (OSError,ValueError,subprocess.TimeoutExpired):return []


def flatpak_scope(row,scope,identity):
    pid=row.get('pid')
    if not isinstance(pid,int) or pid<=0:return False
    path=Path('/proc')/str(pid)/'root/.flatpak-info'
    try:
        with path.open() as stream:text=stream.read(65537)
        if len(text)>65536:return False
        info=configparser.ConfigParser(interpolation=None);info.read_string(text)
        if info.get('Application','name',fallback='')!=identity:return False
        app_path=info.get('Instance','app-path',fallback='')
        base=Path('/var/lib/flatpak/app') if scope=='system' else Path.home()/'.local/share/flatpak/app'
        return bool(app_path) and Path(app_path).is_relative_to(base/identity)
    except (OSError,ValueError,configparser.Error):return False


def launch(identity=None,filename=None,flatpak=None,scope='system',new_window=False):
    import gi
    gi.require_version('GioUnix','2.0')
    from gi.repository import GioUnix,GLib
    info=None
    if flatpak:
        flatpak=app_id(flatpak);identities={flatpak}
        if scope not in ('system','user'):raise ValueError('Неизвестная область Flatpak')
        if new_window:raise ValueError('Универсальное новое окно для Flatpak не поддерживается; используйте действие приложения')
        installed=subprocess.run(['flatpak','info','--'+scope,'--show-ref',flatpak],capture_output=True,timeout=5)
        if installed.returncode or not installed.stdout.strip().startswith(b'app/'):
            raise ValueError('Flatpak-приложение не установлено в выбранной области')
    else:
        if filename:
            path=Path(filename)
            if not path.is_absolute() or path.suffix!='.desktop':raise ValueError('Нужен абсолютный путь к desktop-файлу')
            info=GioUnix.DesktopAppInfo.new_from_filename(str(path))
            identity=path.name
        else:identity=desktop_id(identity);info=GioUnix.DesktopAppInfo.new(identity)
        if not info or info.get_is_hidden():raise ValueError('Приложение отсутствует или скрыто')
        identities=candidates(identity,info.get_startup_wm_class())
    root=Path(os.environ.get('XDG_RUNTIME_DIR',f'/run/user/{os.getuid()}'))/'runneos-launcher'
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    key=hashlib.sha256(json.dumps([identity,filename,flatpak,scope]).encode()).hexdigest()
    fd=os.open(root/(key+'.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    pending=root/(key+'.pending')
    with os.fdopen(fd,'w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        predicate=(lambda w:flatpak_scope(w,scope,flatpak)) if flatpak else (lambda w:True)
        match=None if new_window else choose_window(windows(),identities,predicate)
        if match:
            result=subprocess.run(['niri','msg','action','focus-window','--id',str(match['id'])],capture_output=True,timeout=3)
            if result.returncode:raise RuntimeError('Окно изменилось; повторите действие')
            pending.unlink(missing_ok=True)
            return {'action':'focused','windowId':match['id']}
        if not new_window and pending.exists() and 0<=time.time()-pending.stat().st_mtime<5:
            return {'action':'starting'}
        if flatpak:
            subprocess.Popen(['flatpak','run','--'+scope,flatpak],stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        elif new_window:
            if 'new-window' not in info.list_actions():raise ValueError('Приложение не объявляет действие «Новое окно»')
            info.launch_action('new-window',None)
        else:
            if not info.launch_uris_as_manager([],None,GLib.SpawnFlags.SEARCH_PATH|GLib.SpawnFlags.STDOUT_TO_DEV_NULL|GLib.SpawnFlags.STDERR_TO_DEV_NULL,None,None,None,None):
                raise RuntimeError('Не удалось запустить приложение')
        descriptor=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600);os.close(descriptor)
        # Debounce concurrent requests until the initial window maps. No title
        # matching, shell command parsing or automatic closing of windows.
        deadline=time.monotonic()+1.5
        while time.monotonic()<deadline:
            if choose_window(windows(),identities,predicate):pending.unlink(missing_ok=True);break
            time.sleep(.1)
        return {'action':'launch_requested'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--desktop');group.add_argument('--desktop-file');group.add_argument('--flatpak')
    parser.add_argument('--scope',choices=('system','user'),default='system')
    parser.add_argument('--new-window',action='store_true')
    args=parser.parse_args()
    try:result=launch(args.desktop,args.desktop_file,args.flatpak,args.scope,args.new_window)
    except (ValueError,RuntimeError,OSError,subprocess.TimeoutExpired) as error:
        print('RunneOS: '+str(error));return 1
    except Exception:
        print('RunneOS: не удалось выполнить desktop-действие');return 1
    print(json.dumps(result));return 0


if __name__=='__main__':raise SystemExit(main())
