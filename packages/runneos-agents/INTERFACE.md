# RunneOS Agents — контракт (FROZEN v1.10)

Дата: 2026-10-13. Область: исходники пакета `runneos-agents` в репозитории `~/runneos`.
Статус: **реализовано и проверено на машине**.

Изменения версии:

| Версия | Что изменено |
|---|---|
| v1 | первая редакция контракта |
| v1.1 | добавлен `Orchestrator.ask(new_session=True)` — «начать новую сессию» для `--new` |
| v1.2 | контракт приведён в соответствие с кодом после независимого аудита: раскладка файлов (виджет в `files/dms-plugin/`), полный словарь событий журнала, поля `transient`/`turn.finished`, фактические структуры `Message`/`Event`, схема БД (`PRAGMA user_version` вместо таблицы, индекс `sessions_agent`), полный список методов `Store`, флаги CLI (`--after`, `--instructions`, `--no-workspace`), честное описание `ask` (без стрима событий) |
| v1.3 | ворота политик подключены к рантайму (решение владельца 2026-10-13 — семантика A «сначала аудит»): новый вид события `policy.decision` (оркестратор, до запуска провайдера), секция `policy` в манифесте воркспейса (`enforce` + `rules`), шаг ворот в последовательности `ask`, `doctor` показывает политику каждого агента. Коды возврата и схема БД не менялись |
| v1.4 | авторизация провайдеров (additive): структура `Authorization` (`authorized: true/false/None`, `None` — неизвестно), метод адаптера `authorization()` (codex — `codex login status`, opencode — `opencode auth list`), шаг проверки авторизации в `ask` (не авторизован → честный отказ, процесс не запускается), подкоманда `provider auth-status [--json]` |
| v1.5 | песочница хода (решение владельца 2026-10-13, выключена по умолчанию): секция `sandbox` манифеста воркспейса (`mode: systemd-run` + `memory_max`/`tasks_max`/`cpu_quota`, сломанная секция запрещает ход), kwarg `sandbox_prefix` в `adapter.start/resume` (префикс `systemd-run --user --scope --unit=<уникальный>`; юнит останавливается при завершении хода — scope-процесс принадлежит менеджеру и killpg его не достаёт), шаг песочницы в `ask`, `doctor` показывает песочницу каждого агента. Новый модуль `sandbox.py` |
| v1.6 | слой файловой изоляции (additive): ключ `sandbox.filesystem` (`none` по умолчанию / `bwrap`) — bwrap поверх лимитов: корень только чтение, rw воркспейс и каталоги провайдера (`adapter.data_dirs()`, новый метод протокола: codex — `~/.codex`, opencode — каталоги XDG), сеть не изолируется (нужна для API), приватный `/tmp` |
| v1.7 | управление политиками (решение владельца 2026-10-13 — экран управления даёт основание включать принуждение): CLI `agent policy ID [--enforce on/off] [--allow CMD] [--deny CMD] [--args SUB] [--note N] [--remove NAME] [--reset] [--json]` (просмотр + правка секции; сломанную секцию сохранить нельзя), ядро `policy.read_section/write_section` (атомарная запись манифеста с валидацией), панель: диалог «Политика» (правила, принуждение, добавление/удаление) — UI пишет политику только через ядро (дополнение §8) |
| v1.8 | межагентные каналы (additive): CLI `send FROM TO PROMPT [--run] [--new] [--json]`, модуль `channels.py` (`delegate`, `HandoffResult`), новые виды событий `task.received`/`task.sent` (словарь §4). Без `--run` передача только записывается в журнал получателя; с `--run` ход получателя проходит через все его ворота. Схема БД не менялась |
| v1.9 | локальный API (additive, opt-in): CLI `serve [--socket PATH]`, модуль `api.py` (`AgentApi`, `serve`, `default_socket_path`) — построчный JSON поверх unix-сокета (`$XDG_RUNTIME_DIR/runneos/agents.sock`, 0600): `ping`, `status`, `providers`, `auth-status`, `agents`, `sessions`, `log`, `ask` (ход через все ворота; ходы сериализуемы). Чтение не создаёт данные (проба db_path до открытия Store). Ядро по-прежнему ничего не запускает само: сервер гасит владелец |
| v1.10 | сервис агентов (решение владельца 2026-10-14 — закрыть разрыв с openbot): **схема БД v2** — таблица `tasks` (FIFO по `position`; состояния queued/running/paused/done/failed/canceled; миграция 1→2 автоматом), Store API (`create_task`, `get_task`, `list_tasks`, `set_task_state` — терминальные итоги не переписываются, `claim_next_task` — атомарный FIFO, `recover_running_tasks` — crash-safe, `task_counts`), `Orchestrator.ask(should_cancel=…)`, CLI `task add/list/show/cancel`, API-операции `task-add/list/show/cancel`. Исполнитель — отдельный пакет `runneos-agents-service` (opt-in юнит; ход через все ворота; отмена закрывает стрим) |

