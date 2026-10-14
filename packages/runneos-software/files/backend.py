"""Real repositories and installed applications. Blocking work runs off the UI thread."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import configparser
import hashlib
import json
import os
import re
import subprocess
import urllib.request
import urllib.parse

from catalog import App, merge_apps, parse_appstream


def run(argv, allowed=(0,), timeout=45, empty_success=()):
    env = os.environ.copy()
    env.update({'LC_ALL':'C.UTF-8','LANG':'C.UTF-8'})
    result = subprocess.run(argv, capture_output=True, text=True, errors='replace',
                            timeout=timeout, env=env)
    empty_ok=result.returncode in empty_success and not result.stdout.strip() and not result.stderr.strip()
    if result.returncode not in allowed and not empty_ok:
        raise RuntimeError((result.stderr or result.stdout or 'Команда завершилась с ошибкой').strip()[-1500:])
    return result.stdout


def read_desktop(path):
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.optionxform = str
    try:
        config.read(path, encoding='utf-8')
        entry = config['Desktop Entry']
        if entry.get('Type') != 'Application' or entry.get('Hidden') == 'true' or entry.get('NoDisplay') == 'true':
            return None
        if 'OnlyShowIn' in entry and not any(s in entry['OnlyShowIn'] for s in ('niri','GNOME')):
            return None
        name = entry.get('Name[ru]',entry.get('Name',''))
        if not name: return None
        return App(Path(path).name.removesuffix('.desktop'),name,
                   entry.get('Comment[ru]',entry.get('Comment','')), source='local',
                   installed=True, icon=entry.get('Icon','application-x-executable'),
                   desktop=str(path), categories=entry.get('Categories','').split(';'),
                   version=entry.get('X-AppImage-Version',''))
    except (OSError,configparser.Error,KeyError):
        return None


class Backend:
    def __init__(self):
        self.apps = []
        self.warnings = []
        self.packages = {}
        self.flatpaks = {}

    def flatpak_inventory(self, scope):
        items = []
        for line in run(['flatpak','list','--'+scope,'--app','--columns=application,name,version,origin']).splitlines():
            values = line.split('\t')
            if len(values) < 4: continue
            package,name,version,origin = values[:4]
            items.append(App(package,name, source='flatpak',package=package,scope=scope,
                             origin=origin,version=version,installed=True,icon=package))
        return items

    def load(self):
        self.warnings = []
        self.packages = {}
        for line in run(['pacman','-Q']).splitlines():
            name,_,version = line.partition(' ')
            self.packages[name] = version
        apps = []
        for path in Path('/usr/share/swcatalog/xml').glob('*.xml.gz'):
            try: apps.extend(parse_appstream(path,'arch',path.name.split('.')[0]))
            except Exception as error: self.warnings.append(f'Каталог Arch: {error}')
        self.flatpaks = {}
        for scope in ('system','user'):
            try:
                for app in self.flatpak_inventory(scope): self.flatpaks[(scope,app.package)] = app
            except Exception as error: self.warnings.append(f'Flatpak ({scope}): {error}')
        roots=[(Path('/var/lib/flatpak/appstream'),'system'),
               (Path.home()/'.local/share/flatpak/appstream','user')]
        for root,scope in roots:
            for path in root.glob('*/*/active/appstream.xml.gz'):
                origin=path.parents[2].name
                try:
                    data=parse_appstream(path,'flatpak',origin)
                    for app in data: app.scope=scope
                    apps.extend(data)
                except Exception as error: self.warnings.append(f'Каталог {origin}: {error}')
        for app in apps:
            if app.source == 'arch' and app.package in self.packages:
                app.installed=True; app.version=self.packages[app.package]
            elif app.source == 'flatpak' and (app.scope,app.package) in self.flatpaks:
                installed=self.flatpaks[(app.scope,app.package)]
                app.installed=True;app.version=installed.version
        # Add apps lacking AppStream data, without losing Flatpak scope.
        present={(app.scope,app.package) for app in apps if app.source=='flatpak' and app.installed}
        apps.extend(app for key,app in self.flatpaks.items() if key not in present)
        represented={app.id.removesuffix('.desktop') for app in apps if app.installed}
        desktop_paths=list((Path.home()/'.local/share/applications').glob('*.desktop'))
        desktop_paths+=list(Path('/usr/share/applications').glob('*.desktop'))
        for path in desktop_paths:
            app=read_desktop(path)
            if not app or app.id.removesuffix('.desktop') in represented: continue
            if str(path).startswith('/usr/share/'):
                owner=run(['pacman','-Qoq',str(path)],allowed=(0,1)).strip()
                if owner:
                    app.source='arch';app.package=owner;app.version=self.packages.get(owner,'')
            apps.append(app)
        # User npm tools are separate software, never fabricated pacman packages.
        for path in (Path.home()/'.local/lib/node_modules').glob('*/package.json'):
            try:
                package=json.loads(path.read_text())
                if package.get('bin'):
                    apps.append(App(package['name'],package['name'], 'Инструмент командной строки',
                        source='npm',package=package['name'],installed=True,version=package.get('version',''),
                        categories=['Development']))
            except (OSError,ValueError,KeyError): pass
        for path in (Path.home()/'.local/lib/node_modules').glob('@*/*/package.json'):
            try:
                package=json.loads(path.read_text())
                if package.get('bin'):
                    apps.append(App(package['name'],package['name'], 'Инструмент командной строки',
                        source='npm',package=package['name'],installed=True,version=package.get('version',''),
                        categories=['Development']))
            except (OSError,ValueError,KeyError): pass
        self.apps=merge_apps(apps)
        return self.apps

    def search_aur(self, query):
        if len(query.strip()) < 2: return []
        url='https://aur.archlinux.org/rpc/v5/search/'+urllib.parse.quote(query.strip(),safe='')+'?by=name-desc'
        with urllib.request.urlopen(url,timeout=12) as response: data=json.load(response)
        if data.get('type')=='error': raise RuntimeError(data.get('error','AUR недоступен'))
        # Rank before limiting: RPC order must not bury an exact match past row 80.
        needle=query.strip().casefold()
        items=sorted(data.get('results',[]),key=lambda item:(
            0 if item['Name'].casefold()==needle else
            1 if item['Name'].casefold().startswith(needle+'-') else
            2 if item['Name'].casefold().startswith(needle) else
            3 if needle in item['Name'].casefold() else 4,
            -item.get('NumVotes',0),item['Name'].casefold()))
        return [App(item['Name'],item['Name'],item.get('Description') or '',source='aur',
                    package=item['Name'],version=self.packages.get(item['Name'],item.get('Version','')),
                    installed=item['Name'] in self.packages, homepage=item.get('URL') or '',
                    categories=['Development']) for item in items[:80]]

    def updates(self):
        result=[];errors=[]
        sources=[('Arch',['checkupdates'],(0,2)),('AUR',['yay','-Qua'],(0,)),
                 ('Flatpak',['flatpak','remote-ls','--updates','--system','--app','--columns=application,name,version'],(0,)),
                 ('Flatpak (пользователь)',['flatpak','remote-ls','--updates','--user','--app','--columns=application,name,version'],(0,))]
        def check(source):
            label,argv,codes=source
            # yay -Qua exits 1 with empty stdout/stderr when nothing is outdated.
            # A diagnostic on either stream remains an error, not "up to date".
            try: return label,run(argv,allowed=codes,timeout=90,empty_success=(1,) if label=='AUR' else ()),None
            except FileNotFoundError: return label,'',argv[0]+' не установлен'
            except Exception as error: return label,'',str(error)
        with ThreadPoolExecutor(max_workers=4) as pool:
            for label,text,error in pool.map(check,sources):
                if error: errors.append(label+': '+error);continue
                for line in text.splitlines():
                    if not line.strip(): continue
                    if label.startswith('Flatpak'):
                        fields=line.split('\t');name=fields[1] if len(fields)>1 else fields[0]
                        version=fields[2] if len(fields)>2 else ''
                        result.append({'name':name,'version':version,'source':label})
                    else:
                        fields=line.split(); result.append({'name':fields[0],
                            'version':' '.join(fields[1:]),'source':label})
        return result,errors


def cached_image(url):
    if not url.startswith('https://'): raise ValueError('Только HTTPS изображения')
    folder=Path.home()/'.cache/runneos-software/images';folder.mkdir(parents=True,exist_ok=True)
    target=folder/hashlib.sha256(url.encode()).hexdigest()
    if target.is_file(): return str(target)
    with urllib.request.urlopen(url,timeout=15) as response:
        data=response.read(12*1024*1024+1)
        if len(data)>12*1024*1024: raise ValueError('Изображение слишком большое')
    temporary=target.with_suffix('.tmp');temporary.write_bytes(data);temporary.replace(target)
    return str(target)
