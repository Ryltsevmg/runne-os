"""Validated transaction plans. No shell, no recursive removal of user data."""
from pathlib import Path
import datetime
import fcntl
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import uuid

PROTECTED = {'pacman','glibc','systemd','linux','linux-lts','linux-zen','niri','dms-shell',
             'networkmanager','sddm','kwallet','kwallet-pam','sudo','polkit','warp-terminal','ghostty',
             'runneos-software','runneos-config','runneos-desktop','python','gtk4','libadwaita',
             'tailscale','ktailctl','network-manager-applet','opencode','opencode-bin',
             'opencode-desktop','opencode-desktop-bin','gnome-keyring','snapper','snap-pac',
             'org.fkoehler.KTailctl','ai.opencode.desktop'}
POLKIT_SUDO = '/usr/lib/runneos-software/polkit-sudo'


def valid_package(name):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9@._+:-]*', name or ''):
        raise ValueError('Некорректный идентификатор приложения')
    return name


def commands(action, app):
    package = valid_package(app.package)
    if action not in ('install', 'remove'):
        raise ValueError('Неизвестное действие')
    if app.source in ('local','npm'):
        raise ValueError('Эта программа установлена отдельно. Управление — в её настройках или установщике.')
    if action == 'remove' and package in PROTECTED:
        raise ValueError('Эта программа нужна для работы RunneOS и не удаляется через центр приложений.')
    if app.source == 'flatpak':
        if app.scope not in ('system','user'):
            raise ValueError('Неизвестная область установки Flatpak')
        if action == 'install':
            return [['flatpak','install','--'+app.scope,'--noninteractive','--assumeyes',
                     valid_package(app.origin or 'flathub'),package]]
        return [['flatpak','uninstall','--'+app.scope,'--noninteractive','--assumeyes',package]]
    if app.source == 'aur' and action == 'install':
        return [['yay','-S','--needed','--noconfirm','--cleanafter','--sudoloop=false',
                 '--sudo',POLKIT_SUDO,'--', 'aur/'+package]]
    if app.source not in ('arch','aur'):
        raise ValueError('Источник не поддерживает это действие')
    return [['pkexec','/usr/bin/pacman','-S' if action == 'install' else '-R',
             *(['--needed'] if action == 'install' else []),'--noconfirm','--',package]]


def update_commands(snapshot=True, scopes=('system','user')):
    result = []
    if snapshot:
        result.append(['pkexec','/usr/bin/snapper','-c','root','create','--type','single',
                       '--description','RunneOS: before software update',
                       '--cleanup-algorithm','number','--print-number'])
    result.extend([
        ['pkexec','/usr/bin/pacman','-Syu','--noconfirm'],
        ['yay','-Sua','--noconfirm','--cleanafter','--sudoloop=false','--sudo',POLKIT_SUDO],
    ])
    for scope in scopes:
        if scope not in ('system','user'): raise ValueError('Unknown Flatpak scope')
        result.append(['flatpak','update','--'+scope,'--noninteractive','--assumeyes'])
    return result


def state_directory():
    return Path(os.environ.get('XDG_STATE_HOME', str(Path.home()/'.local/state')))/'runneos/software'


def read_operation(root=None):
    path=(Path(root) if root else state_directory())/'operation.json'
    if not path.exists():return None
    data=json.loads(path.read_text())
    if (not isinstance(data,dict) or data.get('schemaVersion')!=1 or
            data.get('status') not in ('running','interrupted','failed','succeeded','stopped_after_step')):
        raise ValueError('Invalid operation journal')
    return data


def write_operation(root,data):
    with tempfile.NamedTemporaryFile(mode='w',dir=root,delete=False) as output:
        json.dump(data,output,ensure_ascii=False,indent=2);output.write('\n');output.flush();os.fsync(output.fileno())
        temp=Path(output.name)
    temp.replace(root/'operation.json')
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


