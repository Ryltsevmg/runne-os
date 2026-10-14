"""Оффлайн-тесты панели агентов: чистая логика, состояния и сборка окна.

Запуск:
  cd ~/runneos && PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_agents_panel.py' -v

Ядро (runneos_agents) панель не подменяет: тесты подставляют свою двойняшку
модуля через sys.modules. Проверки с GTK выполняются только при наличии
дисплея; без дисплея честно пропускаются, а чистая логика проверяется всегда.
"""
import configparser
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import types
import json
import unittest
from unittest import mock

import gi
gi.require_version('Gtk','4.0');gi.require_version('Adw','1')
from gi.repository import Adw,Gio,Gtk

ROOT=Path(__file__).parents[1]
PANEL=ROOT/'packages/runneos-agents/files/runneos-agents-panel'
THEME=ROOT/'packages/runneos-theme/files/ui_style.py'
DESKTOP=ROOT/'packages/runneos-agents/files/org.runneos.Agents.desktop'
SOURCE=PANEL.read_text()
EVENT_BATCH=200
CORE_PREFIX='runneos_agents'


def shared_theme():
    """Один модуль темы на процесс — панель не должна импортировать тему дважды.

    Панель делает `from ui_style import Theme,wordmark`. Класс внутри wordmark()
    несёт глобальное для процесса имя GType (`RunneWordmarkPicture`), поэтому
    вторая регистрация того же имени падает с RuntimeError. Если отдать панели
    новую копию модуля (например, установленную в /usr/share/runneos/theme),
    исход прогона начинает зависеть от порядка тестовых модулей: test_ui_style,
    загруженный первым, регистрирует GType, и каждый тест сборки окна падает.
    Кладём в sys.modules единственный модуль темы из исходников пакета — тема
    при этом настоящая, а порядок модулей перестаёт что-либо решать.
    """
    module=sys.modules.get('ui_style')
    if module is None:
        spec=importlib.util.spec_from_file_location('ui_style',THEME)
        module=importlib.util.module_from_spec(spec)
        sys.modules['ui_style']=module
        spec.loader.exec_module(module)
    return module


SHARED_THEME=shared_theme()


def load_panel(name,modules=None,drop_core=False):
    """Загружает панель как модуль: она же исполняемый скрипт с main().

    Состояние импорта ядра сохраняется и восстанавливается целиком: одного
    sys.modules['runneos_agents']=None мало, потому что уже импортированные
    чужим тестом подмодули (`runneos_agents.store`) отдаются из кеша, и панель
    получила бы настоящее ядро вместо его отсутствия.
    """
    saved={key:value for key,value in sys.modules.items()
           if key==CORE_PREFIX or key.startswith(CORE_PREFIX+'.')}
    try:
        for key in saved:sys.modules.pop(key,None)
        sys.modules[CORE_PREFIX]=None if drop_core else dict(modules or {})[CORE_PREFIX]
        loader=importlib.machinery.SourceFileLoader(name,str(PANEL))
        spec=importlib.util.spec_from_loader(name,loader)
        module=importlib.util.module_from_spec(spec)
        sys.modules[name]=module
        loader.exec_module(module)
    finally:
        for key in [key for key in sys.modules if key==CORE_PREFIX or key.startswith(CORE_PREFIX+'.')]:
            sys.modules.pop(key,None)
        sys.modules.update(saved)
    return module


def panel_boots_headless_without_pythonpath() -> tuple[bool, str]:
    """Обычный запуск (без PYTHONPATH): панель обязана сама найти ядро.

    Регрессия 2026-10-14: у установленного /usr/bin/runneos-agents-panel не было
    поиска ядра по кандидатам, и живой сеанс показывал «Ядро не найдено».
    """
    probe = (
        "import importlib.machinery, importlib.util, sys\n"
        f"loader = importlib.machinery.SourceFileLoader('panel_probe', r'{PANEL}')\n"
        "spec = importlib.util.spec_from_loader('panel_probe', loader)\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "print(module.CORE_ERROR)\n"
    )
    import subprocess as sp

    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    done = sp.run([sys.executable, "-c", probe], capture_output=True, text=True, env=environment, timeout=120)
    output = done.stdout.strip().strip("'")
    return done.returncode == 0 and output == "", f"rc={done.returncode}: {done.stdout[-200:]} {done.stderr[-300:]}"


def display_available():
    if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):return False
    try:
        return bool(Gtk.init_check())
    except Exception:
        return False


HAVE_DISPLAY=display_available()

_PANEL_NO_CORE=None


def panel_without_core():
    """Панель, загруженная при отсутствующем ядре (лениво, чтобы не зависеть от порядка тестов)."""
    global _PANEL_NO_CORE
    if _PANEL_NO_CORE is None:_PANEL_NO_CORE=load_panel('agents_panel_without_core',drop_core=True)
    return _PANEL_NO_CORE


# --- Двойняшки ядра: формы данных ровно как в INTERFACE.md -------------------

class AgentDouble:
    def __init__(self,identifier,name,role='',provider='codex',state='idle'):
        self.id=identifier;self.name=name;self.role=role;self.provider=provider
        self.workspace=None;self.created_at='2026-10-12T07:00:00Z';self.state=state;self.instructions=''


