# runneos-deepseek-harness (Tauri shell)

Нативная оболочка RunneOS для DeepSeek Harness (dsh): пользовательская служба
dsh остаётся как есть (`~/.config/systemd/user/runneos-deepseek-harness.service`,
127.0.0.1:3080), оболочка заменяет только окно — вместо flatpak Chrome `--app`
это окно Tauri на системном WebKitGTK.

## Контракт (совпадает с прежним bash-планом)

- старт службы dsh по требованию, ожидание порта;
- первый запуск — токен-URL из журнала юнита (pairing-cookie webkit переживает
  рестарты сервера), далее обычный URL; маркер спаренности:
  `~/.local/state/runneos/deepseek-harness/paired`;
- `--pair` — принудительная перепривязка;
- повторный запуск фокусирует существующее окно (app_id
  `runneos-deepseek-harness` — имя бинарника; Wayland app_id tao берёт из
  него, а не из identifier tauri.conf.json);
- ошибки — через notify-send.

## Сборка и установка (machine scope)

```sh
pacman -S --needed webkit2gtk-4.1          # системная зависимость
cargo install tauri-cli --version '^2' --locked
cd src-tauri && cargo tauri build --no-bundle
install -m755 target/release/runneos-deepseek-harness ~/.local/bin/
```

Desktop-запись `org.runneos.DeepSeekHarness.desktop` (Exec — абсолютный путь
в `~/.local/bin`) и иконка hicolor уже установлены на машине; `StartupWMClass`
должен совпадать с фактическим app_id окна (проверять `niri msg windows`).

## Удалённый режим: сессии другой машины (2026-10-14)

`--remote <ssh-host>[,<запасной>...]` открывает UI dsh, запущенного на другой
машине, в отдельном окне. Добавлено 2026-10-14 для задачи «работать и на
ноутбуке, и на bigpc».

- Оболочка поднимает `ssh -N -L 127.0.0.2:3080:127.0.0.1:3080 <host>` и
  открывает `http://127.0.0.2:3080/`. Сервер dsh на той стороне остаётся
  привязанным к своему loopback — в сеть ничего не выставляется.
- **Почему 127.0.0.2, а не 127.0.0.1:3081.** Cookie в WebKit общие на
  приложение и не различаются по порту: `127.0.0.1:3080` (локально) и
  `127.0.0.1:3081` (туннель) делили бы одну сессионную cookie, и окна
  затирали бы пейринг друг друга. Второй loopback-адрес даёт отдельную банку
  cookie на каждую машину.
- app_id берётся из имени бинарника (`std::env::current_exe`), поэтому для
  второго окна нужна копия бинарника под другим именем — иначе повторный
  запуск счёл бы окно локальным и просто сфокусировал его.
- Пейринг — как локально, но токен читается из журнала удалённого юнита по
  ssh; маркер отдельный: `~/.local/state/runneos/deepseek-harness/paired-<host>`.
- Туннель живёт ровно столько, сколько окно: процесс ssh убивается при выходе.
- Кандидаты перебираются по порядку, поэтому `--remote bigpc-lan,bigpc` даёт
  работу по LAN дома и по tailnet вне дома (для tailnet нужен один раз
  подтвердить Tailscale SSH check в браузере).

Установка второго окна:

```sh
install -m755 target/release/runneos-deepseek-harness ~/.local/bin/runneos-deepseek-harness
install -m755 target/release/runneos-deepseek-harness ~/.local/bin/runneos-deepseek-harness-bigpc
# desktop-запись: org.runneos.DeepSeekHarness.bigpc.desktop
#   Exec=.../runneos-deepseek-harness-bigpc --remote bigpc-lan,bigpc
#   StartupWMClass=runneos-deepseek-harness-bigpc
```

Проверено 2026-10-14 на ноутбуке против bigpc: туннель поднимается, окно
`runneos-deepseek-harness-bigpc` открывается отдельно от локального,
WebKit держит соединение через ssh, на bigpc видны подключения к его dsh,
`gio launch` записи фокусирует окно без дубля.

## Проверка

1. `runneos-deepseek-harness` — окно «DeepSeek Harness», app_id
   `runneos-deepseek-harness`.
2. Повторный запуск и `gio launch` записи — фокус существующего окна, дубля нет.
3. `systemctl --user restart runneos-deepseek-harness.service` (токен
   ротируется), перезапуск оболочки — UI грузится по обычному URL без 401.
4. 401 в окне (чистка данных webkit) — лечится `runneos-deepseek-harness --pair`.

Все четыре пункта проверены 2026-10-11 на машине (см.
`docs/results-deepseek-harness-2026-10-11.md`).

## Откат на bash-обёртку

`wrapper-legacy.sh` в этом каталоге — прежняя Chrome-версия обёртки:

```sh
install -m755 wrapper-legacy.sh ~/.local/bin/runneos-deepseek-harness
```

и вернуть `StartupWMClass=chrome-127.0.0.1__-Default` в desktop-записи.

## Границы

- Machine scope: в ISO не входит; dsh и служба DeepSeek — сторонние (MIT-код,
  чужой бренд/сервис), как Pen.
- Cookie webkit хранятся в `~/.local/share/org.runneos.DeepSeekHarness`
  (identifier); их чистка = повторный `--pair`.
- Сессии dsh раздельные по браузерным cookie: сессия из Chrome-окна в
  webkit-окне не видна и наоборот.
