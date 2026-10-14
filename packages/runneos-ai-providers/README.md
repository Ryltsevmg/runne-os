# runneos-ai-providers

Запиненные CLI провайдеров ИИ для `runneos-agents` и `runneos-ai-usage`. Ставит
`/usr/bin/codex` и `/usr/bin/opencode`; агенты находят их по PATH. Провайдеры
считаются доступными сразу после установки; авторизация (вход в аккаунт или API-ключ)
— отдельное действие пользователя, агенты честно сообщают «недоступен», пока её нет.

## Что внутри (пин и происхождение)

| CLI | Версия | Источник | Хеш | Лицензия |
|---|---|---|---|---|
| codex | 0.162.1 | GitHub release `rust-v0.162.1`, ассет `codex-x86_64-unknown-linux-musl.tar.gz` (статический musl-бинарник) | sha256 `86f268d81b898f3e144c5ecff2ad2fda2c5802e042d6195b72538fe0fa125057` (сверен с digest из GitHub API) | Apache-2.0 |
| opencode | 2.0.26 | npm `@opencode/cli-linux-x64`, tarball `cli-linux-x64-2.0.26.tgz` (внутри `package/bin/opencode`) | sha256 `0a116e033a02804741d4644337d012bdf5a24aac33a34c0c96534d2d53961119`; npm integrity sha512 `UIA2/1Ik8HaN54C4xp7H+OHgRfq995XUixrX+kLmFs8EPn1fAcmn3OoiN+3c3Vc3jEV/LHmj0Cy1SL6nW4wdGw==` (сверена с реестром) | MIT |

Полный provenance: `provenance.json` рядом с PKGBUILD.

## Обновление пина

1. Новую версию codex взять из GitHub releases (digest ассета из API — обязательная
   сверка), opencode — из npm-метаданных (`dist.integrity`).
2. Обновить `source`/`sha256sums` в PKGBUILD, таблицу и `provenance.json`.
3. Прогнать локально `makepkg -f` (check() сверяет `--version`), затем полный набор
   тестов агентов: адаптеры снимают фикстуры с реальных CLI — при смене версии
   сверить формат вывода (`tests/test_agents_adapters.py`).
4. Поднять `pkgrel`, пересобрать репозиторий, зафиксировать в справке агентов.

## Границы

- Авторизация провайдеров не входит в пакет: `codex login` / ключ opencode —
  действие пользователя (срез онбординга — отдельно).
- Пакет не содержит ключей и не читает чужие каталоги провайдеров (`~/.codex`,
  `~/.opencode` остаются у их владельцев).
- Опкод-бинарник велик (~100+ МиБ сжатым): ISO дорожает соответственно — это
  осознанная цена «агенты из коробки».