class SessionDouble:
    def __init__(self,identifier,agent_id,title='',state='open'):
        self.id=identifier;self.agent_id=agent_id;self.title=title
        self.provider_session_id=None;self.created_at='2026-10-12T07:00:00Z'
        self.updated_at='2026-10-12T07:05:00Z';self.state=state


class MessageDouble:
    def __init__(self,identifier,session_id,role,text):
        self.id=identifier;self.session_id=session_id;self.role=role
        self.text=text;self.created_at='2026-10-12T07:04:00Z';self.event_id=None


class EventDouble:
    def __init__(self,identifier,session_id,kind,payload,seq):
        self.id=identifier;self.session_id=session_id;self.kind=kind
        self.payload=payload;self.created_at='2026-10-12T07:04:00Z';self.seq=seq


class TurnResultDouble:
    def __init__(self,ok,session_id,text='',error=None,usage=None):
        self.ok=ok;self.session_id=session_id;self.provider_session_id='thread-1'
        self.text=text;self.error=error;self.usage=usage;self.exit_code=0


class AvailabilityDouble:
    def __init__(self,provider,available,reason='',version=None):
        self.provider=provider;self.available=available;self.reason=reason
        self.version=version;self.binary=None


class FakeDatabase:
    """Общее состояние всех соединений StoreDouble — как одна БД ядра."""

    def __init__(self,agents=(),sessions=(),messages=(),events=(),providers=(),authorizations=None):
        self.agents=list(agents);self.sessions=list(sessions);self.messages=list(messages)
        self.events=list(events);self.providers=list(providers)
        self.authorizations=authorizations if authorizations is not None else {}
        self.ask_calls=[];self.event_reads=[];self.closed=0
        self.ask_started=threading.Event();self.release=threading.Event()
        self.reply='PONG';self.ok=True;self.error=None;self.raise_error=None

    def append_event(self,session_id,kind,payload):
        seq=max([event.seq for event in self.events if event.session_id==session_id],default=0)+1
        event=EventDouble(len(self.events)+1,session_id,kind,payload,seq)
        self.events.append(event)
        return event

    def append_message(self,session_id,role,text):
        message=MessageDouble(len(self.messages)+1,session_id,role,text)
        self.messages.append(message)
        return message


class StoreDouble:
    def __init__(self,db,path=None):
        self.db=db;self.path=path

    def list_agents(self):return list(self.db.agents)

    def get_agent(self,agent_id):return next((a for a in self.db.agents if a.id==agent_id),None)

    def list_sessions(self,agent_id=None,limit=50):
        return [s for s in self.db.sessions if agent_id is None or s.agent_id==agent_id][-limit:]

    def get_session(self,session_id):return next((s for s in self.db.sessions if s.id==session_id),None)

    def latest_open_session(self,agent_id):
        found=[s for s in self.db.sessions if s.agent_id==agent_id and s.state=='open']
        return found[-1] if found else None

    def create_session(self,agent_id,title=''):
        session=SessionDouble(f'session-{len(self.db.sessions)+1}',agent_id,title)
        self.db.sessions.append(session)
        return session

    def add_message(self,session_id,role,text,event_id=None):
        return self.db.append_message(session_id,role,text)

    def list_messages(self,session_id,limit=200):
        return [m for m in self.db.messages if m.session_id==session_id][-limit:]

    def append_event(self,session_id,kind,payload):return self.db.append_event(session_id,kind,payload)

    def list_events(self,session_id,limit=500):
        self.db.event_reads.append(limit)
        return [e for e in self.db.events if e.session_id==session_id][-limit:]

    def close(self):self.db.closed+=1


class OrchestratorDouble:
    def __init__(self,store):
        self.store=store;self.db=store.db

    def providers(self):return list(self.db.providers)

    def authorization(self,provider):
        item=self.db.authorizations.get(provider)
        if item is None:
            return types.SimpleNamespace(provider=provider,authorized=None,detail='нет данных двойника',how='')
        return types.SimpleNamespace(provider=provider,authorized=item,detail='состояние двойника',how=f'{provider} login')

    def ask(self,agent_id,prompt,*,session_id=None,model=None,timeout=None,resume=True):
        db=self.db
        db.ask_calls.append((agent_id,prompt,session_id))
        db.append_message(session_id,'user',prompt)
        db.append_event(session_id,'turn.started',{'kind':'turn.started'})
        db.ask_started.set()
        db.release.wait(timeout=10)
        if db.raise_error is not None:raise db.raise_error
        db.append_event(session_id,'agent_message',{'kind':'agent_message','text':db.reply})
        db.append_message(session_id,'agent',db.reply)
        db.append_event(session_id,'turn.completed',{'kind':'turn.completed'})
        return TurnResultDouble(db.ok,session_id,db.reply,db.error,{'input_tokens':10,'output_tokens':2})


def fake_core(db):
    module=types.ModuleType('runneos_agents')
    module.Store=lambda path=None:StoreDouble(db,path)
    module.Orchestrator=lambda store:OrchestratorDouble(store)
    return {'runneos_agents':module}


