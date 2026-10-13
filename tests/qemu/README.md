# Стенд установки RunneOS в QEMU

Проверяет то, что нельзя проверить в исходниках: загружается ли носитель, разрешаются ли на
нём пакеты `runneos-*`, проходит ли установку `archinstall` и что остаётся на целевом диске.
Стенд появился 2026-10-12 вместе с первой сквозной установкой
([протокол](../../docs/results-install-2026-10-12.md)).

## Что внутри

| Файл | Роль |
|---|---|
| `vm.py` | драйвер QEMU и последовательной консоли: `extract`, `start-live`, `install`, `shell`, `run-script`, `boot-installed`, `shot`, `stop` |
| `guest-install.sh` | выполняется в live-системе: разметка пустого диска, профиль `pre_mounted_config`, установка, проверки, две тестовые правки (см. ниже) |
| `guest-verify-installed.sh` | выполняется в установленной системе после первой загрузки: fstab, юнит снимков, failed units, цели systemd, пакеты, pacman, отчёт `runneos-post-install --check` |
| `guest-disk-rehearsal.sh` | только разметка и разбор конфига (`--dry_run`), без установки |
| `guest-debug-snapper.sh` | разбирает, почему `snapper create-config` не работает из chroot (подтом и шина DBus) |
| `run-full-test.sh` | полный прогон: `extract` → `start-live` → `install` → `stop` |
| `inspect-target.sh` | монтирует образ диска на хосте и печатает, что попало в целевую систему |
| `sandbox-post-install.sh` | проверяет шаг репозитория в `runneos-post-install` на поддельном `pacman.conf`, без установки |

## Требования

- `qemu-system-x86` и `edk2-ovmf` (стенд грузит UEFI: `archinstall` ставит Limine через
  `efibootmgr` и без `/sys/firmware/efi` падает), `squashfs-tools`, `xorriso`, `bsdtar`,
  `imagemagick`, `/dev/kvm`.
- Права root не нужны: QEMU работает от пользователя, `/dev/kvm` доступен на запись группе.
- Свободное место под рабочий каталог `${RUNNEOS_QEMU_WORK:-~/.cache/runneos-qemu}`: образ
  диска — разреженный файл на 30 ГиБ, реально занимает установленная система. С 2026-10-13
  `run-full-test.sh` создаёт диск сам, если его нет (`truncate`, qemu-img не требуется).

## Как запускать

```bash
tests/qemu/run-full-test.sh                 # свежий ISO из out/ выбирается автоматически
RUNNEOS_ISO=out/runneos-2026.10.12-x86_64.iso tests/qemu/run-full-test.sh
python3 tests/qemu/vm.py boot-installed     # загрузить установленную систему
python3 tests/qemu/vm.py run-script --user runneos --password runneos --sudo \
  --remote /tmp/check.sh guest-verify-installed.sh   # опции после подкоманды
python3 tests/qemu/inspect-target.sh        # посмотреть, что на диске
```

Прогон идёт в фоне и пишет полный журнал консоли в
`${RUNNEOS_QEMU_WORK:-~/.cache/runneos-qemu}/serial.log`; в stdout попадают только маркеры
шагов (полный вывод — с `--verbose`).

## Что стенд меняет в проверяемых системах

Живая установка идёт по конфигу с `pre_mounted_config`: разметку делает сам скрипт стенда,
потому что интерактивное меню `archinstall` headless не провести. Поэтому шаги «выбрать диск»
и «создать пользователя» остаются непроверенными — их проверяет человек.

Две правки делаются только ради наблюдаемости и к установке отношения не имеют:

1. в Limine установленной системы добавляется `console=ttyS0,115200`, иначе у неё нет
   последовательной консоли, а графика в QEMU не поднимается (известное ограничение);
2. Limine дублируется в `\EFI\BOOT`, потому что загрузочная запись UEFI живёт в NVRAM, а
   стенд запускает новую ВМ.

Обе правки видны в `guest-install.sh` отдельными шагами с пометкой «тестовая правка».
