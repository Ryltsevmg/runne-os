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

log "агенты RunneOS: пакеты и версия"
pacman -Q runneos-agents runneos-agents-firstboot runneos-agents-scheduler runneos-ai-providers 2>&1
runneos-agents --version 2>&1
runneos-agents-firstboot --version 2>&1
runneos-agents-scheduler --version 2>&1

log "агенты: провайдеры и состояние входа (в ВМ без логина — честное «не авторизован»)"
runneos-agents provider list 2>&1
runneos-agents provider auth-status 2>&1

log "агенты: heboot-онбординг и планировщик без дисплея (smoke-test)"
runneos-agents-firstboot --smoke-test 2>&1
runneos-agents-scheduler --smoke-test 2>&1

log "агенты: юниты онбординга в сеансе пользователя"
# Скрипт идёт под sudo (root): его user-менеджера нет — смотрим сеанс пользователя
# через --machine, как runneos-doctor.
systemctl --user --machine=runneos@.host is-enabled runneos-agents-firstboot.service 2>&1
ls /usr/lib/systemd/user/runneos-agents-firstboot.service \
   /etc/systemd/user/graphical-session.target.wants/runneos-agents-firstboot.service \
   /usr/lib/systemd/user/runneos-agents-scheduler.service \
   /usr/lib/systemd/user/runneos-agents-scheduler.timer 2>&1
systemctl --user list-unit-files 2>/dev/null | grep runneos-agents

log "агенты: локальный API — сервер, ping, остановка"
timeout 30 runneos-agents serve --socket /tmp/agents-api.sock >/tmp/serve.log 2>&1 &
SERVE_PID=$!
sleep 3
python3 - <<'PY'
import json, socket
client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.settimeout(15)
client.connect("/tmp/agents-api.sock")
file = client.makefile("rw", encoding="utf-8", newline="\n")
file.write(json.dumps({"op": "ping"}) + "\n")
file.flush()
print("API ping:", json.loads(file.readline()))
client.close()
PY
# Фоновые задачи игнорируют SIGINT (POSIX), гасим TERM: файл сокета останется,
# следующий старт убирает его сам (самоисцеление проверено отдельно).
kill -TERM $SERVE_PID 2>/dev/null
sleep 1
[ -e /tmp/agents-api.sock ] && echo "сокет остался после TERM — самоуберётся при следующем старте" || echo "сокет убран"

echo "=== INSTALLED SYSTEM CHECK DONE ==="
