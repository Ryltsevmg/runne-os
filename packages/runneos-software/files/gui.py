#!/usr/bin/env python3
"""Native application center for RunneOS."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import argparse
import json
import sys
import time

import gi
gi.require_version('Gtk','4.0')
gi.require_version('Adw','1')
gi.require_version('GioUnix','2.0')
from gi.repository import Gtk, Adw, Gio, GioUnix, GLib, Gdk, Pango

from backend import Backend, cached_image
from operations import OperationRunner, commands, update_commands, PROTECTED, read_operation

sys.path.insert(0,'/usr/share/runneos/theme')
from ui_style import Theme,wordmark

HERE=Path(__file__).resolve().parent
CATEGORIES=[('Все',''),('Работа','Office'),('Разработка','Development'),
            ('Интернет','Network'),('Фото и видео','Graphics'),('Музыка','AudioVideo'),('Игры','Game')]
FEATURED=['org.mozilla.firefox','org.videolan.VLC','org.gimp.GIMP','org.blender.Blender',
          'org.kde.krita','com.spotify.Client','org.telegram.desktop','org.libreoffice.LibreOffice',
          'com.valvesoftware.Steam','md.obsidian.Obsidian','org.inkscape.Inkscape','org.gnome.Fractal']


def box(vertical=True, spacing=0, **kwargs):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL,
                   spacing=spacing, **kwargs)


def label(text, css='', **kwargs):
    widget=Gtk.Label(label=text,xalign=0,**kwargs)
    for name in css.split(): widget.add_css_class(name)
    return widget


def icon(app,size=48):
    image=Gtk.Image(pixel_size=size)
    if app.icon.startswith('/'):
        try: image.set_from_paintable(Gdk.Texture.new_from_filename(app.icon))
        except GLib.Error: image.set_from_icon_name(available_icon(app.id.removesuffix('.desktop')))
    else: image.set_from_icon_name(available_icon(app.icon))
    return image


def available_icon(*names):
    theme=Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    return next((name for name in (*names,'application-x-executable-symbolic','image-missing')
                 if theme.has_icon(name)), 'image-missing')


def options(argv):
    parser=argparse.ArgumentParser(description='Программы RunneOS')
    parser.add_argument('--page',choices=('catalog','installed','updates'),default=None)
    parser.add_argument('--smoke-test',action='store_true')
    parser.add_argument('--smoke-timeout',type=float,default=30)
    parser.add_argument('--status',action='store_true',help='Print the active window state as JSON')
    return parser.parse_args(argv)


class Window(Adw.ApplicationWindow):
    def __init__(self, application, initial_page='catalog'):
        super().__init__(application=application,title='Программы RunneOS',
                         default_width=1120,default_height=790)
        self.set_size_request(760,550)
        self.backend=Backend();self.pool=ThreadPoolExecutor(max_workers=5)
        self.runner=OperationRunner();self.page=initial_page;self.category='';self.source='all'
        self.generation=0;self.limit=36;self.catalog_ready=False;self.load_error=None;self.busy=False;self.operation_dialog=None
        self._search_timeout=0;self._update_result=None
        self.connect('close-request',self.close_requested)

        root=box(False);self.set_content(root)
        sidebar=box(spacing=10);sidebar.set_size_request(210,-1);sidebar.add_css_class('sidebar')
        root.append(sidebar)
        brand=box(False,12);brand.set_margin_top(30);brand.set_margin_bottom(30)
        self.brand_logo=wordmark(158)
        brand_text=box(spacing=8);brand_text.append(self.brand_logo)
        brand_text.append(label('Программы · RunneOS','muted caption'));brand.append(brand_text);sidebar.append(brand)
        self.nav={};self.nav_icons={}
        for key,name,symbol in [('catalog','Каталог','view-grid-symbolic'),
                                ('installed','Установлено','drive-harddisk-symbolic'),
                                ('updates','Обновления','view-refresh-symbolic')]:
            button=Gtk.Button();content=box(False,12)
            chosen=available_icon(symbol,'folder-symbolic');self.nav_icons[key]=chosen
            content.append(Gtk.Image.new_from_icon_name(chosen));content.append(label(name))
            button.set_child(content);button.add_css_class('navigation')
            button.connect('clicked',lambda _,key=key:self.navigate(key));sidebar.append(button);self.nav[key]=button
        spacer=box(vexpand=True);sidebar.append(spacer)
        footer=box(spacing=6);footer.add_css_class('sidebar-footer')
        footer.append(label('Всё для вашей работы','muted caption'))
        footer.append(label('Arch · AUR · Flatpak','muted caption'));sidebar.append(footer)

        main=box(hexpand=True,vexpand=True);root.append(main)
        header=Adw.HeaderBar();header.add_css_class('flat')
        header.set_show_title(True)
        self.search=Gtk.SearchEntry(placeholder_text='Найти приложение',hexpand=True)
        self.search.set_size_request(280,-1);self.search.connect('search-changed',self.search_changed)
        header.set_title_widget(self.search)
        refresh=Gtk.Button(icon_name='view-refresh-symbolic',tooltip_text='Обновить список')
        refresh.connect('clicked',lambda _:self.refresh());header.pack_start(refresh);main.append(header)
        self.overlay=Adw.ToastOverlay();main.append(self.overlay);self.overlay.set_vexpand(True)
        self.scroll=Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,vexpand=True)
        self.overlay.set_child(self.scroll)
        self.content=box(spacing=22,margin_start=28,margin_end=28,margin_top=12,margin_bottom=30)
        self.scroll.set_child(self.content)
        self.status=label('Загружаем приложения…','muted caption',margin_start=28,margin_end=28,margin_bottom=12)
        main.append(self.status)
        self.theme=Theme(self,HERE.joinpath('style.css').read_text(),[self.brand_logo])
        self.navigate(initial_page)
        self.refresh()

    def close_requested(self,_):
        if self.busy:
            self.toast('Дождитесь завершения операции, затем закройте окно.')
            return True
        return False

    def toast(self,text):self.overlay.add_toast(Adw.Toast.new(text))

    def async_call(self,worker,callback):
        future=self.pool.submit(worker)
        def done(future):
            try: result=future.result();GLib.idle_add(callback,result,None)
            except Exception as error:GLib.idle_add(callback,None,str(error))
        future.add_done_callback(done)

    def refresh(self):
        if self.busy:return
        self.status.set_text('Загружаем приложения…')
        self.async_call(self.backend.load,self.loaded)

    def loaded(self,apps,error):
        if error:
            self.load_error=error;self.status.set_text('Не удалось загрузить список');self.toast(error);return
        self.load_error=None
        self.catalog_ready=True
        if self.backend.warnings:self.toast('Часть источников недоступна. '+self.backend.warnings[0])
        self.status.set_text(f'{sum(a.installed for a in apps)} установлено · {len(apps)} в каталоге')
        self.render()

    def navigate(self,page):
        self.page=page;self.generation+=1;self.limit=36;self.category='';self.source='all'
        for key,button in self.nav.items():
            if key==page:button.add_css_class('selected')
            else:button.remove_css_class('selected')
        self.search.set_visible(page!='updates')
        if self._search_timeout:
            GLib.source_remove(self._search_timeout);self._search_timeout=0
        self.render()
        if page=='updates' and self._update_result is None:self.check_updates()

    def search_changed(self,_):
        if self._search_timeout:GLib.source_remove(self._search_timeout)
        self.generation+=1
        self._search_timeout=GLib.timeout_add(250,self.perform_search)

    def perform_search(self):
        self._search_timeout=0;self.limit=36;self.render()
        if self.page=='catalog' and self.source=='aur':
            query=self.search.get_text().strip();generation=self.generation
            self.status.set_text('Ищем в AUR…' if len(query)>1 else 'Введите хотя бы две буквы')
            def ready(apps,error):
                if generation!=self.generation or self.page!='catalog' or self.source!='aur':return
                if error:self.status.set_text('AUR сейчас недоступен');self.toast(error);return
                self.fill_cards(apps);self.status.set_text(f'Найдено: {len(apps)}')
            self.async_call(lambda:self.backend.search_aur(query),ready)
        return False

    def clear_content(self):
        while child:=self.content.get_first_child():self.content.remove(child)

    def render(self):
        self.clear_content()
        if self.page=='updates':self.render_updates();return
        query=self.search.get_text().strip().casefold()
        title='Найдите что-то полезное' if self.page=='catalog' else 'Ваши приложения'
        self.content.append(label(title,'page-title'))
        self.content.append(label('Программы для работы, творчества и отдыха' if self.page=='catalog'
                                  else 'Всё установленное — в одном месте','muted',wrap=True))
        if self.page=='catalog':
            if not query:
                hero=box(spacing=9);hero.add_css_class('hero')
                hero.append(label('Хорошие инструменты.\nБольше возможностей.','hero-title'))
                hero.append(label('Выбирайте приложения — RunneOS позаботится об установке.','muted',wrap=True))
                self.content.append(hero)
            filters=box(False,8);self.content.append(filters)
            dropdown=Gtk.DropDown.new_from_strings([entry[0] for entry in CATEGORIES]);dropdown.set_selected(next(i for i,x in enumerate(CATEGORIES) if x[1]==self.category))
            dropdown.connect('notify::selected',lambda widget,_:self.set_category(CATEGORIES[widget.get_selected()][1]));filters.append(dropdown)
            sources=Gtk.DropDown.new_from_strings(['Все приложения','Arch','Flatpak','Сообщество · AUR'])
            sources.set_selected(['all','arch','flatpak','aur'].index(self.source))
            sources.connect('notify::selected',lambda widget,_:self.set_source(['all','arch','flatpak','aur'][widget.get_selected()]));filters.append(sources)
        self.flow=Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,homogeneous=True,
                              column_spacing=14,row_spacing=14,min_children_per_line=1,max_children_per_line=4)
        self.content.append(self.flow)
        self.more=Gtk.Button(label='Показать ещё');self.more.add_css_class('flat')
        self.more.connect('clicked',lambda _:self.show_more());self.content.append(self.more)
        if not self.catalog_ready:self.empty('Загружаем каталог…','Это займёт несколько секунд.');return
        if self.source=='aur':self.more.set_visible(False);self.empty('Приложения сообщества','Введите название в поиске.');return
        apps=[app for app in self.backend.apps if (self.page!='installed' or app.installed)
              and (self.source=='all' or app.source==self.source)
              and (not self.category or self.category in app.categories)
              and (not query or query in (' '.join((app.name,app.summary,app.id,app.package))).casefold())]
        if not query and self.page=='catalog':
            def rank(app):
                app_id=app.id.removesuffix('.desktop')
                return (0 if app_id in FEATURED else 1,FEATURED.index(app_id) if app_id in FEATURED else 100,
                        0 if app.installed else 1,app.name.casefold())
            apps.sort(key=rank)
        self.fill_cards(apps)

    def empty(self,title,subtitle):
        self.flow.insert(Adw.StatusPage(title=title,description=subtitle,icon_name='system-search-symbolic'),-1)

    def fill_cards(self,apps):
        while child:=self.flow.get_first_child():self.flow.remove(child)
        if not apps:self.empty('Ничего не найдено','Попробуйте другое название или категорию.')
        for app in apps[:self.limit]:
            button=Gtk.Button();button.add_css_class('app-card');button.set_hexpand(True)
            button.set_size_request(230,168);body=box(spacing=14)
            top=box(False,12);top.append(icon(app));titles=box(spacing=5,hexpand=True)
            titles.append(label(app.name,'card-title',ellipsize=Pango.EllipsizeMode.END,
                                width_chars=1,max_width_chars=18))
            titles.append(label(app.source_label,'muted caption'));top.append(titles);body.append(top)
            summary=label(app.summary or 'Откройте карточку, чтобы узнать больше','muted',wrap=True,
                          ellipsize=Pango.EllipsizeMode.END,lines=2);summary.set_max_width_chars(25);body.append(summary)
            bottom=box(False,8);bottom.append(label('Установлено' if app.installed else 'Подробнее',('installed' if app.installed else 'accent')+' caption'))
            body.append(bottom);button.set_child(body)
            button.connect('clicked',lambda _,app=app:self.details(app));self.flow.insert(button,-1)
        self.more.set_visible(len(apps)>self.limit)

    def set_category(self,category):self.category=category;self.limit=36;self.render()
    def set_source(self,source):self.source=source;self.generation+=1;self.perform_search()
    def show_more(self):self.limit+=36;self.render()

    def launch(self,app,new_window=False):
        if app.source=='flatpak':args=['runneos-launch','--flatpak',app.package,'--scope',app.scope]
        elif app.desktop.startswith('/'):args=['runneos-launch','--desktop-file',app.desktop]
        elif app.desktop:args=['runneos-launch','--desktop',app.desktop if app.desktop.endswith('.desktop') else app.desktop+'.desktop']
        else:self.toast('Нет desktop-действия для запуска');return
        if new_window:args.append('--new-window')
        def work():
            import subprocess
            result=subprocess.run(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
            if result.returncode:raise RuntimeError('Не удалось открыть приложение или его окно')
            return True
        self.async_call(work,lambda result,error:self.toast(error) if error else None)

    def details(self,app):
        dialog=Adw.Window(title=app.name,transient_for=self,modal=True,default_width=750,default_height=660)
        outer=box();dialog.set_content(outer);header=Adw.HeaderBar();outer.append(header)
        scroller=Gtk.ScrolledWindow(vexpand=True,hscrollbar_policy=Gtk.PolicyType.NEVER);outer.append(scroller)
        content=box(spacing=22,margin_start=32,margin_end=32,margin_top=16,margin_bottom=32);scroller.set_child(content)
        intro=box(False,20);intro.append(icon(app,72));info=box(spacing=7,hexpand=True)
        info.append(label(app.name,'page-title',wrap=True));info.append(label(app.summary,'muted',wrap=True));intro.append(info);content.append(intro)
        actions=box(False,12);content.append(actions)
        primary=Gtk.Button(label='Открыть' if app.installed and app.source!='npm' else ('Установлено' if app.installed else 'Установить'))
        primary.add_css_class('suggested-action');primary.add_css_class('pill');actions.append(primary)
        primary.set_sensitive(not self.busy and (not app.installed or bool(app.desktop) or app.source=='flatpak'))
        primary.connect('clicked',lambda _:self.launch(app) if app.installed else self.install(app,dialog))
        if app.installed and app.source not in ('flatpak','npm') and app.desktop:
            identity=app.desktop if app.desktop.endswith('.desktop') else app.desktop+'.desktop'
            info=GioUnix.DesktopAppInfo.new_from_filename(identity) if identity.startswith('/') else GioUnix.DesktopAppInfo.new(identity)
            if info and 'new-window' in info.list_actions():
                fresh=Gtk.Button(label='Новое окно');fresh.add_css_class('pill')
                fresh.connect('clicked',lambda _:self.launch(app,True));actions.append(fresh)
        if app.installed and app.source not in ('local','npm'):
            remove=Gtk.Button(label='Удалить');remove.add_css_class('pill');remove.set_sensitive(not self.busy and app.package not in PROTECTED)
            remove.connect('clicked',lambda _:self.confirm_remove(app,dialog));actions.append(remove)
        if app.source in ('local','npm'):
            content.append(label('Установлено отдельно. Обновления и удаление выполняются штатным установщиком этой программы.','muted',wrap=True))
        facts=Adw.PreferencesGroup()
        for title,value in [('Источник',app.source_label),('Версия',app.version or 'Актуальная версия'),
                            ('Размещение','Для этого пользователя' if app.scope=='user' else 'На этом компьютере')]:
            facts.add(Adw.ActionRow(title=title,subtitle=value))
        content.append(facts)
        if app.description:content.append(label(app.description,wrap=True,selectable=True))
        if app.source=='aur':
            content.append(label('Пакет поддерживается сообществом Arch. Перед установкой можно посмотреть рецепт сборки.','muted',wrap=True))
            link=Gtk.LinkButton(uri='https://aur.archlinux.org/packages/'+app.package,label='Страница пакета и рецепт установки');content.append(link)
        if app.homepage.startswith('https://'):content.append(Gtk.LinkButton(uri=app.homepage,label='Сайт приложения'))
        if app.screenshots:
            screenshot=Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN,can_shrink=True)
            screenshot.set_size_request(-1,250);content.append(screenshot)
            def ready(path,error):
                if path:
                    try:screenshot.set_paintable(Gdk.Texture.new_from_filename(path))
                    except GLib.Error:screenshot.set_visible(False)
                else:screenshot.set_visible(False)
            self.async_call(lambda:cached_image(app.screenshots[0]),ready)
        dialog.present()

    def install(self,app,dialog):
        if app.source=='aur':
            prompt=Adw.MessageDialog.new(dialog,'Установить '+app.name+'?',
                'Эта программа устанавливается из сообщества Arch. Рецепт сборки доступен на странице пакета.')
            prompt.add_response('cancel','Отмена');prompt.add_response('install','Установить')
            prompt.set_response_appearance('install',Adw.ResponseAppearance.SUGGESTED)
            prompt.connect('response',lambda _,response:self.start_app_operation('install',app,dialog) if response=='install' else None)
            prompt.present()
        else:self.start_app_operation('install',app,dialog)

    def confirm_remove(self,app,dialog):
        prompt=Adw.MessageDialog.new(dialog,'Удалить '+app.name+'?',
            'Программа будет удалена. Ваши документы, настройки и личные данные сохранятся.')
        prompt.add_response('cancel','Отмена');prompt.add_response('remove','Удалить')
        prompt.set_response_appearance('remove',Adw.ResponseAppearance.DESTRUCTIVE)
        prompt.connect('response',lambda _,response:self.start_app_operation('remove',app,dialog) if response=='remove' else None)
        prompt.present()

    def start_app_operation(self,action,app,dialog):
        try:steps=commands(action,app)
        except ValueError as error:self.toast(str(error));return
        dialog.close();self.start_operation(steps,('Устанавливаем ' if action=='install' else 'Удаляем ')+app.name)

    def start_operation(self,steps,title):
        if self.busy:self.toast('Другая операция ещё выполняется');return
        self.busy=True;self.status.set_text(title)
        dialog=Adw.Window(title=title,transient_for=self,modal=True,default_width=670,default_height=400)
        self.operation_dialog=dialog
        dialog.connect('close-request',lambda _:self.busy)
        content=box(spacing=20,margin_start=26,margin_end=26,margin_top=26,margin_bottom=26);dialog.set_content(content)
        heading=label(title,'section-title',wrap=True);content.append(heading)
        self.progress=Gtk.ProgressBar();content.append(self.progress)
        subtitle=label('Можно оставить окно открытым. Мы сообщим, когда всё будет готово.','muted',wrap=True);content.append(subtitle)
        log=Gtk.TextView(editable=False,cursor_visible=False,monospace=True,wrap_mode=Gtk.WrapMode.WORD_CHAR)
        expander=Gtk.Expander(label='Подробнее');scroll=Gtk.ScrolledWindow(min_content_height=170);scroll.set_child(log);expander.set_child(scroll);content.append(expander)
        self.log_buffer=log.get_buffer();self.log_lines=[]
        close=Gtk.Button(label='Готово');close.add_css_class('suggested-action');close.set_sensitive(False)
        close.connect('clicked',lambda _:dialog.close());content.append(close)
        timer=GLib.timeout_add(100,lambda:self.pulse())
        def output(text):GLib.idle_add(self.append_log,text)
        def finished(code,error):
            self.busy=False;GLib.source_remove(timer)
            success=code==0 and not error
            self.progress.set_fraction(1 if success else 0)
            heading.set_text('Готово' if success else 'Не удалось завершить операцию')
            subtitle.set_text('Список приложений обновлён.' if success else 'Изменения могли выполниться частично. Подробности — в журнале ниже.')
            if error:self.append_log(error+'\n')
            if not success:expander.set_expanded(True)
            close.set_sensitive(True);self.toast('Операция завершена' if success else 'Операция остановлена')
            self._update_result=None;self.refresh()
            if self.page=='updates':self.check_updates()
        self.async_call(lambda:self.runner.run(steps,output),finished);dialog.present()

    def pulse(self):
        if self.busy:self.progress.pulse();return True
        return False

    def append_log(self,text):
        self.log_buffer.insert(self.log_buffer.get_end_iter(),text)
        if self.log_buffer.get_char_count()>150000:
            self.log_buffer.delete(self.log_buffer.get_start_iter(),self.log_buffer.get_iter_at_offset(50000))

    def check_updates(self):
        if self.busy:return
        self._update_result=None;self.status.set_text('Проверяем обновления…');self.render_updates_if_active()
        self.async_call(self.backend.updates,self.updates_ready)

    def updates_ready(self,result,error):
        self._update_result=result if result is not None else ([],[error])
        self.status.set_text('Проверка обновлений завершена' if not self._update_result[1] else 'Некоторые источники недоступны')
        self.render_updates_if_active()

    def render_updates_if_active(self):
        if self.page=='updates':self.render()

    def render_updates(self):
        self.content.append(label('Обновления','page-title'))
        self.content.append(label('Свежие версии ваших приложений и системы','muted',wrap=True))
        panel=box(spacing=16);panel.add_css_class('hero')
        panel.append(label('Сначала — снимок системы','section-title'))
        panel.append(label('Перед обновлением сохраним состояние корня /. Отдельные /home и /boot в этот снимок не входят. Ваши приложения обновятся последовательно.','muted',wrap=True))
        actions=box(False,10);panel.append(actions)
        try:
            operation=read_operation();journal_error=False
        except (OSError,ValueError):operation=None;journal_error=True
        blocked=journal_error or bool(operation and operation.get('status') in ('running','interrupted'))
        if blocked:
            self.content.append(label('Есть незавершённая операция или недоступен её журнал. Новое обновление заблокировано; проверьте runneos-update --status.','warning',wrap=True))
        elif operation:
            names={'succeeded':'завершена','failed':'остановлена с ошибкой','stopped_after_step':'остановлена после текущего шага'}
            self.content.append(label('Последняя операция: '+names.get(operation.get('status'),'требует проверки')+
                                      f' · шагов выполнено {operation.get("completedSteps",0)}/{operation.get("totalSteps",0)}','muted',wrap=True))
        all_button=Gtk.Button(label='Обновить всё');all_button.add_css_class('suggested-action');all_button.add_css_class('pill')
        all_button.set_sensitive(not blocked and not self.busy and self._update_result is not None and bool(self._update_result[0]))
        all_button.connect('clicked',lambda _:self.confirm_update());actions.append(all_button)
        check=Gtk.Button(label='Проверить');check.add_css_class('pill');check.set_sensitive(not self.busy)
        check.connect('clicked',lambda _:self.check_updates());actions.append(check);self.content.append(panel)
        if self._update_result is None:
            self.content.append(Adw.StatusPage(title='Ищем новые версии',description='Проверяем Arch, AUR и Flatpak',icon_name='software-update-available-symbolic'));return
        updates,errors=self._update_result
        if errors:
            self.content.append(label('Не все источники удалось проверить','section-title'))
            for error in errors:self.content.append(label(error,'muted',wrap=True))
        if not updates and not errors:
            self.content.append(Adw.StatusPage(title='Всё актуально',description='Доступных обновлений Arch, AUR и Flatpak нет.',icon_name='emblem-ok-symbolic'))
        group=Adw.PreferencesGroup(title=f'Доступно обновлений: {len(updates)}')
        for item in updates:group.add(Adw.ActionRow(title=item['name'],subtitle=item['source']+' · '+item['version']))
        if updates:self.content.append(group)
        self.content.append(label('Локально установленные программы обновляются через свои установщики.','muted caption',wrap=True))

    def confirm_update(self):
        prompt=Adw.MessageDialog.new(self,'Обновить приложения и систему?',
            'Создадим снимок корня, затем обновим Arch, AUR и Flatpak. Дождитесь завершения операции.')
        prompt.add_response('cancel','Отмена');prompt.add_response('update','Обновить всё')
        prompt.set_response_appearance('update',Adw.ResponseAppearance.SUGGESTED)
        prompt.connect('response',lambda _,response:self.start_operation(update_commands(),'Обновляем RunneOS') if response=='update' else None)
        prompt.present()


class Application(Adw.Application):
    def __init__(self,smoke=False):
        flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        if smoke:flags|=Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id='org.runneos.Software',flags=flags)
        self.page='catalog';self.smoke=smoke;self.window=None;self.smoke_result=1

    def do_command_line(self,command_line):
        args=options(command_line.get_arguments()[1:])
        if self.smoke and args.smoke_timeout<=0:
            print(json.dumps({'ok':False,'error':'Smoke test timed out'}),flush=True)
            return 1
        if args.page:self.page=args.page
        if not args.status:
            if self.window and args.page:self.window.navigate(args.page)
            self.activate()
        elif self.window:
            command_line.print_literal(json.dumps(self.window_state(),ensure_ascii=False)+'\n')
        else:
            command_line.printerr_literal('Центр приложений не запущен\n')
            return 1
        if self.smoke:
            deadline=time.monotonic()+max(0,args.smoke_timeout)
            GLib.timeout_add(100,lambda:self.smoke_check(deadline))
        return 0

    def window_state(self):
        window=self.window
        positions=[]
        if window.page!='updates' and hasattr(window,'flow'):
            child=window.flow.get_first_child()
            while child:
                measured,rect=child.compute_bounds(window.flow)
                if measured and child.get_width()>0:positions.append((round(rect.get_x()),round(rect.get_y())))
                child=child.get_next_sibling()
        columns=len({x for x,y in positions if y==min((p[1] for p in positions),default=0)})
        return {'page':window.page,'catalog_ready':window.catalog_ready,
                'catalog':len(window.backend.apps),'installed':sum(a.installed for a in window.backend.apps),
                'search_visible':window.search.get_mapped(),'columns':columns,
                'width':window.get_width(),'icons':window.nav_icons,'warnings':window.backend.warnings}

    def smoke_check(self,deadline):
        if self.window.load_error or time.monotonic()>=deadline:
            print(json.dumps({'ok':False,'error':self.window.load_error or 'Smoke test timed out'}),flush=True)
            self.quit();return False
        if not self.window.catalog_ready or not self.window.get_mapped():return True
        state=self.window_state()
        if state['columns']==0:return True
        expected_columns=2 if state['width']>=900 else 1
        if not state['catalog'] or not state['installed'] or not state['search_visible'] or state['columns']<expected_columns:
            print(json.dumps(dict(state,ok=False,error='Catalog layout check failed')),flush=True)
            self.quit();return False
        # Inspect both rendered grids on consecutive frames, not just Python objects.
        if self.window.page=='catalog':
            self.window.navigate('installed');return True
        print(json.dumps(dict(state,ok=True),ensure_ascii=False),flush=True)
        self.smoke_result=0;self.quit();return False

    def do_activate(self):
        if not self.window:self.window=Window(self,self.page)
        self.window.present()


if __name__=='__main__':
    args=options(sys.argv[1:])
    if args.smoke_test and (args.status or args.page not in (None,'catalog')):
        raise SystemExit('--smoke-test checks catalog and installed; do not combine with --page or --status')
    application=Application(args.smoke_test)
    result=application.run(sys.argv)
    raise SystemExit(application.smoke_result if args.smoke_test else result)
