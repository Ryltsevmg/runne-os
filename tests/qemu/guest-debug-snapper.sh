#!/bin/bash
# Debug the snapper first-boot hang on the installed target, from the live medium.
set -uo pipefail

log() { echo; echo "=== $* ==="; }

log "монтирование цели"
mkdir -p /mnt/archinstall
mount -o subvol=@ /dev/vda2 /mnt/archinstall
mkdir -p /mnt/archinstall/home /mnt/archinstall/boot
mount -o subvol=@home /dev/vda2 /mnt/archinstall/home
mount /dev/vda1 /mnt/archinstall/boot
findmnt -R /mnt/archinstall | head -4

log "snapper: версия и справка create-config"
chroot /mnt/archinstall snapper --version
chroot /mnt/archinstall snapper create-config --help 2>&1 | head -20

log "попытка 1: как в юните, со stdin из /dev/null, лимит 30 с"
timeout 30 chroot /mnt/archinstall snapper -c root create-config / < /dev/null
echo "rc=$?"

log "состояние после попытки 1"
ls -la /mnt/archinstall/etc/snapper/configs/ 2>&1
ls -d /mnt/archinstall/.snapshots 2>&1

log "попытка 2: если не создалось — в фоне и смотрим, где висит"
if [ ! -f /mnt/archinstall/etc/snapper/configs/root ]; then
	( chroot /mnt/archinstall snapper -c root create-config / < /dev/null > /tmp/snapper.out 2>&1 & echo $! > /tmp/snapper.pid )
	sleep 15
	pid=$(cat /tmp/snapper.pid)
	echo "--- процессы snapper ---"
	ps -eo pid,ppid,stat,etimes,cmd | grep -i snapper | grep -v grep
	echo "--- wchan/syscall ---"
	for p in $(pgrep -f "snapper -c root"); do
		echo "pid $p: wchan=$(cat /proc/$p/wchan 2>/dev/null) syscall=$(cat /proc/$p/syscall 2>/dev/null | cut -c1-60)"
	done
	echo "--- вывод snapper ---"
	cat /tmp/snapper.out 2>/dev/null | head -20
	echo "--- стек ядра (первые строки) ---"
	for p in $(pgrep -f "snapper -c root"); do
		sudo cat /proc/$p/stack 2>/dev/null | head -6
	done
	pkill -f "snapper -c root" 2>/dev/null
	echo "(остановлено)"
fi

log "итог"
ls -la /mnt/archinstall/etc/snapper/configs/ 2>&1
ls -d /mnt/archinstall/.snapshots 2>&1

echo "=== SNAPPER DEBUG DONE ==="
