#!/usr/bin/python3
"""RunneOS AI: account connections and honest resource metrics."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import uuid
from datetime import datetime

import gi
gi.require_version('Gtk','4.0');gi.require_version('Adw','1')
from gi.repository import Gtk,Adw,Gio,GLib,Gdk

from accounts import AccountStore,SecretStore,PRODUCTS

sys.path.insert(0,'/usr/share/runneos/theme')
from ui_style import Theme,wordmark


def label(text,css='',**kwargs):
    obj=Gtk.Label(label=text,xalign=0,wrap=True,**kwargs)
    for name in css.split():obj.add_css_class(name)
    return obj


def box(spacing=12):return Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=spacing)


def timestamp(value):
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00')).astimezone().strftime('%d.%m в %H:%M')
    except (ValueError,TypeError):return 'неизвестно'


def compact(value):
    if value>=1_000_000:return f'{value/1_000_000:.2f} млн'
    if value>=1000:return f'{value/1000:.1f} тыс.'
    return str(int(value))


class Window(Adw.ApplicationWindow):
    def __init__(self,app):
        super().__init__(application=app,title='AI · RunneOS',default_width=940,default_height=760)
        self.pool=ThreadPoolExecutor(max_workers=2);self.busy=False;self.pending_refresh=False
        self.store=AccountStore();self.snapshot={}
        outer=box(0);self.set_content(outer)
        header=Adw.HeaderBar();self.brand_logo=wordmark(124);header.set_title_widget(self.brand_logo)
        add=Gtk.Button(label='Добавить аккаунт');add.add_css_class('suggested-action');add.connect('clicked',lambda _:self.connect_account());header.pack_end(add)
        self.refresh_button=Gtk.Button(icon_name='view-refresh-symbolic',tooltip_text='Обновить данные')
        self.refresh_button.connect('clicked',lambda _:self.refresh(True));header.pack_start(self.refresh_button);outer.append(header)
        self.overlay=Adw.ToastOverlay();outer.append(self.overlay);self.overlay.set_vexpand(True)
        scroll=Gtk.ScrolledWindow(vexpand=True,hscrollbar_policy=Gtk.PolicyType.NEVER);self.overlay.set_child(scroll)
        self.content=box(20)
        for side in ('top','bottom','start','end'):getattr(self.content,'set_margin_'+side)(28)
        scroll.set_child(self.content)
        self.status=label('Получаем данные…','dim-label',margin_start=28,margin_bottom=14);outer.append(self.status)
        self.theme=Theme(self,wordmarks=[self.brand_logo])
        # Atomic normalized cache can render immediately while the shared
        # collector refreshes; never show it as a new successful observation.
        try:
            from engine_v2 import state_dir
            cached=json.loads((state_dir()/'accounts-v2.json').read_text())
            allowed={a['id'] for a in self.store.read()['accounts']}|{'codex-cli-active'}
            cached['accounts']=[a for a in cached.get('accounts',[]) if a['id'] in allowed]
            for row in cached['accounts']:
                if row.get('lastSuccess'):row.update(status='stale',message='Сохранённые данные · проверяем источник')
            self.snapshot=cached;self.render()
        except (OSError,ValueError,KeyError):pass
        self.refresh()
        GLib.timeout_add_seconds(60,lambda:(self.refresh(),True)[1])

    def async_call(self,worker,callback):
        future=self.pool.submit(worker)
        def done(future):
            try:value=future.result();GLib.idle_add(callback,value,None)
            except Exception:GLib.idle_add(callback,None,'Не удалось выполнить действие. Проверьте подключение и хранилище ключей.')
        future.add_done_callback(done)

    def toast(self,text):self.overlay.add_toast(Adw.Toast.new(text))

    def refresh(self,force=False):
        if self.busy:
            self.pending_refresh=self.pending_refresh or force
            return
        self.busy=True;self.refresh_button.set_sensitive(False);self.status.set_text('Обновляем данные аккаунтов…')
        def work():
            script=Path(__file__).with_name('engine_v2.py')
            result=subprocess.run([sys.executable,str(script),*(['--force'] if force else [])],capture_output=True,text=True,timeout=120)
            data=json.loads(result.stdout)
            if result.returncode or data.get('error'):raise ValueError('collector failed')
            return data
        def ready(data,error):
            self.busy=False;self.refresh_button.set_sensitive(True)
            if error:self.status.set_text(error);return
            self.snapshot=data;self.render();self.status.set_text('Последняя проверка: '+timestamp(data.get('generatedAt')))
            if self.pending_refresh:self.pending_refresh=False;self.refresh(True)
        self.async_call(work,ready)

    def render(self):
        while child:=self.content.get_first_child():self.content.remove(child)
        self.content.append(label('Ваш AI-ресурс','title-1'))
        self.content.append(label('Подписки, API-балансы и локальная история — разные источники данных.','dim-label'))
        rows=self.snapshot.get('accounts',[])
        if not rows:self.content.append(Adw.StatusPage(title='Подключите свой AI',description='Добавьте нужный продукт. Ключи сохраняются в GNOME Keyring.',icon_name='network-server-symbolic'))
        for row in rows:
            card=box(10);card.add_css_class('account')
            heading=Gtk.Box(spacing=12);title=label(row['name'],'title-3',hexpand=True);heading.append(title)
            if row['id']!='codex-cli-active':
                pin=Gtk.CheckButton(label='В панели');pin.set_active(row.get('pinned',True))
                def pinned(button,row=row):
                    with self.store.edit() as data:
                        for account in data['accounts']:
                            if account['id']==row['id']:account['pinned']=button.get_active()
                    self.refresh(True)
                pin.connect('toggled',pinned);heading.append(pin)
                if PRODUCTS[row['product']]['auth'] in ('key','management'):
                    reconnect=Gtk.Button(label='Заменить ключ');reconnect.connect('clicked',lambda _,row=row:self.replace_key(row));heading.append(reconnect)
                remove=Gtk.Button(icon_name='edit-delete-symbolic',tooltip_text='Удалить подключение')
                remove.connect('clicked',lambda _,row=row:self.confirm_remove(row));heading.append(remove)
            card.append(heading);card.append(label(row['label']+' · '+row['scope'],'dim-label'))
            if row['status']!='live':card.append(label(row.get('message') or 'Данные недоступны','warning'))
            for q in row.get('quotas',[]) or []:
                card.append(label(f'{100-q["usedPercent"]:.0f}% осталось · {q["label"]}','metric'))
                progress=Gtk.ProgressBar();progress.set_fraction(max(0,min(1,1-q['usedPercent']/100)));card.append(progress)
                if q.get('resetsAt'):card.append(label('Сброс: '+timestamp(q['resetsAt']),'dim-label'))
            for b in row.get('balances',[]) or []:card.append(label(f'{b["amount"]:,.2f} {b["currency"]}','metric'))
            for m in row.get('metrics',[]) or []:card.append(label(f'{m["label"]}: {m["value"]:,.2f} {m["unit"]}'))
            if not any(row.get(k) for k in ('quotas','balances','metrics')):card.append(label('Показатели неизвестны','dim-label'))
            if row.get('lastSuccess'):card.append(label('Последний успешный ответ: '+timestamp(row['lastSuccess']),'dim-label'))
            link=Gtk.LinkButton(uri=row['consoleUrl'],label='Открыть кабинет');link.set_halign(Gtk.Align.START);card.append(link)
            self.content.append(card)
        history=self.snapshot.get('history',[])
        if history:
            self.content.append(label('Локальная история · '+str(self.snapshot.get('periodDays',7))+' дней','title-2'))
            self.content.append(label('Только этот компьютер. Источники не суммируются между собой; это не остаток подписки.','dim-label'))
            for row in history:
                self.content.append(label(f'{row["provider"]} · {row["source"]}: {compact(row["tokens"])} токенов'))
            period=Gtk.DropDown.new_from_strings(['7 дней','30 дней','90 дней']);period.set_selected([7,30,90].index(self.snapshot.get('periodDays',7)))
            def change_period(widget,_):
                with self.store.edit() as data:data['periodDays']=[7,30,90][widget.get_selected()]
                self.refresh(True)
            period.connect('notify::selected',change_period);self.content.append(period)
        for error in self.snapshot.get('sourceErrors',[]):self.content.append(label(error['source']+': источник временно недоступен','dim-label'))

    def connect_account(self):
        dialog=Adw.Window(transient_for=self,modal=True,title='Подключить AI-аккаунт',default_width=520,default_height=600)
        outer=box(0);dialog.set_content(outer);outer.append(Adw.HeaderBar())
        content=box(14)
        for side in ('top','bottom','start','end'):getattr(content,'set_margin_'+side)(24)
        scroll=Gtk.ScrolledWindow(vexpand=True,hscrollbar_policy=Gtk.PolicyType.NEVER);scroll.set_child(content);outer.append(scroll)
        content.append(label('Добавить аккаунт','title-1'))
        products=list(PRODUCTS);selector=Gtk.DropDown.new_from_strings([PRODUCTS[p]['name'] for p in products]);content.append(selector)
        description=label('','dim-label');content.append(description)
        name=Gtk.Entry(placeholder_text='Название, например «Личный»');name.set_text('Личный');content.append(name)
        region=Gtk.DropDown.new_from_strings(['International','China']);content.append(region)
        extra=Gtk.Entry();content.append(extra)
        key=Gtk.PasswordEntry(show_peek_icon=True,placeholder_text='API-ключ');content.append(key)
        content.append(label('Ключ сохраняется в системном кошельке. В конфигурации остаётся только ссылка.','dim-label'))
        status=label('');content.append(status)
        save=Gtk.Button(label='Сохранить и проверить');save.add_css_class('suggested-action');content.append(save)
        def changed(*_):
            product=PRODUCTS[products[selector.get_selected()]];auth=product['auth']
            description.set_text(product['scope']);key.set_visible(auth in ('key','management'));key.set_text('')
            extra.set_visible(auth in ('management','profile'));extra.set_placeholder_text('Team ID' if auth=='management' else 'Профиль Alibaba CLI')
            if auth=='cli':status.set_text('Codex подключается автоматически. Для Claude сначала войдите в штатный Claude CLI; здесь используется его доступный источник квот.')
            elif auth=='local':status.set_text('Здесь только локальная история и кабинет. Ввод ключа не нужен.')
            else:status.set_text('')
            save.set_sensitive(products[selector.get_selected()]!='codex-subscription');save.set_label('Добавить ссылку на продукт' if auth=='local' else 'Сохранить и проверить')
        selector.connect('notify::selected',changed);changed()
        def submit(_):
            product_id=products[selector.get_selected()];product=PRODUCTS[product_id]
            account={'id':str(uuid.uuid4()),'product':product_id,'label':name.get_text().strip(),
                     'region':'china' if region.get_selected()==1 else 'international','pinned':True}
            if product['auth']=='management':account['teamId']=extra.get_text().strip()
            if product['auth']=='profile':account['profile']=extra.get_text().strip()
            credential=key.get_text();key.set_text('');save.set_sensitive(False)
            def work():
                AccountStore.validate(account)
                stored=False
                try:
                    if product['auth'] in ('key','management'):
                        SecretStore().put(account['id'],account['label'],credential);stored=True
                    with self.store.edit() as data:data['accounts'].append(account)
                except Exception:
                    if stored:SecretStore().delete(account['id'])
                    raise
                return True
            def done(result,error):
                save.set_sensitive(True)
                if error:status.set_text(error);return
                dialog.close();self.toast('Подключение сохранено. Проверяем доступные метрики.');self.refresh(True)
            self.async_call(work,done)
        save.connect('clicked',submit);dialog.present()

    def replace_key(self,row):
        dialog=Adw.Window(transient_for=self,modal=True,title='Заменить ключ',default_width=460)
        content=box(16)
        for side in ('top','bottom','start','end'):getattr(content,'set_margin_'+side)(24)
        dialog.set_content(content);content.append(label(row['name'],'title-2'))
        entry=Gtk.PasswordEntry(show_peek_icon=True,placeholder_text='Новый ключ');content.append(entry)
        status=label('Старое значение не показывается. Новый ключ сохраняется в GNOME Keyring.','dim-label');content.append(status)
        save=Gtk.Button(label='Сохранить');save.add_css_class('suggested-action');content.append(save)
        def clicked(_):
            value=entry.get_text();entry.set_text('');save.set_sensitive(False)
            def worker():
                SecretStore().put(row['id'],row['label'],value)
                with self.store.edit() as data:data['revision']=str(uuid.uuid4())
                return True
            def done(result,error):
                if error:status.set_text(error);save.set_sensitive(True);return
                dialog.close();self.refresh(True)
            self.async_call(worker,done)
        save.connect('clicked',clicked);dialog.present()

    def confirm_remove(self,row):
        prompt=Adw.MessageDialog.new(self,'Удалить подключение?',row['label']+' · Внешний аккаунт и его данные сохранятся.')
        prompt.add_response('cancel','Отмена');prompt.add_response('remove','Удалить подключение')
        prompt.set_response_appearance('remove',Adw.ResponseAppearance.DESTRUCTIVE)
        def response(_,value):
            if value!='remove':return
            def work():
                if PRODUCTS[row['product']]['auth'] in ('key','management'):SecretStore().delete(row['id'])
                with self.store.edit() as data:data['accounts']=[a for a in data['accounts'] if a['id']!=row['id']]
                return True
            self.async_call(work,lambda result,error:self.toast(error) if error else self.refresh(True))
        prompt.connect('response',response);prompt.present()


class Application(Adw.Application):
    def __init__(self):super().__init__(application_id='org.runneos.AI',flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
    def do_activate(self):
        window=self.get_active_window()
        if window:window.refresh()
        else:window=Window(self)
        window.present()


if __name__=='__main__':
    if '--smoke-test' in sys.argv:
        app=Application();app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
        result={'ok':False}
        def check():
            window=app.get_active_window()
            if not window or window.busy:return True
            if not window.snapshot:return True
            window.connect_account()
            result.update(ok=True,accounts=len(window.snapshot.get('accounts',[])),history=len(window.snapshot.get('history',[])))
            GLib.timeout_add(300,lambda:(app.quit(),False)[1]);return False
        GLib.timeout_add(200,check)
        GLib.timeout_add_seconds(110,lambda:(app.quit(),False)[1])
        app.run([sys.argv[0]])
        print(json.dumps(result));raise SystemExit(0 if result['ok'] else 1)
    raise SystemExit(Application().run(sys.argv))
