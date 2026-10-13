# runneos-vicinae — сохранённый исходник, не часть рабочего стола

Vicinae — быстрый расширяемый лаунчер. Здесь лежит его сборка из upstream-исходника
(GPL3) с закреплёнными версией и коммитом.

## Статус: архивированный исходник

**На рабочей машине не установлен и desktop-метапакетом не требуется.** 2026-10-10
пользователь удалил Vicinae как дублирующий лаунчер: поиск и запуск приложений
выполняет DMS Spotlight. Компонент сохранён, потому что решение может измениться,
а воспроизводимая сборка из закреплённого коммита ценнее, чем повторный поиск
исходников.

По этой же причине пакет **исключён из сборки репозитория по умолчанию**:
`scripts/build-repo.sh` пропускает его и сообщает об этом в строке «пропущено: 1».

## Что проверено

- **Целостность исходника:** `sha256sum vicinae-v0.29.1.tar.gz` совпадает со
  значением `sha256sums` в PKGBUILD (`ca79782e…`).
- **Сборочная пригодность восстановлена.** 2026-10-11 пакет нельзя было собрать:
  `cmake` и `qt6-tools` из `makedepends` удалили при общей очистке, а
  `layer-shell-qt`, `libqalculate`, `qtkeychain-qt6` и `syntax-highlighting` из
  `depends` ушли как осиротевшие при удалении самого Vicinae. Первая попытка
  сборки упала на конфигурации CMake с `Could not find a package configuration
  file provided by "KF6SyntaxHighlighting"`.

  Это важная деталь для будущего: `depends` участвуют и в конфигурации CMake
  (`find_package`), поэтому «архивный» пакет без установленных зависимостей
  неразбираем, хотя исходник цел. `scripts/build-repo.sh` запускает `makepkg` с
  `--nodeps` и потому такую нехватку не подсказывает — проверять зависимости
  нужно вручную.
- **Сборка проверена 2026-10-11.** После установки недостающих зависимостей
  `makepkg -f --nodeps` завершилась успешно: `runneos-vicinae 0.29.1-3`, 14 МиБ,
  внутри `/usr/bin/vicinae` и три desktop-записи (`vicinae.desktop`,
  `vicinae-toggle.desktop`, `vicinae-url-handler.desktop`). Пакет **не
  устанавливался** — проверялась собираемость архива, а не возврат лаунчера.
  Debug-артефакт (201 МиБ) удалён, он воспроизводится при необходимости.
- Устаревшие артефакты прошлых сборок (`0.29.1-1/2/3` и debug-пакеты, около
  650 МиБ) удалены: каталог больше не выглядит как готовый пакет.

## Сборка

```bash
# нужны и makedepends, и depends — последние тоже используются на конфигурации:
# git cmake ninja npm jq glaze curl qt6-shadertools qt6-tools wayland-protocols
# cmark-gfm nodejs qt6-base qt6-svg qt6-declarative layer-shell-qt libqalculate
# qtkeychain-qt6 syntax-highlighting
sudo pacman -S --needed cmake qt6-tools layer-shell-qt libqalculate \
     qtkeychain-qt6 syntax-highlighting        # чего не хватало на 2026-10-11

cd packages/runneos-vicinae && makepkg -f      # только этот пакет
# или
./scripts/build-repo.sh --with-vicinae         # вместе с остальными, с публикацией
```

Сборка тяжёлая: Qt6 + C++ плюс фронтенд на npm, исходники распаковываются
примерно в 2 ГиБ, готовый пакет — около 650 МиБ. `makepkg` отказывается работать
под root, поэтому сборка выполняется от пользователя.

## Установка и откат

```bash
# установить (если решение о лаунчере изменится)
sudo pacman -U packages/runneos-vicinae/runneos-vicinae-*.pkg.tar.zst

# убрать
sudo pacman -Rns runneos-vicinae
```

Пакет объявляет `provides=(vicinae)` и `conflicts=(vicinae vicinae-bin)`, поэтому
не встанет рядом с другими сборками того же лаунчера. При возврате Vicinae не
забыть, что сочетания поиска в профиле Niri (`niri-bindings.kdl`) назначены на
DMS Spotlight — их придётся переназначать осознанно.