Любое дальнейшее изменение — отдельная ревизия с записью в таблице выше и явным уведомлением
исполнителей.

## 1. Назначение

Локальный «пульт агентов» для RunneOS: постоянные ИИ-агенты на своей машине, у каждого — свой
воркспейс, своя история, свои права. Модель доступа — через уже установленные и оплаченные CLI
(codex, opencode), без собственного облачного control plane.

Принципы:
1. **Только свободные лицензии** в собственном коде (MIT) и в зависимостях.
2. **CLI-провайдер — внешняя программа**, вызывается subprocess'ом; её код не копируется в пакет.
3. **Отсутствие провайдера — честное состояние**, а не падение: `unavailable`, а не выдуманный ответ.
4. **Журнал — источник правды.** Каждое событие сессии сначала пишется в журнал, потом показывается.
5. Ничего не мешает входу, сети и обновлению ОС: пакет не является зависимостью сеанса и не
   запускает сам себя (ни служб, ни таймеров, ни автозапуска).

## 2. Раскладка файлов

```
packages/runneos-agents/
├── PKGBUILD                  # pkgname=runneos-agents, license=('MIT'), arch=('any')
├── README.md                 # инструкция и честные ограничения
├── LICENSE                   # MIT
├── INTERFACE.md              # этот файл
└── files/
    ├── runneos_agents/       # python-модуль (импортируется тестами)
    │   ├── __init__.py       # __version__, публичные имена
    │   ├── model.py          # чистые dataclass'ы, без ввода-вывода
    │   ├── store.py          # SQLite: схема, миграции, журнал, проекции
    │   ├── workspace.py      # воркспейс агента: путь, манифест, git-инициализация
    │   ├── routing.py        # выбор адаптера, разрешение каталогов, внешние адаптеры
    │   ├── policy.py         # ворота политик: правила, решение по провайдеру, загрузка секции policy
    │   ├── sandbox.py        # песочница хода: секция sandbox, лимиты, префикс systemd-run --scope
    │   ├── channels.py       # межагентные каналы: delegate, task.received/task.sent (v1.8)
    │   ├── api.py            # локальный API: serve, AgentApi, JSON поверх unix-сокета (v1.9)
    │   ├── orchestrator.py   # запуск задачи, стриминг, персистенция сессии, RunLog
    │   └── adapters/
    │       ├── __init__.py   # build_registry()
    │       ├── base.py       # каркас CLI-адаптера, поиск бинаря, редакция секретов
    │       ├── codex.py      # codex exec --json / exec resume --json
    │       ├── opencode.py   # opencode run --standalone --format json
    │       ├── stub.py       # фиктивный адаптер для тестов и панели
    │       └── fixtures/     # строки JSONL из живых прогонов
    ├── runneos-agents        # CLI (исполняемый, python3)
    ├── runneos-agents-panel  # GTK4/libadwaita UI
    ├── org.runneos.Agents.desktop
    └── dms-plugin/           # виджет DMS: RunneAgentsWidget.qml + plugin.json
tests/
├── test_agents_model.py
├── test_agents_store.py
├── test_agents_orchestrator.py
├── test_agents_adapters.py
├── test_agents_cli.py
├── test_agents_panel.py
├── test_agents_channels.py  # v1.8: передача задач между агентами
├── test_agents_api.py       # v1.9: локальный API (диспетчер и сокет)
├── test_agents_docs.py     # сторож: числа и пути в документации против фактов
├── test_firstboot_agents.py # пакет runneos-agents-firstboot (вне шаблона test_agents_*)
└── check_agents_live.py     # живой протокол §10 (запуск вручную)
```

Пакет устанавливает: `/usr/bin/runneos-agents`, `/usr/bin/runneos-agents-panel`,
`/usr/share/runneos/agents/runneos_agents/**`, `/usr/share/dms-plugins/RunneosAgents/**`,
`/usr/share/applications/org.runneos.Agents.desktop`, лицензию и README.
DMS читает плагины из `~/.config/DankMaterialShell/plugins/<id>/`, поэтому виджет
устанавливается копированием каталога (см. README); автоматического шага установки нет.

