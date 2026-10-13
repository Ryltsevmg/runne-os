#!/usr/bin/python3
"""Read-only RunneOS diagnostics. No journal bodies, secrets or typed input."""
import argparse
import concurrent.futures
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess


def command(args,timeout=8):
    try:
        result=subprocess.run(args,capture_output=True,text=True,timeout=timeout,
                              env=dict(os.environ,LC_ALL='C'))
        return result.returncode,result.stdout.strip()
    except (OSError,subprocess.TimeoutExpired):return 127,''


def user_manager_command(*args):
    """`systemctl --user` with a fallback through the host user manager.

    Inside agent sandboxes the local transport to the user manager is usually
    unavailable: `systemctl --user` then fails with empty output for every
    query, and a perfectly healthy session would be reported as 'attention'.
    The same user manager is reachable over the system bus with
    --machine=<user>@.host, so an empty answer is retried that way.
    """
    code,value=command(['systemctl','--user',*args])
    if value:return code,value
    user=os.environ.get('USER') or os.environ.get('LOGNAME')
    if user:
        fallback=command(['systemctl','--user',f'--machine={user}@.host',*args])
        if fallback[1]:return fallback
    return code,value


def check(name,argv,expected=None):
    code,value=argv() if callable(argv) else command(argv)
    good=code==0 and (expected is None or value==expected)
    return {'name':name,'status':'ok' if good else 'attention','detail':value[:120] if expected else ('Проверка пройдена' if good else 'Нужна диагностика'),'code':code}


def held_mouse_buttons():
    rows=[];denied=0
    for path in Path('/sys/class/input').glob('event*'):
        try:name=(path/'device/name').read_text().strip()
        except OSError:continue
        if 'mouse' not in name.casefold():continue
        try:
            fd=os.open('/dev/input/'+path.name,os.O_RDONLY|os.O_NONBLOCK)
            try:
                keys=bytearray(96);fcntl.ioctl(fd,0x80604518,keys)
                held=[number for number in range(272,280) if keys[number//8] & (1<<(number%8))]
                if held:rows.append({'device':name,'buttons':held})
            finally:os.close(fd)
        except OSError:denied+=1
    return {'name':'Кнопки мыши','status':'attention' if rows else ('unknown' if denied else 'ok'),
            'detail':'Удерживаемые кнопки обнаружены; это не доказательство неисправности' if rows else ('Часть устройств недоступна для чтения' if denied else 'Удерживаемых кнопок не обнаружено'),
            'held':rows,'unreadable':denied}


def collect():
    jobs=[('Niri',lambda:user_manager_command('is-active','niri.service'),'active'),
          ('DMS',lambda:user_manager_command('is-active','dms.service'),'active'),
          ('Портал файлов и экрана',lambda:user_manager_command('is-active','xdg-desktop-portal.service'),'active'),
          ('Хранилище ключей',lambda:user_manager_command('is-active','gnome-keyring-daemon.service'),'active'),
          ('PipeWire',lambda:user_manager_command('is-active','pipewire.service'),'active'),
          ('WirePlumber',lambda:user_manager_command('is-active','wireplumber.service'),'active'),
          ('NetworkManager',['systemctl','is-active','NetworkManager.service'],'active'),
          ('Очистка снимков',['systemctl','is-active','snapper-cleanup.timer'],'active'),
          ('Конфигурация Niri',['niri','validate'],None),
          ('Конфигурация Ghostty',['ghostty','+validate-config'],None),
          ('База пакетов',['pacman','-Dk'],None)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rows=list(pool.map(lambda job:check(*job),jobs))
    for user in (False,True):
        code,value=(user_manager_command('--failed','--no-legend','--plain','--no-pager') if user
                    else command(['systemctl','--failed','--no-legend','--plain','--no-pager']))
        names=[line.split()[0] for line in value.splitlines() if line.strip()]
        rows.append({'name':'Failed-службы пользователя' if user else 'Failed-службы системы',
                     'status':'ok' if code==0 and not names else 'attention','detail':', '.join(names) if names else 'Нет','units':names})
    disk=shutil.disk_usage(Path.home())
    rows.append({'name':'Свободное место','status':'ok' if disk.free/disk.total>.1 else 'attention',
                 'detail':f'{disk.free/1024**3:.1f} GiB свободно ({disk.free/disk.total:.0%})'})
    rows.append(held_mouse_buttons())
    return {'schemaVersion':1,'scope':'Текущий сеанс; не заменяет тест сна, звонка и восстановления','checks':rows}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--json',action='store_true');args=parser.parse_args()
    data=collect()
    if args.json:print(json.dumps(data,ensure_ascii=False,indent=2))
    else:
        for row in data['checks']:print(f'{row["status"]:9} {row["name"]}: {row["detail"]}')
        print(data['scope'])
    raise SystemExit(1 if any(row['status']=='attention' for row in data['checks']) else 0)
