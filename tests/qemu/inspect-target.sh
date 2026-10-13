#!/bin/bash
# Mount the installed RunneOS disk image on the host and report what landed there.
#
# The install itself runs inside QEMU; this script answers the questions that
# matter for a real machine by reading the target filesystem directly: does
# pacman still point at the live medium, are the runneos-* packages installed,
# is a display manager enabled, does the disk have the expected layout.
set -uo pipefail

work=${RUNNEOS_QEMU_WORK:-$HOME/.cache/runneos-qemu}
disk=$work/disk.img
mountpoint=$work/target

[[ -f $disk ]] || { echo "нет $disk" >&2; exit 1; }

loop=$(sudo losetup -Pf --show "$disk") || exit 1
echo "loop-устройство: $loop"
lsblk "$loop"

cleanup() {
	sudo umount -R "$mountpoint" 2>/dev/null
	sudo losetup -d "$loop" 2>/dev/null
}
trap cleanup EXIT

sudo mkdir -p "$mountpoint"
sudo mount -o ro,subvol=@ "$loop"p2 "$mountpoint" || exit 1
sudo mount -o ro,subvol=@home "$loop"p2 "$mountpoint/home" 2>/dev/null
sudo mount -o ro "$loop"p1 "$mountpoint/boot" 2>/dev/null

echo
echo "=== btrfs: подтома и снимки ==="
sudo btrfs subvolume list "$mountpoint" 2>/dev/null || echo "(не btrfs)"

echo
echo "=== /etc/pacman.conf: раздел [runneos] ==="
grep -n -B2 -A5 'runneos' "$mountpoint/etc/pacman.conf" || echo "(раздела нет)"

echo
echo "=== установленные пакеты runneos-* ==="
ls "$mountpoint/var/lib/pacman/local/" | grep '^runneos-' || echo "(нет)"
echo "всего пакетов: $(ls "$mountpoint/var/lib/pacman/local/" | wc -l)"

echo
echo "=== ключевые пакеты ==="
for pkg in niri dms-shell quickshell ghostty sddm limine linux runneos-config snapper snap-pac; do
	name=$(ls "$mountpoint/var/lib/pacman/local/" | grep "^${pkg}-[0-9]" | head -1)
	echo "  ${name:-$pkg: НЕ УСТАНОВЛЕН}"
done

echo
echo "=== дисплей-менеджер и службы ==="
readlink -f "$mountpoint/etc/systemd/system/display-manager.service" || echo "(DM не выбран)"
ls "$mountpoint/etc/systemd/system/multi-user.target.wants/" 2>/dev/null | head -20

echo
echo "=== снимки snapper ==="
ls "$mountpoint/etc/snapper/configs/" 2>/dev/null || echo "(конфигурации нет)"
systemctl --root="$mountpoint" is-enabled snapper-timeline.timer 2>/dev/null || true

echo
echo "=== профиль новых пользователей /etc/skel ==="
find "$mountpoint/etc/skel" -maxdepth 3 2>/dev/null | head -20

echo
echo "=== загрузчик ==="
ls -la "$mountpoint/boot/" 2>/dev/null | head -20
ls -la "$mountpoint/boot/EFI/" 2>/dev/null
find "$mountpoint/boot" -iname "*limine*" -o -iname "*.conf" 2>/dev/null | head -10

echo
echo "=== fstab ==="
cat "$mountpoint/etc/fstab" 2>/dev/null

echo
echo "=== zram, локаль, часовой пояс, хостname ==="
cat "$mountpoint/etc/systemd/zram-generator.conf" 2>/dev/null || echo "(zram-generator.conf нет)"
grep -v '^#' "$mountpoint/etc/locale.conf" 2>/dev/null
cat "$mountpoint/etc/hostname" 2>/dev/null
readlink -f "$mountpoint/etc/localtime" 2>/dev/null

echo
echo "=== лог archinstall в целевой системе ==="
ls -la "$mountpoint/var/log/archinstall/" 2>/dev/null || echo "(лога нет)"

echo
echo "=== журнал post-install (если сохранён) ==="
grep -rn "RunneOS post-install" "$mountpoint/var/log/" 2>/dev/null | head -5 || true

echo
echo "Проверка завершена."