Тесты — только `unittest` из стандартной библиотеки, запуск:
`PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_agents_*.py' -v`

## 3. Данные (model.py) — заморожено

```python
@dataclass(frozen=True)
class Agent:
    id: str                    # slug, [a-z0-9-], до 64 символов, уникален
    name: str                  # непустое
    role: str = ""             # описание роли
    provider: str = "codex"
    workspace: Path | None = None   # None → вычислить из корня
    created_at: str = ""       # ISO-8601 UTC, суффикс Z
    state: str = "idle"        # idle | running | waiting_user | error | unavailable
    instructions: str = ""

@dataclass(frozen=True)
class Session:
    id: str                    # uuid4 hex
    agent_id: str
    title: str = ""
    provider_session_id: str | None = None   # thread_id у провайдера
    created_at: str = ""
    updated_at: str = ""
    state: str = "open"        # open | closed | error
    # свойство resumable = provider_session_id есть и state == "open"

@dataclass(frozen=True)
class Message:
    session_id: str
    role: str                  # user | agent | system
    text: str
    id: int | None = None      # rowid, None до вставки
    created_at: str = ""
    event_id: int | None = None

@dataclass(frozen=True)
class Event:
    session_id: str
    kind: str                  # непустое
    payload: dict = field(default_factory=dict)
    id: int | None = None
    created_at: str = ""
    seq: int = 0               # монотонный номер внутри сессии

@dataclass(frozen=True)
class Task:                    # v1.10: задача очереди сервиса агентов
    id: str                    # "task-<hex8>"
    agent_id: str
    prompt: str
    state: str = "queued"      # queued | running | paused | done | failed | canceled
    position: int = 0          # FIFO внутри агента
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    session_id: str | None = None   # ход задачи = обычная сессия с журналом
    error: str | None = None
    result: str | None = None

@dataclass(frozen=True)
class TurnResult:
    ok: bool
    session_id: str
    provider_session_id: str | None = None
    text: str = ""
    error: str | None = None
    usage: dict | None = None
    exit_code: int | None = None
```

Правила: неизвестное значение — `None`, не `0`/`""`. Время — ISO-8601 UTC с суффиксом `Z`.
Идентификаторы агентов валидируются регуляркой `^[a-z0-9][a-z0-9-]{0,63}$`.
`Availability` и `ProviderUnavailable` живут в `model.py` (общий контракт ядра и адаптеров).

## 4. Хранилище (store.py) — заморожено

Класс `Store(path: Path | str | None = None)`, контекстный менеджер, WAL,
`PRAGMA foreign_keys=ON`, `busy_timeout=5000`, `isolation_level=None` (транзакции
управляются явно через `BEGIN IMMEDIATE`). Корень по умолчанию:
`$RUNNEOS_AGENTS_HOME` → `$XDG_DATA_HOME/runneos/agents` → `~/.local/share/runneos/agents`;
файл `agents.db`, воркспейсы — `<root>/workspaces/<agent_id>/`, журналы прогонов —
`<workspace>/runs/<session_id>.jsonl`.

Версия схемы хранится в `PRAGMA user_version` (значение `SCHEMA_VERSION`), отдельной таблицы
версии нет. Миграции применяются по возрастанию версии, каждая — в своей транзакции.

SQL (v1; расширения — только дополнительными миграциями):

```sql
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL DEFAULT '',
  provider TEXT NOT NULL DEFAULT 'codex', workspace TEXT,
  instructions TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'idle',
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  title TEXT NOT NULL DEFAULT '', provider_session_id TEXT,
  state TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  role TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL, event_id INTEGER);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS events_session_seq ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS sessions_agent ON sessions(agent_id, updated_at);
```

Методы:

```python
Store(path=None) -> Store
close() ; __enter__/__exit__ ; connection  # свойство, sqlite3.Connection
# агенты
upsert_agent(agent: Agent) -> Agent          # created_at существующего не меняется
get_agent(agent_id: str) -> Agent | None
list_agents() -> list[Agent]                 # сортировка по имени без учёта регистра
remove_agent(agent_id: str) -> bool          # каскад по FK
set_agent_state(agent_id: str, state: str) -> None
# сессии
create_session(agent_id: str, title: str = "") -> Session
get_session(session_id: str) -> Session | None
list_sessions(agent_id: str | None = None, limit: int = 50) -> list[Session]
latest_open_session(agent_id: str) -> Session | None
set_provider_session_id(session_id: str, provider_session_id: str) -> None
set_session_state(session_id: str, state: str) -> None
touch_session(session_id: str) -> None        # обновляет updated_at
# сообщения и журнал
add_message(session_id: str, role: str, text: str, event_id: int | None = None) -> Message
list_messages(session_id: str, limit: int = 200) -> list[Message]
append_event(session_id: str, kind: str, payload: dict) -> Event   # seq = max+1 ВНУТРИ сессии
list_events(session_id: str, limit: int = 500) -> list[Event]      # последние N, по возрастанию seq
events_after(session_id: str, seq: int, limit: int = 500) -> list[Event]  # инкрементальное чтение
stats() -> dict                               # schema_version, counts, last_activity, путь и размер
```