class Dispatcher:
    """Замена GLib.idle_add в тесте: очередь, которую главный поток выполняет сам."""

    def __init__(self):
        self.queue=[];self.lock=threading.Lock()

    def __call__(self,callback,*args):
        with self.lock:self.queue.append((callback,args))
        return 0

    def flush(self):
        while True:
            with self.lock:
                if not self.queue:return 0
                callback,args=self.queue.pop(0)
            callback(*args)


def collect_texts(widget,found=None):
    found=[] if found is None else found
    if isinstance(widget,Gtk.Label):found.append(widget.get_text())
    child=widget.get_first_child()
    while child is not None:
        collect_texts(child,found)
        child=child.get_next_sibling()
    return found


_APPLICATION=None


def test_application():
    """Одно приложение на процесс: окна создаются без запуска главного цикла."""
    global _APPLICATION
    if _APPLICATION is None:
        _APPLICATION=Adw.Application(application_id='org.runneos.Agents.Tests',
                                     flags=Gio.ApplicationFlags.NON_UNIQUE)
        _APPLICATION.register(None)
    return _APPLICATION


def build_panel(db=None,drop_core=False,name='agents_panel_window'):
    """Строит панель во временном корне: настоящий каталог пользователя тесты не трогают."""
    root=Path(tempfile.mkdtemp(prefix='runneos-agents-panel-root-'))
    module=load_panel(name,None,drop_core=True) if drop_core else load_panel(name,fake_core(db))
    dispatcher=Dispatcher()
    window=module.AgentsPanel(test_application(),root=root,dispatch=dispatcher)
    window.test_root=root
    window.dispatcher=dispatcher
    if window.providers_thread is not None:window.providers_thread.join(5)
    dispatcher.flush()
    return module,window


# --- Чистая логика: без дисплея ---------------------------------------------

class PanelImportTests(unittest.TestCase):
    def test_import_core_reports_missing_module_instead_of_raising(self):
        module=panel_without_core()
        self.assertIsNone(module.STORE_CLASS)
        self.assertIsNone(module.ORCHESTRATOR_CLASS)
        self.assertTrue(module.CORE_ERROR,'причина отсутствия ядра должна быть названа')

    def test_import_core_accepts_importable_module(self):
        db=FakeDatabase()
        module=load_panel('agents_panel_with_injected_core',fake_core(db))
        self.assertIsNotNone(module.STORE_CLASS)
        self.assertIsNotNone(module.ORCHESTRATOR_CLASS)
        self.assertEqual(module.CORE_ERROR,'')

    def test_panel_reuses_the_single_theme_module(self):
        """Тема не импортируется второй раз: иначе GType RunneWordmarkPicture
        регистрируется повторно и падает — прогон зависел бы от порядка модулей."""
        module=load_panel('agents_panel_theme_probe',fake_core(FakeDatabase()))
        self.assertIs(module.Theme,SHARED_THEME.Theme)
        self.assertIs(module.wordmark,SHARED_THEME.wordmark)
        self.assertIs(sys.modules.get('ui_style'),SHARED_THEME)


class DmsWidgetFilesTests(unittest.TestCase):
    """Виджет DMS: манифест и QML должны совпадать по идентификатору и лежать рядом.

    DMS читает плагины из ~/.config/DankMaterialShell/plugins/<id>/, поэтому пакет кладёт
    источник в /usr/share/dms-plugins/RunneosAgents/ — каталог копируется целиком.
    """

    @classmethod
    def setUpClass(cls):
        candidates = (
            ROOT / "packages/runneos-agents/files/dms-plugin",
            Path("/usr/share/dms-plugins/RunneosAgents"),
        )
        cls.plugin_dir = next(path for path in candidates if path.is_dir())

    def test_plugin_json_matches_widget_plugin_id(self):
        manifest = json.loads((self.plugin_dir / "plugin.json").read_text(encoding="utf-8"))
        qml = (self.plugin_dir / "RunneAgentsWidget.qml").read_text(encoding="utf-8")
        self.assertEqual(manifest["id"], "runneosAgents")
        self.assertIn(f'pluginId: "{manifest["id"]}"', qml)
        self.assertEqual(manifest["component"], "./RunneAgentsWidget.qml")
        self.assertTrue((self.plugin_dir / "RunneAgentsWidget.qml").is_file())
        self.assertEqual(manifest["type"], "widget")
        self.assertEqual(manifest["capabilities"], ["dankbar-widget"])

    def test_widget_reads_state_through_cli_only(self):
        qml = (self.plugin_dir / "RunneAgentsWidget.qml").read_text(encoding="utf-8")
        self.assertIn("runneos-agents", qml)
        for forbidden in ("sqlite3", "agents.db", "curl ", "http://", "https://"):
            self.assertNotIn(forbidden, qml, f"виджет не должен использовать {forbidden}")

    def test_pkgbuild_installs_widget_where_dms_reads_it(self):
        pkgbuild = (ROOT / "packages/runneos-agents/PKGBUILD").read_text(encoding="utf-8")
        self.assertIn("/usr/share/dms-plugins/RunneosAgents", pkgbuild)
        self.assertIn("RunneAgentsWidget.qml", pkgbuild)
        self.assertIn("plugin.json", pkgbuild)


