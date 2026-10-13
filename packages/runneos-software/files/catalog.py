"""Application identities and AppStream metadata, independent of the GUI."""
from dataclasses import dataclass, field
from pathlib import Path
import gzip
import re
import xml.etree.ElementTree as ET


@dataclass
class App:
    id: str
    name: str
    summary: str = ''
    source: str = 'arch'
    package: str = ''
    scope: str = 'system'
    origin: str = ''
    version: str = ''
    installed: bool = False
    icon: str = 'application-x-executable'
    description: str = ''
    categories: list = field(default_factory=list)
    screenshots: list = field(default_factory=list)
    desktop: str = ''
    homepage: str = ''
    alternatives: list = field(default_factory=list)

    @property
    def key(self):
        return ':'.join((self.source, self.scope, self.package or self.id))

    @property
    def source_label(self):
        return {'arch': 'Arch', 'aur': 'Сообщество', 'flatpak': 'Flatpak',
                'local': 'Локально', 'npm': 'Инструмент npm'}.get(self.source, self.source)


def localized(component, tag, locale):
    elements = component.findall(tag)
    for desired in (locale, locale.split('_')[0], ''):
        for element in elements:
            if element.get('{http://www.w3.org/XML/1998/namespace}lang', '') == desired:
                return ''.join(element.itertext()).strip()
    return ''.join(elements[0].itertext()).strip() if elements else ''


def parse_appstream(path, source, origin, locale='ru'):
    path = Path(path)
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rb') as stream:
        root = ET.parse(stream).getroot()
    result = []
    for item in root.findall('component'):
        if item.get('type') != 'desktop-application':
            continue
        app_id = item.findtext('id', '').strip()
        package = item.findtext('pkgname', '').strip()
        if source == 'flatpak':
            bundle = item.findtext("bundle[@type='flatpak']", '')
            parts = bundle.split('/')
            package = parts[1] if len(parts) > 1 else app_id.removesuffix('.desktop')
        if not app_id or not package:
            continue
        icon = item.find("icon[@type='stock']")
        icon_name = icon.text if icon is not None and icon.text else app_id.removesuffix('.desktop')
        for cached in item.findall("icon[@type='cached']"):
            name = cached.text or ''
            if source == 'flatpak':
                candidates = [path.parent / 'icons' / size / name for size in ('128x128','64x64')]
            else:
                candidates = [Path('/usr/share/swcatalog/icons') / ('archlinux-arch-'+origin) / size / name
                              for size in ('128x128','64x64')]
            found = next((str(p) for p in candidates if p.is_file()), None)
            if found:
                icon_name = found
                break
        screenshots = []
        for shot in item.findall('screenshots/screenshot'):
            image = shot.find("image[@type='source']")
            if image is None:
                image = shot.find('image')
            if image is not None and (image.text or '').startswith('https://'):
                screenshots.append(image.text.strip())
        releases = item.find('releases/release')
        result.append(App(app_id, localized(item, 'name', locale) or package,
            localized(item, 'summary', locale), source, package, origin=origin,
            version=releases.get('version','') if releases is not None else '',
            icon=icon_name, description=localized(item, 'description', locale),
            categories=[c.text for c in item.findall('categories/category') if c.text],
            screenshots=screenshots[:4], homepage=item.findtext("url[@type='homepage']", ''),
            desktop=item.findtext("launchable[@type='desktop-id']", app_id)))
    return result


def merge_apps(apps):
    groups = {}
    for app in apps:
        name = re.sub(r'[^\w]+', '', app.name.casefold())
        groups.setdefault(name or app.id, []).append(app)
    result = []
    for items in groups.values():
        unique = {app.key: app for app in items}
        items = list(unique.values())
        installed = [app for app in items if app.installed]
        # Two installed scopes are two real installations; both must remain manageable.
        chosen = installed or [min(items, key=lambda a: {'local':0,'arch':1,'flatpak':2,'aur':3}.get(a.source,4))]
        for app in chosen:
            others = [item for item in items if item.key != app.key]
            app.alternatives = others
            for other in others:
                if not app.summary: app.summary = other.summary
                if not app.description: app.description = other.description
                if not app.screenshots: app.screenshots = list(other.screenshots)
                if not app.categories: app.categories = list(other.categories)
            result.append(app)
    return sorted(result, key=lambda app: (app.name.casefold(), app.key))
