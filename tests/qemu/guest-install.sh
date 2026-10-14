#!/bin/bash
# Runs inside the RunneOS live medium in QEMU and installs RunneOS onto /dev/vda.
#
# Uploaded over the serial console by vm.py and executed there: the live medium
# has no other channel. The disk is partitioned by hand because the RunneOS
# profile deliberately carries no disk layout, and archinstall is then pointed at
# the already-mounted target with a pre_mounted_config; that keeps the run
# headless while still exercising package resolution from the medium, the
# bootloader step and the runneos-post-install custom command.
set -uo pipefail

log() { echo; echo "=== $* ==="; }

# 2026-10-13: pacman's default five parallel downloads stall through QEMU
# user-mode networking — big files (linux, linux-firmware-*) fall under
# "<1 bytes/sec transferred the last 10 seconds" and pacstrap aborts, twice in
# a row. One stream plus no low-speed abort grinds through slirp reliably;
# the same files download fine from the host outside QEMU.
log "bench: однопоточные загрузки (slirp + параллельные крупные файлы = stall)"
sed -i 's/^#\?ParallelDownloads.*/ParallelDownloads = 1/' /etc/pacman.conf
sed -i '/^\[options\]/a DisableDownloadTimeout' /etc/pacman.conf
grep -E 'ParallelDownloads|DisableDownloadTimeout' /etc/pacman.conf || true

log "repo: sync from the medium"
pacman -Sy --noconfirm 2>&1 | tail -3
pacman -Si runneos-desktop | sed -n '1,6p'
ls -la /runneos-repo/x86_64/runneos.db

log "disk: GPT + ESP + btrfs @/@home"
wipefs -a /dev/vda
sgdisk --zap-all /dev/vda
sgdisk -n 1:0:+512M -t 1:ef00 -c 1:EFI /dev/vda
sgdisk -n 2:0:0 -t 2:8300 -c 2:runneos /dev/vda
partprobe /dev/vda
sleep 2
lsblk /dev/vda
mkfs.fat -F32 -n EFI /dev/vda1
mkfs.btrfs -f -L runneos /dev/vda2
mount /dev/vda2 /mnt
btrfs subvolume create /mnt/@
btrfs subvolume create /mnt/@home
umount /mnt
mkdir -p /mnt/archinstall
mount -o subvol=@,compress=zstd,noatime /dev/vda2 /mnt/archinstall
mkdir -p /mnt/archinstall/home /mnt/archinstall/boot
mount -o subvol=@home,compress=zstd,noatime /dev/vda2 /mnt/archinstall/home
mount /dev/vda1 /mnt/archinstall/boot
findmnt -R /mnt/archinstall

log "config: RunneOS profile + pre-mounted layout"
python3 - <<'PY'
import json, pathlib
src = pathlib.Path('/usr/share/runneos/installer/user_configuration.json')
cfg = json.loads(src.read_text())
cfg['disk_config'] = {'config_type': 'pre_mounted_config', 'mountpoint': '/mnt/archinstall'}
cfg['auth_config'] = {'root_enc_password': '@@ROOT_HASH@@'}
cfg['users'] = [{'username': 'runneos', 'enc_password': '@@USER_HASH@@', 'sudo': True, 'groups': []}]
cfg['silent'] = True
pathlib.Path('/root/test-config.json').write_text(json.dumps(cfg, indent=2))
print('disk_config:', cfg['disk_config'])
print('packages in profile:', len(cfg['packages']))
print('custom_commands:', cfg.get('custom_commands'))
PY

log "installer wrapper: guard from the medium"
grep -n '^\[runneos\]' /etc/pacman.conf
/usr/share/runneos/installer/runneos-install --help >/dev/null && echo "wrapper: help ok"

log "archinstall (silent, pre-mounted target)"
archinstall --config /root/test-config.json --silent
rc=$?
echo "archinstall rc=$rc"

log "target: [runneos] in /etc/pacman.conf"
grep -n -A4 -B1 'runneos' /mnt/archinstall/etc/pacman.conf || echo "(раздела [runneos] нет)"

log "target: installed runneos packages"
ls /mnt/archinstall/var/lib/pacman/local/ | grep '^runneos-' || echo "(нет)"
ls /mnt/archinstall/var/lib/pacman/local/ | grep -c '^runneos-' || true

log "target: display manager and snapshots"
readlink -f /mnt/archinstall/etc/systemd/system/display-manager.service || true
ls /mnt/archinstall/etc/snapper/configs/ 2>/dev/null || echo "(snapper configs нет)"
ls -d /mnt/archinstall/etc/skel/.config/niri 2>/dev/null || echo "(skel niri нет)"

