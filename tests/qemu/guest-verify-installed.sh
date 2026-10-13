#!/bin/bash
# Проверка свежеустановленной системы после первой загрузки.
#
# Запускается внутри установленной системы через последовательную консоль под
# тестовой учётной записью с sudo. Отвечает на вопросы, которые нельзя проверить
# из установщика: смонтировался ли fstab, отработал ли юнит snapper, что попало в
# failed, работает ли pacman с отключённым [runneos].
set -uo pipefail

log() { echo; echo "=== $* ==="; }

log "кто мы и что загрузилось"
id
uname -r
uptime

log "fstab и монтирования"
cat /etc/fstab
findmnt -R / | head -10

log "юнит snapper первого запуска"
systemctl status runneos-snapper-firstboot.service --no-pager 2>&1 | head -14
echo "--- конфигурации snapper ---"
snapper list-configs 2>&1
ls -ld /.snapshots 2>&1
systemctl is-enabled snapper-timeline.timer snapper-cleanup.timer 2>&1

log "failed units"
systemctl --failed --no-pager

log "цели systemd"
systemctl is-active graphical.target multi-user.target sddm 2>&1

log "пакеты RunneOS"
pacman -Q runneos-config runneos-desktop niri dms-shell ghostty limine sddm linux 2>&1

log "pacman с выключенным [runneos]"
grep -n -A4 runneos /etc/pacman.conf | tail -8
pacman -Sy --noconfirm 2>&1 | tail -3

log "отчёт постустановочного шага"
runneos-post-install --check 2>&1 | head -16

echo "=== INSTALLED SYSTEM CHECK DONE ==="
