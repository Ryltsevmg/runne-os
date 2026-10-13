#!/bin/bash
# Rehearsal for guest-install.sh: everything except the installation itself.
#
# Checks that partitioning, btrfs subvolumes, the pre-mounted layout and the
# archinstall test profile are accepted on this medium, so a failure in the real
# run cannot come from a typo in the disk steps.
set -uo pipefail

log() { echo; echo "=== $* ==="; }

log "диск: GPT + ESP + btrfs @/@home"
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

log "конфиг: профиль + pre_mounted_config"
python3 - <<'PY'
import json, pathlib
src = pathlib.Path('/usr/share/runneos/installer/user_configuration.json')
cfg = json.loads(src.read_text())
cfg['disk_config'] = {'config_type': 'pre_mounted_config', 'mountpoint': '/mnt/archinstall'}
cfg['auth_config'] = {'root_enc_password': '$6$runneostest$FIXTURE'}
cfg['silent'] = True
pathlib.Path('/root/test-config.json').write_text(json.dumps(cfg, indent=2))
print('ключей в профиле:', len(cfg), 'пакетов:', len(cfg['packages']))
PY

log "archinstall: разбор конфига и проверка раскладки (--dry_run)"
archinstall --config /root/test-config.json --silent --dry_run 2>&1 | tail -15
echo "dry-run rc=$?"

log "размонтирование"
umount -R /mnt/archinstall
echo "=== REHEARSAL DONE ==="