Гарантии: `append_event` атомарен (`BEGIN IMMEDIATE`), `seq` монотонен и уникален **внутри
сессии** (у каждой сессии нумерация с 1); `upsert_agent` не меняет `created_at`; удаление агента
каскадно удаляет сессии, сообщения и события.

### Словарь видов событий (`Event.kind`)

Пишутся оркестратором (`orchestrator.py`) и адаптерами (`adapters/base.py`, `codex.py`):

| kind | Кто пишет | Смысл |
|---|---|---|
| `user_message` | оркестратор | задача пользователя (payload: `text`) |
| `turn.started` | адаптер | начало хода |
| `provider_session` | адаптер | идентификатор сессии провайдера (payload: `provider_session_id`) |
| `agent_message` | адаптер | ответ агента (payload: `text`); дублируется в `messages(role='agent')` |
| `reasoning` | адаптер | рассуждение, если провайдер его отдаёт |
| `command` | адаптер | выполненная команда (payload: `text`) |
| `file_change` | адаптер | изменённый файл (payload: `path`, `text`) |
| `usage` | адаптер | расход (payload: `usage`) |
| `error` | адаптер и оркестратор | ошибка; `transient: true` — временное сообщение провайдера; у отказов оркестратора `stage` называет этап (`availability`, `authorization` — с `how`, `policy`, `start`, `stream`) |
| `policy.decision` | оркестратор | решение ворот политик о запуске провайдера, до старта процесса (payload: `action='provider-run'`, `provider`, `allowed`, `rule`, `reason`, `enforce`, `source`; при сломанной секции — `policy_error`) |
| `raw` | адаптер | неизвестный тип события провайдера (payload: `payload`) |
| `turn.completed` | адаптер | конец хода у провайдера (payload: `exit_code`) |
| `turn.finished` | оркестратор | итог хода: `ok`, `outcome`, `text`, `usage`, `provider_session_id`, `exit_code`, `transient_errors`, `error` |
| `task.received` | оркестратор (каналы, v1.8) | передача задачи получателю (payload: `from`, `text`, `handoff_id`); дублируется `messages(role='system')` |
| `task.sent` | оркестратор (каналы, v1.8) | след передачи в открытой сессии отправителя (payload: `to`, `text`, `handoff_id`) |

## 5. Адаптеры (adapters/base.py) — заморожено

```python
@dataclass(frozen=True)
class Availability:
    provider: str
    available: bool
    reason: str = ""          # человекочитаемо, без секретов
    version: str | None = None
    binary: str | None = None

@dataclass(frozen=True)
class Authorization:          # v1.4: состояние входа у провайдера
    provider: str
    authorized: bool | None   # None — неизвестно (не «нет»): команда не отработала/нет у провайдера
    detail: str = ""          # человекочитаемая причина или цитата ответа CLI
    how: str = ""             # штатная команда входа: «codex login», «opencode auth login»

class Adapter(Protocol):
    provider: str             # "codex" | "opencode" | ...
    def availability(self) -> Availability: ...
    def authorization(self) -> Authorization: ...   # v1.4; у внешних адаптеров может не быть
    def data_dirs(self) -> list[Path]: ...          # v1.6: каталоги записи вне воркспейса (для bwrap)
    def start(self, ws: Path, prompt: str, *, model: str | None = None,
              instructions: str = "", timeout: float | None = None,
              sandbox_prefix: Sequence[str] | None = None) -> Iterator[dict]: ...   # v1.5
    def resume(self, ws: Path, provider_session_id: str, prompt: str, *, model: str | None = None,
               timeout: float | None = None,
               sandbox_prefix: Sequence[str] | None = None) -> Iterator[dict]: ...   # v1.5
```

