#!/usr/bin/python3
"""Small native entry point to existing RunneOS owners, plus read-only Doctor."""
import json
import subprocess
import threading
import sys
import gi
gi.require_version('Gtk','4.0');gi.require_version('Adw','1')
from gi.repository import Gtk,Adw,GLib,Gio

sys.path.insert(0,'/usr/share/runneos/theme')
from ui_style import Theme,wordmark


class Hub(Adw.Application):
    def __init__(self):super().__init__(application_id='org.runneos.Hub')
    def do_activate(self):
        if self.get_active_window():self.get_active_window().present();return
        win=Adw.ApplicationWindow(application=self,title='RunneOS · Центр управления',default_width=720,default_height=690)
        outer=Gtk.Box(orientation=Gtk.Orientation.VERTICAL);win.set_content(outer);outer.append(Adw.HeaderBar())
        scroll=Gtk.ScrolledWindow(vexpand=True);outer.append(scroll)
        content=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=20)
        for side in ('top','bottom','start','end'):getattr(content,'set_margin_'+side)(28)
        scroll.set_child(content)
        logo=wordmark(190);logo.set_halign(Gtk.Align.START);content.append(logo)
        self.theme=Theme(win,wordmarks=[logo])
        title=Gtk.Label(label='Всё на своих местах.',wrap=True,xalign=0);title.add_css_class('title-1');content.append(title)
        group=Adw.PreferencesGroup(title='Рабочая среда');content.append(group)
        for name,description,args in (
            ('Программы','Каталог, установка и обновления',['runneos-launch','--desktop','org.runneos.Software.desktop']),
            ('AI-аккаунты','Подписки, балансы и подключения',['runneos-launch','--desktop','org.runneos.AI.desktop']),
            ('Оформление','Carbon и Cloud',['runneos-launch','--desktop','org.runneos.Appearance.desktop']),
            ('Настройки среды','Экраны, звук, сеть и устройства — в DMS',['dms','ipc','call','settings','open']),
            ('Пароли','Личный синхронизируемый сейф',['runneos-launch','--desktop','bitwarden.desktop']),
            ('Локальные ключи','Системное хранилище секретов',['runneos-launch','--desktop','org.gnome.seahorse.Application.desktop'])):
            row=Adw.ActionRow(title=name,subtitle=description);button=Gtk.Button(icon_name='go-next-symbolic',valign=Gtk.Align.CENTER)
            def launch(_,args=args):
                def worker():
                    try:
                        result=subprocess.run(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=8)
                        if result.returncode:GLib.idle_add(status.set_text,'Не удалось открыть приложение. Проверьте его установку.')
                    except Exception:GLib.idle_add(status.set_text,'Не удалось открыть приложение.')
                threading.Thread(target=worker,daemon=True).start()
            button.connect('clicked',launch);row.add_suffix(button);row.set_activatable_widget(button);group.add(row)
        status=Gtk.Label(label='Диагностика только читает состояние системы.',xalign=0,wrap=True)
        run=Gtk.Button(label='Проверить систему');run.add_css_class('suggested-action');content.append(run);content.append(status)
        checks=Adw.PreferencesGroup(title='Runne Doctor');content.append(checks)
        agent=Adw.PreferencesGroup(title='Для агента',description='Сводка без секретов: службы, репозиторий, обновления, знания');content.append(agent)
        context_row=Adw.ActionRow(title='Состояние машины',subtitle='Скопировать сводку, чтобы вставить её агенту')
        context_button=Gtk.Button(label='Скопировать',valign=Gtk.Align.CENTER)
        def copy_context(_):
            context_button.set_sensitive(False);status.set_text('Собираем состояние машины…')
            def worker():
                try:
                    result=subprocess.run(['runneos-context'],capture_output=True,text=True,timeout=40)
                    text=result.stdout.strip()
                except Exception:text=''
                def done():
                    context_button.set_sensitive(True)
                    if not text:
                        status.set_text('runneos-context недоступен: обновите пакет runneos-tools.');return
                    win.get_clipboard().set(text)
                    status.set_text('Сводка скопирована в буфер обмена — вставьте её агенту.')
                GLib.idle_add(done)
            threading.Thread(target=worker,daemon=True).start()
        context_button.connect('clicked',copy_context)
        context_row.add_suffix(context_button);context_row.set_activatable_widget(context_button);agent.add(context_row)
        existing=[]
        def clicked(_):
            run.set_sensitive(False);status.set_text('Проверяем службы, конфигурации и устройства…')
            def worker():
                try:
                    result=subprocess.run(['runneos-doctor','--json'],capture_output=True,text=True,timeout=35);data=json.loads(result.stdout)
                except Exception:data={'checks':[],'scope':'Не удалось завершить диагностику'}
                def done():
                    for row in existing:checks.remove(row)
                    existing.clear()
                    for item in data['checks']:
                        row=Adw.ActionRow(title=item['name'],subtitle=item['detail']);row.add_suffix(Gtk.Label(label={'ok':'✓','attention':'!','unknown':'?'}[item['status']]))
                        checks.add(row);existing.append(row)
                    status.set_text(data['scope']);run.set_sensitive(True)
                GLib.idle_add(done)
            threading.Thread(target=worker,daemon=True).start()
        run.connect('clicked',clicked);win.present()


if __name__=='__main__':Hub().run()
