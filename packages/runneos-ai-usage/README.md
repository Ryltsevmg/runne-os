# AI Usage в RunneOS

## Текущая версия 0.2 — AI-центр и виджет

Запуск: `runneos-ai`, меню **AI · RunneOS**. Новый сборщик — `runneos-ai-collect`; DMS использует его, а не legacy collector ниже. Пакет включает GTK4/libadwaita UI подключения, удаления/замены ключа, pins и выбора периода 7/30/90 дней. API-ключи хранятся в GNOME Keyring по закрытой схеме `org.runneos.AIAccount.v1`; в `~/.config/runneos/ai-accounts.json` только product/account IDs и безопасные ссылки/параметры. Фоновое чтение не разблокирует keyring автоматически и не показывает парольные диалоги.

**Codex:** текущий аккаунт штатного Codex CLI обнаруживается автоматически, в том числе с авторизацией через keyring. Лимиты читаются через documented `account/read` и `account/rateLimits/read` app-server по stdio. Threads/turns/model requests не создаются. Пароли/OAuth tokens не копируются. Название окна определяется `windowDurationMins`, недельный лимит не называется пятичасовым. Изменение/недоступность подтверждения account identity не позволяет молча показывать квоты другого аккаунта.

**Другие продукты:** DeepSeek API, OpenRouter key budget, Z.ai Coding, Kimi Code, Moonshot API, x.ai team management, Alibaba CLI profile. Каждый отдельный account имеет UUID. Claude subscription использует доступные данные штатного helper; если источника/квот нет, UI сообщает ограничение. OpenCode Go/OpenAI API/Anthropic API можно добавить как продукты с локальной историей и ссылкой в кабинет, без обещания API-баланса. Реальные аккаунты этих сервисов ещё не проверены в v2; ввод credentials и сверка с кабинетом выполняются пользователем.

Новый cache: `~/.local/state/runneos/ai-usage/accounts-v2.json`, 0600. Он не содержит ключей, email или сырых ответов. Opaque HMAC binding активного Codex использует локальную случайную соль 0600. Polling не чаще 180 секунд, ручные запросы дедуплицируются на 10 секунд, действует process lock. Ошибки отдельных API сохраняют last-known-good и возраст; 429 учитывает Retry-After. Если личность текущего Codex не подтверждается, старые квоты не отображаются как его данные. Local OpenCode и CLI history раздельные; их нельзя складывать в общий расход.

`runneos-ai --smoke-test` проверяет реальную загрузку GTK/данных и построение формы подключения без ввода ключа. Unit tests проверяют парсеры, cache identity, ошибки и private account config. Автоматический keyring acceptance использует только одноразовый не секретный marker и удаляет его. Синхронизация Bitwarden не является механизмом фонового чтения credentials.

После установки обновить DMS overlay через актуальный `scripts/install-dms-plugins.py --skip-download` (если pinned plugins уже установлены). Перезагрузка ОС/сеанса не нужна. Существующие настройки и другие плагины сохраняются. Кнопка виджета открывает сводку; правый клик и кнопка «Аккаунты и подробности» открывают GTK AI-центр.

### Legacy 0.1 compatibility

Оставшийся `runneos-ai-usage` — прежний collector с файлом `ai-usage.json` для совместимости/диагностики. Ниже — его источники и формат. Новый UI не импортирует ключи из чужой БД или legacy файлов автоматически. Девять постоянных карточек относятся к старому UI, новый показывает только реальные подключённые продукты.

Расширение DankAIUsage для Codex, Claude, Z.ai, x.ai, Kimi, DeepSeek, Qwen, OpenCode Go и OpenRouter. Все девять карточек видны всегда. Неподключённый сервис показывает статус, неизвестные показатели не заменяются нулём. Это мониторинг: запросы моделей, изменение лимитов и автоматический сброс сессий не выполняются.

| Сервис | Данные и источник |
|---|---|
| Codex, Claude | Существующий helper DankAIUsage: доступные лимиты аккаунта и локальная история CLI. Для Claude нужен авторизованный Claude CLI. |
| Z.ai | Coding Plan: процент использования окон, reset только если возвращён API. Обычный API-ключ без Coding Plan может не иметь этих данных. |
| x.ai | Предоплаченный баланс команды, нужен отдельный Management API Key и Team ID. Ключ inference не предоставляет эти права. |
| Kimi | Баланс Moonshot API **или** лимиты Kimi Code (`mode: coding`); это отдельные продукты. |
| DeepSeek | Баланс API; USD и CNY отображаются отдельно. |
| Qwen | Локальный OpenCode; дополнительно баланс всего Alibaba Cloud через профиль Alibaba CLI, а не отдельный лимит Qwen. |
| OpenCode Go | Локальный OpenCode и ссылка в кабинет. Публичный API остатка подписки не подтверждён. OpenCode Zen не считается Go. |
| OpenRouter | Расход ключа за месяц и остаток бюджета ключа, когда бюджет задан. Это не баланс аккаунта. |