`authorization()` спрашивает состояние входа штатной командой самого CLI (`auth_args`:
codex — `login status`, opencode — `auth list`) и никогда не читает каталоги провайдера
(`~/.codex`, `~/.local/share/opencode`) и его секреты; у каркаса это `CliJsonlAdapter.authorization()`
с переопределяемым разбором (`interpret_auth`). Сбой запуска проверки — `authorized=None`
с причиной, а не догадка.

`start`/`resume` — генераторы **нормализованных событий** (единственный контракт между
адаптером и оркестратором):

```python
{"kind": "turn.started"}
{"kind": "provider_session", "provider_session_id": "..."}
{"kind": "agent_message", "text": "..."}
{"kind": "reasoning", "text": "..."}
{"kind": "command", "text": "..."}
{"kind": "file_change", "path": "...", "text": "..."}
{"kind": "usage", "usage": {...}}
{"kind": "error", "text": "...", "transient": True}   # transient только для сообщений о повторе
{"kind": "turn.completed", "exit_code": 0}
{"kind": "raw", "payload": {...}}                     # неизвестный тип события провайдера
```

Правила адаптера:
- Никогда не бросать наружу секреты: аргументы и окружение логируются с редактированием.
- Отсутствие бинаря → `availability().available is False` с причиной; `start` бросает
  `ProviderUnavailable`.
- `transient: true` — провайдер сам сообщил о повторе (`Reconnecting…`, `stream disconnected`,
  `connection reset`); такой ход не считается провалом, если ответ в итоге получен.
- Ненулевой код возврата → событие `error` + `turn.completed.exit_code`, без исключения наружу.
- Таймаут → убить процесс (группу процессов) и вернуть `error`, `exit_code=None`.
- Адаптер не пишет в БД: только отдаёт события.

Расширение: если задана переменная окружения `RUNNEOS_AGENTS_ADAPTER_PATH`, ядро (`routing.py`)
загружает указанный python-файл и вызывает в нём `build_registry()`. Это точка расширения для
тестов и внешних адаптеров; файл исполняется как код, поэтому переменную нельзя задавать
произвольным значением.

## 6. Оркестратор (orchestrator.py) — заморожено

```python
class Orchestrator:
    def __init__(self, store: Store, adapters: AdapterRegistry | None = None): ...
    def ask(self, agent_id: str, prompt: str, *, session_id: str | None = None,
            model: str | None = None, timeout: float | None = None,
            resume: bool = True, new_session: bool = False) -> TurnResult: ...
    def providers(self) -> list[Availability]: ...
    def availability(self, provider: str) -> Availability: ...
    def authorization(self, provider: str) -> Authorization: ...
```

Поведение `ask`:
1. Найти агента (нет — `TurnResult(ok=False, error="неизвестный агент: …")`).
2. Пустая задача — отказ без запуска процесса.
3. Взять сессию: `session_id` → она; иначе при `new_session` → новая; иначе
   `latest_open_session(agent)`; иначе `create_session`.
4. Подготовить воркспейс (путь сохраняется в агенте) и файловый журнал `RunLog`.
5. Записать `messages(role='user')` + `events(kind='user_message')` — до запуска процесса.
6. Выбрать адаптер по `agent.provider`. Недоступен → события + `messages(role='system')`,
   состояние агента `unavailable`, `ok=False`, **процесс не запускается**.
7. Авторизация (v1.4): спросить у адаптера состояние входа (`authorization()`). Явное
   «не авторизован» → событие `error` со `stage='authorization'` и `how` (штатная команда
   входа), системное сообщение, состояние агента `unavailable`, `ok=False`, **процесс не
   запускается**. `authorized=None` («неизвестно») отказом не считается. У адаптера без
   метода `authorization()` (внешние старого образца) — «неизвестно», ход не блокируется.
8. Ворота политик: загрузить политику воркспейса (`policy` секция манифеста; нет — набор
   по умолчанию с явным допуском провайдера, режим «только аудит»), вычислить решение для
   `provider-run` и записать событие `policy.decision` в базу и файловый журнал. Если
   `enforce` включён и решение «запрещено» (в том числе сломанная секция — она запрещает
   при любом enforce) — честный отказ: `TurnResult(ok=False, error=…)` с именем правила,
   `error` событие со `stage='policy'`, системное сообщение в ленте; агент не переходит
   в `running`, процесс не запускается, сессия остаётся открытой.
