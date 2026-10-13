#!/usr/bin/python3
"""Native selector for the two packaged RunneOS themes."""
import subprocess
import threading
import sys
from pathlib import Path
import gi
gi.require_version('Gtk','4.0');gi.require_version('Adw','1')
from gi.repository import Gtk,Adw,GLib

sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.insert(0,'/usr/share/runneos/theme')
from ui_style import Theme,wordmark,current_mode


class Appearance(Adw.Application):
    def __init__(self):super().__init__(application_id='org.runneos.Appearance')
    def do_activate(self):
        if self.get_active_window():self.get_active_window().present();return
        window=Adw.ApplicationWindow(application=self,title='Оформление RunneOS',default_width=560,default_height=390)
        outer=Gtk.Box(orientation=Gtk.Orientation.VERTICAL);window.set_content(outer);outer.append(Adw.HeaderBar())
        content=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=18)
        for side in ('top','bottom','start','end'):getattr(content,'set_margin_'+side)(28)
        outer.append(content)
        logo=wordmark(190);logo.set_halign(Gtk.Align.START);content.append(logo)
        mode_buttons={}
        def selected(mode):
            for identity,button in mode_buttons.items():
                if identity==mode:button.add_css_class('suggested-action')
                else:button.remove_css_class('suggested-action')
        self.theme=Theme(window,wordmarks=[logo],on_change=selected)
        title=Gtk.Label(label='По-своему.',xalign=0);title.add_css_class('title-1');content.append(title)
        content.append(Gtk.Label(label='Общие цвета оболочки, фон и палитра Ghostty.\nПеред изменениями сохраняется резервная копия.',wrap=True,xalign=0))
        status=Gtk.Label(label='',wrap=True,xalign=0);buttons=[]
        for mode,text in [('carbon','Carbon · глубокий графит'),('cloud','Cloud · спокойная светлая')]:
            button=Gtk.Button(label=text);button.add_css_class('pill');buttons.append(button);content.append(button)
            mode_buttons[mode]=button
            def clicked(_,mode=mode):
                for b in buttons:b.set_sensitive(False)
                status.set_text('Применяем оформление…')
                def worker():
                    try:
                        result=subprocess.run(['runneos-theme',mode],capture_output=True,text=True,timeout=25)
                        message='Готово. Новые окна Ghostty используют выбранную палитру.' if result.returncode==0 else 'Не удалось применить тему. Проверьте, запущен ли DMS.'
                    except Exception:message='Не удалось применить тему.'
                    def finish():
                        status.set_text(message)
                        for b in buttons:b.set_sensitive(True)
                    GLib.idle_add(finish)
                threading.Thread(target=worker,daemon=True).start()
            button.connect('clicked',clicked)
        selected(current_mode());content.append(status);window.present()


if __name__=='__main__':Appearance().run()