Все сервисы могут отображать локальные токены и стоимость, сообщённую OpenCode за 7/30/90 дней. Модель, вызванная через Go или OpenRouter, учитывается у оплачивающего провайдера. Эта история охватывает только компьютер и выбранный период; стоимость подписки не вычисляется из стоимости токенов. История CLI и OpenCode показывается с указанием источника, а не складывается в универсальный счётчик.

## Подключение

Сначала используйте существующие подключения OpenCode (`/connect`). Поддержаны API credentials старого `auth.json` и активные API credentials OpenCode 2 SQLite. OAuth OpenAI не используется как ключ другого API. Codex/Claude читаются штатным helper. Для Kimi Code 2 сохранённая интеграция `kimi-for-coding`/`kimi-coding` выбирает coding автоматически; для старого auth.json укажите mode явно.

Для отдельного подключения создайте `~/.config/runneos/ai-usage.json`. В нём храните пути, не ключи. Ключи — отдельные файлы с правами **0600**. Пример структуры (включайте только реально настроенные сервисы):

```json
{
  "refreshInterval": 300,
  "deepseek": {"apiKeyFile": "~/.config/runneos/keys/deepseek"},
  "openrouter": {"apiKeyFile": "~/.config/runneos/keys/openrouter"},
  "zai": {"apiKeyFile": "~/.config/runneos/keys/zai", "region": "international"},
  "kimi": {"apiKeyFile": "~/.config/runneos/keys/kimi", "mode": "coding", "region": "international"},
  "xai": {"managementKeyFile": "~/.config/runneos/keys/xai-management", "teamId": "YOUR_TEAM_ID"},
  "qwen": {"aliyunProfile": "YOUR_PROFILE"}
}
```

Kimi Moonshot: `mode: api`, region international для moonshot.ai, china для moonshot.cn. Для Z.ai китайский endpoint — region china. Qwen: предварительно установить и настроить Alibaba CLI с разрешением `bss:QueryAccountBalance`; DashScope API key сам по себе не даёт доступа к балансу Alibaba.

Также поддержаны переменные окружения ZAI_API_KEY, XAI_API_KEY, XAI_MANAGEMENT_API_KEY, MOONSHOT_API_KEY, KIMI_API_KEY (Coding), DEEPSEEK_API_KEY, DASHSCOPE_API_KEY, OPENCODE_GO_API_KEY, OPENROUTER_API_KEY. У пользовательской службы DMS окружение отличается от терминала; файлы подключения работают без перезапуска оболочки. Не передавайте ключи в аргументах команд и не сохраняйте их в репозитории.

Настройки виджета: провайдер в панели и период локальной истории. Кнопка «Обновить» принудительно обновляет данные; штатный опрос раз в 5 минут, минимум 3 минуты. Нормализованный cache с правами 0600 находится в `~/.local/state/runneos/ai-usage/`. Тела ответов, API-ключи, имена файлов истории и model IDs в него не записываются. Ошибка одного провайдера не скрывает остальные.

## Установка и проверка

```sh
cd packages/runneos-ai-usage
makepkg
sudo pacman -U ./runneos-ai-usage-0.1.0-1-*.pkg.tar.zst
# Из корня репозитория:
python scripts/install-dms-plugins.py
dms ipc call runneos-ai-usage status
```

Установщик накладывает QML-расширение поверх закреплённого DankAIUsage. После upstream update повторите установщик: локальное расширение не является частью upstream. Проверки адаптеров используют фикстуры; реальные балансы требуют соответствующих подключений. ISO этой установкой не изменяется.

Источники: [DankAIUsage](https://github.com/alcxyz/DankAIUsage), [DeepSeek balance](https://api-docs.deepseek.com/api/get-user-balance/), [OpenRouter key](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key), [Z.ai usage helper](https://github.com/zai-org/zai-coding-plugins/blob/main/plugins/glm-plan-usage/skills/usage-query-skill/scripts/query-usage.mjs), [Kimi CLI usage](https://github.com/MoonshotAI/kimi-cli/blob/main/src/kimi_cli/ui/shell/usage.py), [x.ai billing](https://docs.x.ai/developers/rest-api-reference/management/billing), [Alibaba account balance](https://www.alibabacloud.com/help/en/user-center/developer-reference/api-bssopenapi-2017-12-14-queryaccountbalance), [OpenCode Go](https://docs.opencode.ai/docs/go/).