9. Песочница (v1.5): загрузить секцию `sandbox` манифеста. Сломанная секция (в том числе
   неизвестный `mode`) — честный отказ: `error` со `stage='sandbox'`, процесс не
   запускается. Включённая песочница превращается в `sandbox_prefix`
   (`systemd-run --user --scope --unit=<уникальное> -pMemoryMax=… -pTasksMax=… [-pCPUQuota=…]`),
   который уходит в `adapter.start/resume`; после хода (штатного или по таймауту) юнит
   останавливается `systemctl --user stop` — scope-процесс принадлежит менеджеру systemd
   и killpg его не достаёт (проверено на systemd 262).
10. Если у сессии есть `provider_session_id` и `resume=True` → `adapter.resume`, иначе `adapter.start`;
    перед стартом состояние агента `running`, сессии `open`.
11. Каждое событие генератора: `append_event` + запись в файловый журнал; `agent_message`
    дополнительно → `messages(role='agent')`; `provider_session` → `set_provider_session_id`;
    `usage` копится; `error` без `transient` — претендент на провал.
12. Итог по фактам (`_decide_outcome`): постоянная ошибка → провал; ответ получен → успех;
    ненулевой код возврата без ответа → провал с указанием кода; ответа нет и ошибок не было →
    «провайдер завершился, не дав ответа». Пишется `turn.finished`, состояние агента и сессии
    приводится в соответствие.
13. Возврат — `TurnResult`. Исключения провайдера и адаптера наружу не летят; ошибки самого
    хранилища (например, недоступный файл БД) не перехватываются — их гасит вызывающий (CLI
    печатает сообщение без трассировки).

### Ворота политик (policy.py)

Источник правил — секция `policy` манифеста воркспейса (`<workspace>/agent.json`):

```json
"policy": {
  "enforce": true,
  "rules": [
    {"command": "codex", "allow": true, "name": "allow-codex", "note": "ходы через codex"},
    {"command": "curl", "allow": false, "name": "no-network"}
  ]
}
```

- Секции нет → семантика A (решение владельца 2026-10-13): правила чтения `default_for`
  плюс явный допуск запуска провайдера (`default-allow-provider`), `enforce` выключен —
  решение только журналируется, поведение прежнее.
- Секция есть → правила из неё; `enforce` действует только при явном `true`.
- Решение по провайдеру (`check_provider`): правило с `command`, равным имени бинаря
  провайдера, первое совпавшее решает (запрет короткое замыкание); нет правила —
  `default-deny`. Правила с фильтром `args` к запуску провайдера не применяются.
- Ошибка разбора секции (не объект, `rules` не список, правило без `allow`) → fail-closed
  при любом `enforce`: пустые правила, ход запрещён, `policy_error` в событии и причина
  в отказе. Никогда не «работаем без политики».
- `ensure_workspace` переносит секции `policy` и `sandbox` при обновлении служебных полей
  манифеста — настройка владельца не теряется между ходами.

### Песочница хода (sandbox.py, v1.5–v1.6)

Источник — секция `sandbox` манифеста воркспейса:

```json
"sandbox": {
  "mode": "systemd-run",
  "memory_max": "512M",
  "tasks_max": 32,
  "cpu_quota": "50%",
  "filesystem": "bwrap"
}
```

- Секции нет → песочница выключена, ход идёт как раньше (у codex своя песочница записи).
- `mode` отличен от `systemd-run`, лимиты не того типа (`memory_max` не строка,
  `tasks_max` не целое, `cpu_quota` не `NN%`), `filesystem` не `none`/`bwrap` →
  fail-closed: ход запрещён, причина в событии `error` (`stage='sandbox'`).
  Никогда не «работаем с непонятной песочницей».
- Включённая секция → префикс `systemd-run --user --scope --unit=runneos-agents-<hex>
  -pMemoryMax=… -pTasksMax=… [-pCPUQuota=…]` перед argv провайдера; cwd и события не меняются.
- Слой bwrap (`filesystem: "bwrap"`, v1.6) продолжается тем же префиксом:
  `--ro-bind / /`, rw — только воркспейс и `data_dirs()` провайдера (codex — `~/.codex`,
  opencode — каталоги XDG; отсутствующие пропускаются `--bind-try`), `--tmpfs /tmp`
  (приватный), `--dev`, `--proc`, `--die-with-parent`; сеть не изолируется — нужна
  для вызовов API. Изоляция проверена живьём: `/usr` только чтение, `/tmp` приватный,
  воркспейс и `~/.codex` записываемы, сеть отвечает.
- Завершение хода (штатное, по таймауту, при исключении) всегда останавливает юнит
  `systemctl --user stop <unit>.scope`: scope-процесс принадлежит менеджеру пользователя,
  killpg процессной группы systemd-run payload не достаёт (проверено на systemd 262).