class PanelLogicTests(unittest.TestCase):
    def setUp(self):self.panel=panel_without_core()

    def test_missing_core_is_reported_not_hidden(self):
        self.assertIsNone(self.panel.STORE_CLASS)
        self.assertIsNone(self.panel.ORCHESTRATOR_CLASS)
        self.assertTrue(self.panel.CORE_ERROR)
        title,description,icon=self.panel.placeholders('no_core')
        self.assertIn('не найдено',title)
        self.assertIn('runneos-agents init',description)
        self.assertTrue(icon)

    def test_provider_badge_is_exact_and_keeps_reason_for_tooltip(self):
        agent=AgentDouble('tester','Тестер',provider='codex')
        missing=[AvailabilityDouble('codex',False,reason='codex CLI не найден')]
        self.assertEqual(self.panel.provider_badge(agent,missing),'нет провайдера')
        self.assertIn('codex CLI не найден',self.panel.provider_problem(agent,missing))
        unknown=[AvailabilityDouble('opencode',True)]
        self.assertEqual(self.panel.provider_badge(agent,unknown),'нет провайдера')
        self.assertIn('не зарегистрирован',self.panel.provider_problem(agent,unknown))
        ready=[AvailabilityDouble('codex',True,version='0.9.0')]
        self.assertIsNone(self.panel.provider_badge(agent,ready))
        self.assertIsNone(self.panel.provider_problem(agent,ready))

    def test_agent_subtitle_shows_role_and_available_provider_version(self):
        agent=AgentDouble('tester','Тестер','Отвечает ровно PONG',provider='codex',state='running')
        subtitle=self.panel.agent_subtitle(agent,[AvailabilityDouble('codex',True,version='0.9.0')])
        self.assertIn('Отвечает ровно PONG',subtitle)
        self.assertIn('провайдер: codex 0.9.0',subtitle)
        self.assertNotIn('работает',subtitle,'состояние показывается отдельной меткой, а не в подписи')
        self.assertIn('tester',self.panel.agent_title(agent))
        unavailable=self.panel.agent_subtitle(agent,[AvailabilityDouble('codex',False,reason='нет CLI')])
        self.assertIn('провайдер: codex',unavailable)
        self.assertNotIn('нет CLI',unavailable,'причина — только в подсказке')

    def test_unknown_states_and_roles_are_named_honestly(self):
        self.assertEqual(self.panel.state_label('idle'),'простаивает')
        self.assertEqual(self.panel.state_label('unavailable'),'нет провайдера')
        self.assertEqual(self.panel.state_label('взрыв'),'состояние неизвестно')
        self.assertEqual(self.panel.session_label('open'),'открыта')
        self.assertEqual(self.panel.role_label('agent'),'Агент')
        self.assertEqual(self.panel.role_label('user'),'Вы')
        self.assertEqual(self.panel.role_label(''),'Неизвестный отправитель')

    def test_message_header_marks_agent_role(self):
        message=MessageDouble(1,'s1','agent','PONG')
        self.assertTrue(self.panel.message_header(message).startswith('Агент · '))
        self.assertEqual(self.panel.message_body(message),'PONG')
        empty=MessageDouble(2,'s1','system','')
        self.assertEqual(self.panel.message_body(empty),'Пустое сообщение')

    def test_ask_enabled_requires_core_agent_text_and_no_run(self):
        cases=[({'core_ready':True,'running':False,'agent_id':'tester','prompt':'PONG'},True),
               ({'core_ready':True,'running':True,'agent_id':'tester','prompt':'PONG'},False),
               ({'core_ready':True,'running':False,'agent_id':None,'prompt':'PONG'},False),
               ({'core_ready':True,'running':False,'agent_id':'tester','prompt':'   '},False),
               ({'core_ready':False,'running':False,'agent_id':'tester','prompt':'PONG'},False)]
        for arguments,expected in cases:
            with self.subTest(arguments=arguments):
                self.assertIs(self.panel.ask_enabled(**arguments),expected)

    def test_failure_text_is_honest_and_has_no_traceback(self):
        result=TurnResultDouble(False,'s1',error='codex CLI не найден')
        text=self.panel.turn_failure_text(result)
        self.assertIn('codex CLI не найден',text)
        self.assertNotIn('Traceback',text)
        self.assertNotIn('File "',text)
        raised=self.panel.turn_failure_text(error=RuntimeError('провайдер не ответил'))
        self.assertIn('RuntimeError: провайдер не ответил',raised)
        self.assertNotIn('Traceback',raised)
        self.assertIn('причина не указана',self.panel.turn_failure_text(TurnResultDouble(False,'s1')))

    def test_usage_text_from_contract_fields(self):
        self.assertEqual(self.panel.usage_text({'input_tokens':10,'output_tokens':2}),'токены · вход: 10, выход: 2')
        self.assertEqual(self.panel.usage_text(None),'')
        self.assertEqual(self.panel.usage_text({'input_tokens':'много'}),'')

    def test_new_events_reads_only_fresh_and_in_order(self):
        events=[EventDouble(1,'s1','turn.started',{},3),EventDouble(2,'s1','turn.started',{},1),
                EventDouble(3,'s1','turn.started',{},2)]
        fresh,newest=self.panel.new_events(events,1)
        self.assertEqual([event.seq for event in fresh],[2,3])
        self.assertEqual(newest,3)
        fresh,newest=self.panel.new_events(events,3)
        self.assertEqual(fresh,[])
        self.assertEqual(newest,3)

    def test_event_text_covers_contract_kinds(self):
        text=self.panel.event_text(EventDouble(1,'s1','turn.started',{'kind':'turn.started'},1))
        self.assertEqual(text,'Ход начался')
        text=self.panel.event_text(EventDouble(2,'s1','agent_message',{'kind':'agent_message','text':'PONG'},2))
        self.assertEqual(text,'Агент: PONG')
        text=self.panel.event_text(EventDouble(3,'s1','usage',{'kind':'usage','usage':{'input_tokens':5}},3))
        self.assertIn('вход: 5',text)
        text=self.panel.event_text(EventDouble(4,'s1','error',{'kind':'error','text':'код 1'},4))
        self.assertEqual(text,'Ошибка провайдера: код 1')
        for kind,expected in (('turn.completed','Ход завершён'),('turn.finished','Прогон завершён')):
            with self.subTest(kind=kind):
                self.assertEqual(self.panel.event_text(EventDouble(7,'s1',kind,{'kind':kind},7)),expected)
        gate=self.panel.event_text(EventDouble(8,'s1','policy.decision',{'kind':'policy.decision','allowed':True,'enforce':False,'rule':'default-allow-provider'},8))
        self.assertIn('разрешено',gate)
        self.assertIn('только аудит',gate)
        self.assertIn('default-allow-provider',gate)
        gate_denied=self.panel.event_text(EventDouble(9,'s1','policy.decision',{'kind':'policy.decision','allowed':False,'enforce':True,'rule':'no-codex'},9))
        self.assertIn('запрещено',gate_denied)
        self.assertIn('принуждение',gate_denied)
        self.assertIn('no-codex',gate_denied)
        unknown=self.panel.event_text(EventDouble(5,'s1','неизвестное',{'secret':'ключ'},5))
        self.assertEqual(unknown,'Событие: неизвестное')
        self.assertNotIn('ключ',unknown)
        long_text=self.panel.event_text(EventDouble(6,'s1','stdout',{'kind':'stdout','text':'я'*400},6))
        self.assertLessEqual(len(long_text),200)

    def test_empty_states_offer_a_next_step(self):
        title,description,_=self.panel.placeholders('no_agents')
        self.assertEqual(title,'Нет агентов')
        self.assertIn('runneos-agents agent add',description)
        self.assertIn('Провайдер',description)
        self.assertEqual(self.panel.placeholders('no_sessions')[0],'У агента нет сессий')
        self.assertIn('сессия создастся автоматически',self.panel.placeholders('no_sessions')[1])
        self.assertEqual(self.panel.placeholders('no_messages')[0],'Сообщений пока нет')
        self.assertEqual(self.panel.placeholders('no_agent')[0],'Агент не выбран')

    def test_provider_auth_problem_names_how(self):
        agent=AgentDouble(1,'Тестер',provider='codex')
        self.assertIsNone(self.panel.provider_auth_problem(agent,[]))
        unknown=types.SimpleNamespace(provider='codex',authorized=None,detail='нет данных',how='codex login')
        self.assertIsNone(self.panel.provider_auth_problem(agent,[unknown]),'unknown — не отказ')
        unauthorized=types.SimpleNamespace(provider='codex',authorized=False,detail='Not logged in',how='codex login')
        problem=self.panel.provider_auth_problem(agent,[unauthorized])
        self.assertIn('не авторизован',problem)
        self.assertIn('codex login',problem)
        self.assertIn('Not logged in',problem)

    def test_panel_finds_core_on_plain_launch(self):
        """Живой сеанс 2026-10-14: без этой логики панель показывала «Ядро не найдено»."""
        ok, detail = panel_boots_headless_without_pythonpath()
        self.assertTrue(ok, "панель не нашла ядро при обычном запуске: " + detail)

    def test_policy_editing_helpers(self):
        section={'enforce':False,'rules':[]}
        section,error=self.panel.policy_add_rule(section,'git','status',True,'смотреть')
        self.assertEqual(error,'')
        rejected,error=self.panel.policy_add_rule(section,'','пустая',True,'')
        self.assertIsNone(rejected,'пустая команда не добавляется')
        self.assertIn('команда',error)
        section,error=self.panel.policy_add_rule(section,'curl','',False,'')
        rows=self.panel.policy_rows(section)
        self.assertEqual([row['name'] for row in rows],['rule-1','rule-2'])
        self.assertEqual(rows[0]['command'],'git')
        self.assertTrue(rows[0]['allow'])
        self.assertEqual(rows[0]['note'],'смотреть')
        self.assertFalse(rows[1]['allow'])
        section=self.panel.policy_remove_rule(section,'rule-1')
        self.assertEqual([row['command'] for row in self.panel.policy_rows(section)],['curl'])
        section=self.panel.policy_set_enforce(section,True)
        self.assertTrue(section['enforce'])
        # Пустая политика без enforce → честное «по умолчанию» (секция удаляется).
        self.assertEqual(self.panel.policy_rows(None),[])

    def test_providers_summary_has_no_secret_fields(self):
        rows=self.panel.providers_summary([AvailabilityDouble('codex',False,reason='нет ключа',version=None)])
        self.assertEqual(sorted(rows[0]),['available','provider','reason','version'])
        self.assertFalse(rows[0]['available'])

    def test_time_formats_iso_and_survives_garbage(self):
        self.assertRegex(self.panel.format_time('2026-10-12T07:04:00Z'),r'^\d{2}\.\d{2} в \d{2}:\d{2}$')
        self.assertEqual(self.panel.format_time('не дата'),'время неизвестно')
        self.assertEqual(self.panel.format_time(None),'время неизвестно')

    def test_markup_in_names_is_escaped_not_rendered(self):
        # Adw.ActionRow и описание Adw.StatusPage разбирают Pango-разметку.
        self.assertEqual(self.panel.markup('<b>злой</b> & co'),'&lt;b&gt;злой&lt;/b&gt; &amp; co')
        self.assertEqual(self.panel.markup(None),'')
        agent=AgentDouble('weird','Тестер <b>злой</b>','роль & <i>курсив</i>',provider='codex')
        subtitle=self.panel.agent_subtitle(agent,[AvailabilityDouble('codex',True)])
        self.assertIn('<i>курсив</i>',subtitle,'чистая функция не экранирует — это делает UI')


