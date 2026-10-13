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