class OperationRunner:
    def __init__(self,state_dir=None):
        self.lock = threading.Lock()
        self.process = None
        self.state_dir=Path(state_dir) if state_dir else state_directory()

    def run(self, steps, on_line):
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('Другая операция ещё выполняется')
        file_lock=None;journal=None;handlers={};stop_requested=False
        def emit(text):
            # A closed CLI pipe or disappeared GUI must not kill a package transaction.
            try:on_line(text)
            except Exception:pass
        def stamp():return datetime.datetime.now(datetime.timezone.utc).isoformat()
        def persist():
            journal['updatedAt']=stamp();write_operation(self.state_dir,journal)
        def stop_after_step(signum,frame):
            nonlocal stop_requested
            stop_requested=True
            emit('Остановка запрошена: завершаем текущий шаг без прерывания менеджера пакетов.\n')
        def settle_child():
            if self.process is not None:
                if self.process.stdout and not self.process.stdout.closed:
                    for _ in self.process.stdout:pass
                    self.process.stdout.close()
                self.process.wait()
        try:
            steps=[list(step) for step in steps]
            if not steps or any(not s or not all(isinstance(a,str) and '\0' not in a for a in s) for s in steps):
                raise ValueError('Пустой или некорректный план операции')
            self.state_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
            descriptor=os.open(self.state_dir/'operation.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
            file_lock=os.fdopen(descriptor,'w')
            try:fcntl.flock(file_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('Другая операция RunneOS ещё выполняется') from None
            previous=read_operation(self.state_dir)
            if previous and previous.get('status') in ('running','interrupted'):
                raise RuntimeError('Предыдущая операция не завершена. Проверьте runneos-update --status; автоматического повторения не будет.')
            journal={'schemaVersion':1,'id':str(uuid.uuid4()),'status':'running','startedAt':stamp(),
                     'ownerPid':os.getpid(),'step':0,'totalSteps':len(steps),'completedSteps':0,
                     'executable':None,'childPid':None,'exitCode':None,'snapshotNumber':None}
            persist()
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGINT,signal.SIGTERM):handlers[sig]=signal.signal(sig,stop_after_step)
            for index,step in enumerate(steps):
                if stop_requested:
                    journal.update(status='stopped_after_step',exitCode=130);persist();return 130
                env = os.environ.copy()
                env.update({'LC_ALL':'C.UTF-8','LANG':'C.UTF-8','NO_COLOR':'1'})
                env['PATH']=os.pathsep.join([str(Path.home()/'.local/bin'),env.get('PATH','/usr/bin:/bin')])
                emit('→ ' + ' '.join(step) + '\n')
                offset=1 if Path(step[0]).name in ('pkexec','sudo') else 0
                if offset and len(step)>1 and step[1]=='-n':offset+=1
                executable=Path(step[offset] if offset<len(step) else step[0]).name
                journal.update(step=index+1,executable=executable,childPid=None);persist()
                snapshot_step=executable=='snapper' and 'create' in step and '--print-number' in step
                snapshot_numbers=[]
                try:
                    self.process = subprocess.Popen(step, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                        errors='replace', env=env,start_new_session=True)
                    journal['childPid']=self.process.pid;persist()
                    for line in self.process.stdout:
                        emit(line)
                        if snapshot_step and re.fullmatch(r'[1-9][0-9]{0,9}',line.strip()):snapshot_numbers.append(int(line.strip()))
                    code = self.process.wait()
                    self.process.stdout.close()
                except OSError as error:
                    emit(str(error)+'\n')
                    launched=self.process is not None
                    settle_child()
                    journal.update(status='interrupted' if launched else 'failed',exitCode=127,childPid=None);persist()
                    return 127
                finally:
                    if self.process is not None and self.process.poll() is not None:self.process = None
                if code:
                    emit(f'Операция остановлена (код {code}).\n')
                    journal.update(status='failed',exitCode=code,childPid=None);persist()
                    return code
                if snapshot_step:
                    if len(snapshot_numbers)!=1:
                        emit('Снимок не подтверждён: корректный номер не получен. Обновление остановлено.\n')
                        journal.update(status='failed',exitCode=65,childPid=None);persist();return 65
                    journal['snapshotNumber']=snapshot_numbers[0]
                journal.update(completedSteps=index+1,childPid=None);persist()
            journal.update(status='succeeded',exitCode=0);persist()
            return 0
        except BaseException:
            if journal and journal.get('status')=='running':
                settle_child()
                journal.update(status='interrupted',exitCode=None)
                try:persist()
                except OSError:pass
            raise
        finally:
            for sig,handler in handlers.items():signal.signal(sig,handler)
            if file_lock is not None:file_lock.close()
            self.lock.release()