- Границы: лимиты ресурсов и файловая изоляция; сеть остаётся общей (см. §9);
  `systemd-run`/`bwrap` недоступны → ход падает с причиной в stderr,
  `doctor` показывает состояние секции.

### Задачи и служба очереди (v1.10)

Задача — запись в таблице `tasks` (схема v2, миграция автоматом): FIFO по `position`
внутри агента. Состояния: `queued → running → done|failed|canceled` (+ `paused`
зарезервирован). Правила:

- терминальные итоги не переписываются: `set_task_state` отклоняет переход из
  done/failed/canceled в любое другое; повтор той же записи полей (идемпотентная
  отмена) — разрешён;
- `claim_next_task` атомарен (BEGIN IMMEDIATE): две службы одну задачу не возьмут;
- `recover_running_tasks` на старте службы возвращает `running` в `queued`
  (crash-safe: ход не переживает смерть процесса, задача — да);
- ход задачи исполняет штатный `Orchestrator.ask` со `should_cancel` — все ворота
  действуют, журнал агента неотличим от обычного `ask`. Отмена: `should_cancel`
  возвращает истину → стрим закрывается (процесс провайдера гасится в `finally`),
  `turn.finished.outcome='canceled'`, агент остаётся `idle`, сессия — `error`;
- исполнитель — отдельный пакет `runneos-agents-service` (opt-in юнит
  `runneos-agents-service.service`, `WantedBy=default.target`, не включается
  автоматически — инвариант §1). Без службы очередь ждёт в базе.

### Файловый журнал прогона (RunLog)

`<workspace>/runs/<session_id>.jsonl`, по строке на событие: `{"seq", "at", "kind", "payload"}`.
Пишется с `fsync`; при невозможности записи прогон продолжается (журнал — удобство, не условие).
Перед записью значения ключей с признаками секрета (`*_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`,
`*_CREDENTIAL`, `*_AUTH`) заменяются на `<скрыто>`, длинные строки обрезаются.
**В SQLite payload пишется как есть** (редакция там — по значению, в адаптерах).

## 7. CLI — заморожено

`runneos-agents <команда>`; код возврата 0 при успехе, 1 при ошибке, 2 при неверном использовании.
`--json` принимается и до, и после подкоманды (один объект на строку).

| Команда | Поведение |
|---|---|
| `init` | создаёт корень, БД и каталоги `<root>/{workspaces,runs}`, печатает путь |
| `provider list [--json]` | доступность адаптеров; хранилище не открывается и данные не создаются |
| `provider auth-status [--json]` | v1.4: авторизация провайдеров (штатные команды их CLI) и подсказка «подключить: …»; read-only, как `provider list` |
| `agent add NAME [--role TEXT] [--provider codex] [--id SLUG] [--instructions TEXT] [--no-workspace]` | создать/обновить агента и воркспейс |
| `agent list [--json]` | список агентов; без данных — код 0 и подсказка про `init` |
| `agent show ID [--json]` | агент + последние сессии; без данных — код 1 с подсказкой |
| `agent remove ID` | удалить агента из базы (каскад); файлы воркспейса остаются |
| `agent policy ID [--json]` | v1.7: политика агента — источник, enforce, решение для провайдера, список правил |
| `agent policy ID --enforce on/off --allow CMD --deny CMD --args SUB --note N --remove NAME --reset` | v1.7: правка секции; сломанную секцию сохранить нельзя (код 1 с причиной); `--reset` удаляет секцию целиком |
| `ask ID PROMPT [--new] [--session ID] [--model M] [--timeout S] [--json]` | задача; финал в stdout, ошибка и расход в stderr. **Стрима событий нет**: промежуточный ход виден в журнале и в панели |
| `task add AGENT PROMPT [--json]` | v1.10: задача в очередь агента (FIFO); предупреждает, если служба `runneos-agents-service` не активна |
| `task list [--agent ID] [--limit N] [--json]` | v1.10: задачи, свежие сверху |
| `task show TASK_ID [--json]` | v1.10: состояние, метки, итог |
| `task cancel TASK_ID [--json]` | v1.10: отмена; выполняющийся ход останавливает служба (стрим закрывается, процесс провайдера гасится) |
| `send FROM TO PROMPT [--run] [--new] [--model M] [--timeout S] [--json]` | v1.8: передача задачи агенту-получателю; без `--run` — только запись в журнал (`task.received` + системное сообщение), с `--run` — ход получателя через все его ворота; отправителю — `task.sent` в открытую сессию. Петля (FROM=TO) и неизвестные агенты — код 1 |
| `sessions ID [--limit N] [--json]` | сессии агента |
| `log SESSION_ID [--limit N] [--after SEQ] [--json]` | журнал событий сессии (инкрементально при `--after`) |
| `doctor [--json]` | read-only проверки: корень, база, схема, воркспейсы, провайдеры, политика и песочница каждого агента (сломанные секции — неуспешные проверки) |
| `serve [--socket PATH] [--json]` | v1.9: локальный API — построчный JSON поверх unix-сокета; opt-in, останавливается Ctrl+C. Операции: `ping`, `status`, `providers`, `auth-status`, `agents`, `sessions`, `log`, `ask` |