log "target: fstab и юнит snapper первого запуска"
echo "--- /etc/fstab ---"
cat /mnt/archinstall/etc/fstab
echo "--- fstab без комментариев: $(grep -cvE '^\s*(#|$)' /mnt/archinstall/etc/fstab) строк ---"
ls -la /mnt/archinstall/usr/lib/systemd/system/runneos-snapper-firstboot.service 2>&1
systemctl --root=/mnt/archinstall is-enabled runneos-snapper-firstboot.service 2>&1
systemctl --root=/mnt/archinstall is-enabled snapper-timeline.timer snapper-cleanup.timer 2>&1

log "target: bootloader"
ls -la /mnt/archinstall/boot/ 2>/dev/null | head -20
ls -la /mnt/archinstall/boot/EFI/ 2>/dev/null

log "target: swap/zram"
cat /mnt/archinstall/etc/systemd/zram-generator.conf 2>/dev/null || echo "(zram-generator.conf нет)"

log "условие ноутбука: раздел от live-носителя + имя сервера не разрешается"
python3 - <<'PY'
import pathlib, re
conf = pathlib.Path('/mnt/archinstall/etc/pacman.conf')
text = conf.read_text()
# The signed-repository profile leaves Required DatabaseOptional on the live
# medium; the stanza must still be removed cleanly when the server is gone.
body = '[runneos]\nSigLevel = Required DatabaseOptional\nServer = file:///runneos-repo/$arch\n'
conf.write_text(re.sub(r'(?:#)?\[runneos\][^\[]*', body, text, count=1))
print('раздел возвращён к виду, который оставляет live-носитель')
PY
grep -n -A3 'runneos' /mnt/archinstall/etc/pacman.conf | tail -6
arch-chroot /mnt/archinstall env RUNNEOS_REPO_SERVER=https://repo-does-not-resolve.invalid/runneos \
	runneos-post-install --apply
echo "--- состояние раздела после постустановочного шага ---"
grep -n -A5 'runneos' /mnt/archinstall/etc/pacman.conf | tail -10

# Test-only tweak: the installed system has no serial console of its own, and the
# graphics stack does not come up in QEMU. Adding console=ttyS0 to the Limine
# command line makes the booted system observable and loggable from the host; it
# changes nothing else about the installation.
log "тестовая правка: последовательная консоль в Limine установленной системы"
conf=$(find /mnt/archinstall/boot -name 'limine.conf' 2>/dev/null | head -1)
if [[ -z "${conf:-}" ]]; then
  echo "=== !!! СТЕНД: limine.conf не найден — serial установленной системы будет недоступен"
else
  echo "=== limine.conf найден: $conf"
  cp -a "$conf" "$conf.testbak"
  python3 - "$conf" <<'PY'
import pathlib, re, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
new = re.sub(r'(?m)^(\s*\S*cmdline:[^\n]*)$', lambda m: m.group(1) + ' console=ttyS0,115200', text)
# Меню Limine не должно ждать вечно, а serial нужен для наблюдаемости стенда.
if re.search(r'(?m)^\s*timeout:', new):
    new = re.sub(r'(?m)^(\s*timeout:).*$', lambda m: m.group(1) + ' 3', new)
else:
    new = 'timeout: 3\n' + new
if not re.search(r'(?m)^\s*serial:', new):
    new = 'serial: yes\n' + new
path.write_text(new)
edited = sum(1 for a, b in zip(text.splitlines(), new.splitlines()) if a != b)
print(f"=== строк limine.conf изменено: {edited}")
if edited == 0:
    print("=== !!! СТЕНД: ни одна строка не изменена")
PY
fi

# Second test-only tweak: archinstall installs Limine to \EFI\arch-limine and
# relies on the UEFI boot entry it creates with efibootmgr. The test boots the
# disk with a fresh QEMU, so the same loader is also placed on the removable
# fallback path \EFI\BOOT, which OVMF always tries. Nothing else changes.
log "тестовая правка: запасной путь загрузки \\EFI\\BOOT"
efi=/mnt/archinstall/boot/EFI
limdir=$(dirname "${conf:-/mnt/archinstall/boot/EFI/arch-limine/limine.conf}")
mkdir -p "$efi/BOOT"
if [[ -f "$limdir/BOOTX64.EFI" ]]; then
  cp -a "$limdir/BOOTX64.EFI" "$efi/BOOT/BOOTX64.EFI" && echo "=== BOOTX64.EFI скопирован"
  cp -a "$conf" "$efi/BOOT/limine.conf" && echo "=== limine.conf скопирован"
else
  echo "=== !!! СТЕНД: каталог Limine не найден: $limdir"
fi
ls -la "$efi/BOOT/" 2>/dev/null

sync
echo "INSTALL-RC=$rc"
echo "=== GUEST SCRIPT DONE ==="