# --- Контракт файлов: без дисплея -------------------------------------------

class PanelContractTests(unittest.TestCase):
    def test_panel_is_executable_script_with_main_guard(self):
        self.assertTrue(os.access(PANEL,os.X_OK),'панель должна быть исполняемой')
        self.assertTrue(SOURCE.startswith('#!/usr/bin/python3\n'))
        self.assertIn("if __name__=='__main__':",SOURCE)

    def test_ui_uses_thread_idle_add_and_no_sql_or_network(self):
        self.assertIn('GLib.idle_add',SOURCE)
        self.assertIn('threading.Thread',SOURCE)
        self.assertIn('list_events(session_id,limit=EVENT_BATCH)',SOURCE.replace(' ',''))
        for forbidden in ('sqlite3','INSERT INTO','SELECT ','import socket','import requests','urllib.request'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden,SOURCE)
        self.assertIn('--smoke-test',SOURCE)

    def test_desktop_entry_launches_the_panel(self):
        parser=configparser.ConfigParser(interpolation=None)
        parser.read_string(DESKTOP.read_text())
        entry=parser['Desktop Entry']
        self.assertEqual(entry['Type'],'Application')
        self.assertEqual(entry['Exec'],'runneos-agents-panel')
        self.assertEqual(entry['Terminal'],'false')
        self.assertTrue(entry['Name'])
        self.assertTrue(entry['Name[ru]'])
        self.assertTrue(entry['Comment[ru]'])
        self.assertIn('Utility',entry['Categories'])
        self.assertEqual(entry['StartupWMClass'],'org.runneos.Agents')

    def test_desktop_entry_has_no_secrets_and_no_shell(self):
        text=DESKTOP.read_text().lower()
        for forbidden in ('token','password','secret','api_key','sh -c','bash -c'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden,text)