Чтение (`agent list/show`, `sessions`, `log`) открывает базу только если она есть, и не создаёт
данные; `provider list` и `doctor` — тоже. Запись (`init`, `agent add`, `ask`) создаёт корень и БД.
`agent add` не проверяет доступность провайдера: агента можно завести заранее, а `ask` честно
скажет, что провайдер недоступен.

## 8. UI — заморожено (граница с ядром)

UI импортирует только `runneos_agents` (Store + Orchestrator; с v1.7 — также
`policy.read_section/write_section` для экрана политики) и не пишет SQL и файлы
манифеста сам.
Экран v1: список агентов с состоянием → выбранный агент → список сессий → лента сообщений →
поле ввода и кнопка «Задача». Длительная задача выполняется в отдельном потоке
(`threading.Thread`), UI обновляется через `GLib.idle_add`; прогресс читается
`list_events(session_id, limit=...)` инкрементально по `seq`. Отмена — не в v1,
но кнопка становится неактивной во время прогона. Без ядра или без провайдера панель
деградирует честным экраном, а не падением.

Виджет DMS (`files/dms-plugin/`) данные берёт только из CLI (`agent list/show --json`,
`log --json`), в SQLite и сеть не ходит. Совпадение `plugin.json.id` и `pluginId` в QML
проверяется тестом.

## 9. Что не входит в первый срез (v1) и границы более поздних ревизий

- Межагентные каналы и передача задач между агентами.
- **Песочница v1.5–v1.6 — лимиты и файловая изоляция, но не сеть**: секция `sandbox`
  ограничивает ресурсы хода (MemoryMax/TasksMax/CPUQuota через `systemd-run --user --scope`)
  и по ключу `filesystem: "bwrap"` ставит корень только чтение с rw-воркспейсом и
  каталогами провайдера. Сеть остаётся общей (вызовы API), пользовательские каталоги вне
  списка `data_dirs()` доступны только чтение. Сервисный режим systemd — не используется
  (орфан-риск при таймауте); контроллер io в user-slice не делегирован — io-лимиты
  недоступны.
- **Принуждение политик — явно и осознанно** (семантика A, решение владельца 2026-10-13;
  v1.7 дал инструменты): ворота на каждом ходе пишут `policy.decision`; принуждение
  включается `agent policy ID --enforce on` (или переключателем в панели). Аудит действий
  агента **внутри** провайдера — область песочницы провайдера, воротами не покрывается.
- Расписания (routines), уведомления, несколько провайдеров одновременно.
- Собственный HTTP/socket-API: v1 работает через прямой импорт ядра.
- Подтверждённое поведение `opencode` с флагом `--auto` (автоодобрение прав) не проверялось;
  без него задача, требующая подтверждения, упрётся в таймаут.

## 10. Критерий готовности первого среза

1. `python -m unittest discover -s tests -p 'test_agents_*.py'` — зелёный.
2. `runneos-agents doctor` на этой машине: корень/БД/воркспейсы ок, codex — available.
3. `runneos-agents agent add tester --role '...'` создаёт агента и воркспейс.
4. `runneos-agents ask tester 'ответь ровно PONG'` — реальный прогон через codex CLI,
   в stdout `PONG`, в БД: `agent_message`, `usage`, `thread_id`.
5. Повторный `ask tester '...'` **продолжает** ту же сессию (`resume`), а не создаёт новую.
6. `runneos-agents ask tester ... --new` создаёт новую сессию.
7. Отсутствующий/недоступный провайдер: `ask` возвращает 1 с причиной, без трассировки.
8. `runneos-agents-panel` открывается, показывает агента, запускает задачу и показывает ответ.
9. Логи и БД не содержат секретов (проверяется тестом на фикстурах).