# --- Сборка окна: только при наличии дисплея --------------------------------

@unittest.skipUnless(HAVE_DISPLAY,'нужен дисплей (X11/Wayland)')
class PanelWindowTests(unittest.TestCase):
    def setUp(self):
        # Двойная защита: даже если панель получит настоящее ядро, каталог данных —
        # временный, а не ~/.local/share/runneos/agents.
        self.temporary=Path(tempfile.mkdtemp(prefix='runneos-agents-panel-home-'))
        self.environment=mock.patch.dict(os.environ,{'XDG_DATA_HOME':str(self.temporary),
                                                     'XDG_CACHE_HOME':str(self.temporary)})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        widget=getattr(self,'window',None)
        if widget is not None:
            root=getattr(widget,'test_root',None)
            widget.destroy()
            delattr(self,'window')
            if root is not None:shutil.rmtree(root,ignore_errors=True)
        shutil.rmtree(self.temporary,ignore_errors=True)

    def make(self,db=None,drop_core=False,name=None):
        self.module,self.window=build_panel(db,drop_core,
                                            name=name or f'panel_{self._testMethodName}_{id(db)}')
        return self.window

    def database(self):
        return FakeDatabase(
            agents=[AgentDouble('tester','Тестер','Отвечает ровно PONG','codex','idle')],
            sessions=[SessionDouble('s1','tester','Первая сессия')],
            messages=[MessageDouble(1,'s1','user','ответь ровно PONG'),
                      MessageDouble(2,'s1','agent','PONG')],
            events=[EventDouble(1,'s1','turn.started',{'kind':'turn.started'},1)],
            providers=[AvailabilityDouble('codex',True,version='0.9.0'),
                       AvailabilityDouble('opencode',False,reason='opencode CLI не найден')])

    def test_agents_sessions_and_messages_are_rendered(self):
        db=self.database()
        self.make(db)
        self.assertEqual(list(self.window.agent_rows),['tester'])
        self.assertEqual(self.window.session_stack.get_visible_child_name(),'list')
        self.assertEqual(self.window.feed_stack.get_visible_child_name(),'messages')
        texts=collect_texts(self.window.message_box)
        self.assertTrue(any('PONG'==text for text in texts))
        self.assertTrue(any(text.startswith('Агент · ') for text in texts))
        self.assertTrue(any(text.startswith('Вы · ') for text in texts))
        self.assertEqual(self.window.agent_heading.get_text(),self.module.agent_title(db.agents[0]))

    def test_unavailable_provider_is_labeled_in_agent_row(self):
        db=self.database()
        db.agents.append(AgentDouble('helper','Помощник','Считает','opencode','unavailable'))
        self.make(db)
        texts=collect_texts(self.window.agent_rows['helper'])
        self.assertIn('нет провайдера',texts)
        self.assertTrue(any('провайдер: opencode' in text for text in texts))
        self.assertNotIn('opencode CLI не найден',' '.join(texts))

    def test_markup_in_agent_name_is_escaped_in_row(self):
        db=self.database()
        db.agents.append(AgentDouble('weird','Тестер <b>злой</b> & co','роль <i>италик</i>','codex','idle'))
        self.make(db)
        row=self.window.agent_rows['weird']
        self.assertIn('Тестер &lt;b&gt;злой&lt;/b&gt; &amp; co',row.get_title())
        self.assertNotIn('<b>',row.get_title())
        self.assertIn('&lt;i&gt;италик&lt;/i&gt;',row.get_subtitle())

    def test_state_label_is_shown_once_even_without_provider(self):
        db=self.database()
        db.agents.append(AgentDouble('helper','Помощник','Считает','opencode','unavailable'))
        self.make(db)
        helper=collect_texts(self.window.agent_rows['helper'])
        self.assertEqual(helper.count('нет провайдера'),1,helper)
        tester=collect_texts(self.window.agent_rows['tester'])
        self.assertIn('простаивает',tester)

    def test_ask_button_locked_during_run_and_the_run_is_reported(self):
        db=self.database()
        self.make(db)
        self.window.prompt_entry.set_text('ответь ровно PONG')
        self.assertTrue(self.window.ask_button.get_sensitive())
        self.window.start_task()
        self.assertTrue(db.ask_started.wait(5),'прогон не начался')
        self.window.dispatcher.flush()
        self.assertFalse(self.window.ask_button.get_sensitive(),'кнопка активна во время прогона')
        self.assertTrue(self.window.run_spinner.get_spinning())
        self.assertFalse(self.window.prompt_entry.get_sensitive())
        self.window.prompt_entry.set_text('ещё задача')
        self.assertFalse(self.window.ask_button.get_sensitive(),'текст не должен разблокировать кнопку в прогоне')
        db.release.set()
        self.window.run_thread.join(10)
        if self.window.providers_thread is not None:self.window.providers_thread.join(5)
        self.window.dispatcher.flush()
        self.assertFalse(self.window.run_spinner.get_spinning())
        self.assertEqual(self.window.status_label.get_text(),'Готово · токены · вход: 10, выход: 2')
        self.assertEqual(self.window.prompt_entry.get_text(),'ещё задача')
        self.assertTrue(self.window.prompt_entry.get_sensitive())
        self.assertTrue(self.window.ask_button.get_sensitive(),'после прогона кнопка снова активна при тексте')
        feed=collect_texts(self.window.message_box)
        self.assertTrue(any('PONG'==text for text in feed))
        progress=collect_texts(self.window.progress_box)
        self.assertTrue(any('Ход начался'==text for text in progress),progress)
        self.assertTrue(any('Агент: PONG'==text for text in progress),progress)
        self.assertTrue(self.window.progress_revealer.get_reveal_child())
        self.assertEqual(db.ask_calls,[('tester','ответь ровно PONG','s1')])
        self.assertIn(EVENT_BATCH,db.event_reads)
        self.window.prompt_entry.set_text('')
        self.assertFalse(self.window.ask_button.get_sensitive(),'без текста задачи кнопка неактивна')
        self.window.prompt_entry.set_text('следующая задача')
        self.assertTrue(self.window.ask_button.get_sensitive())

    def test_failed_run_shows_error_without_traceback(self):
        db=self.database()
        db.ok=False;db.error='codex CLI не найден'
        self.make(db)
        self.window.prompt_entry.set_text('ответь ровно PONG')
        self.window.start_task()
        self.assertTrue(db.ask_started.wait(5))
        db.release.set();self.window.run_thread.join(10)
        if self.window.providers_thread is not None:self.window.providers_thread.join(5)
        self.window.dispatcher.flush()
        status=self.window.status_label.get_text()
        self.assertIn('Прогон не удался: codex CLI не найден',status)
        texts=collect_texts(self.window.progress_box)+[status]
        self.assertFalse(any('Traceback' in text for text in texts),texts)
        self.assertFalse(self.window.ask_button.get_sensitive())
        self.window.prompt_entry.set_text('повторить')
        self.assertTrue(self.window.ask_button.get_sensitive())

    def test_crash_in_run_is_reported_without_traceback(self):
        db=self.database()
        db.raise_error=RuntimeError('провайдер оборвал соединение')
        self.make(db)
        self.window.prompt_entry.set_text('ответь ровно PONG')
        self.window.start_task()
        self.assertTrue(db.ask_started.wait(5))
        db.release.set();self.window.run_thread.join(10)
        if self.window.providers_thread is not None:self.window.providers_thread.join(5)
        self.window.dispatcher.flush()
        status=self.window.status_label.get_text()
        self.assertIn('Прогон не выполнен: RuntimeError: провайдер оборвал соединение',status)
        self.assertNotIn('Traceback',status)
        self.assertFalse(self.window.run_spinner.get_spinning())

    def test_incremental_polling_skips_events_already_shown(self):
        db=self.database()
        db.events=[EventDouble(1,'s1','turn.started',{'kind':'turn.started'},1),
                   EventDouble(2,'s1','agent_message',{'kind':'agent_message','text':'раз'},2),
                   EventDouble(3,'s1','turn.completed',{'kind':'turn.completed'},3)]
        self.make(db)
        newest=self.window.poll_once(StoreDouble(db),'s1',1)
        self.window.dispatcher.flush()
        self.assertEqual(newest,3)
        progress=collect_texts(self.window.progress_box)
        self.assertEqual(len(progress),2,progress)
        self.assertTrue(any('Агент: раз'==text for text in progress))
        self.assertFalse(any('Ход начался'==text for text in progress),'старое событие показано повторно')
        self.assertEqual(self.window.poll_once(StoreDouble(db),'s1',3),3)
        self.window.dispatcher.flush()
        self.assertEqual(len(collect_texts(self.window.progress_box)),2,'новых событий нет, лента не должна расти')

    def test_empty_states_for_agent_sessions_and_messages(self):
        db=FakeDatabase(agents=[AgentDouble('tester','Тестер')],
                        providers=[AvailabilityDouble('codex',True)])
        self.make(db)
        self.assertEqual(self.window.session_stack.get_visible_child_name(),'hint')
        self.assertIn('сессия создастся автоматически',self.window.session_hint.get_text())
        self.assertEqual(self.window.feed_placeholder.get_title(),'У агента нет сессий')
        self.assertEqual(self.window.feed_stack.get_visible_child_name(),'empty')

    def test_session_without_messages_offers_to_write_a_task(self):
        db=FakeDatabase(agents=[AgentDouble('tester','Тестер')],
                        sessions=[SessionDouble('s1','tester','Пустая сессия')],
                        providers=[AvailabilityDouble('codex',True)])
        self.make(db)
        self.assertEqual(self.window.session_stack.get_visible_child_name(),'list')
        self.assertEqual(self.window.feed_placeholder.get_title(),'Сообщений пока нет')
        self.assertIn('«Задача»',self.window.feed_placeholder.get_description())

    def test_no_agents_state_points_to_the_cli(self):
        db=FakeDatabase(providers=[AvailabilityDouble('codex',False,reason='codex CLI не найден')])
        self.make(db)
        self.assertEqual(self.window.agent_stack.get_visible_child_name(),'empty')
        self.assertEqual(self.window.agent_placeholder.get_title(),'Нет агентов')
        self.assertIn('runneos-agents agent add',self.window.agent_placeholder.get_description())
        self.assertEqual(self.window.feed_placeholder.get_title(),'Нет агентов')
        self.assertEqual(self.window.session_stack.get_visible_child_name(),'empty')
        self.assertEqual(self.window.session_placeholder.get_title(),'Нет агентов')
        self.assertFalse(self.window.ask_button.get_sensitive())

    def test_window_without_core_degrades(self):
        self.make(drop_core=True,name=f'panel_no_core_{id(self)}')
        self.assertIsNone(self.window.store)
        self.assertFalse(self.window.core_ready)
        self.assertEqual(self.window.agent_placeholder.get_title(),'Ядро runneos_agents не найдено')
        self.assertIn('runneos-agents init',self.window.agent_placeholder.get_description())
        self.assertEqual(self.window.feed_placeholder.get_title(),'Ядро runneos_agents не найдено')
        self.assertFalse(self.window.ask_button.get_sensitive())
        self.window.prompt_entry.set_text('задача')
        self.assertFalse(self.window.ask_button.get_sensitive(),'без ядра задача не запускается')
        self.window.start_task()
        self.assertIsNone(self.window.run_thread,'прогон без ядра запускать нельзя')
        self.assertIn('недоступно',self.window.status_label.get_text())


if __name__=='__main__':unittest.main()
