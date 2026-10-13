#!/bin/bash
# Build the RunneOS ISO.
#
#   1. builds every local package and publishes out/repo/$arch as [runneos]
#   2. links that directory to /runneos-repo, the path iso/pacman.conf expects
#   3. runs mkarchiso against the iso/ profile
#
# Root is required for mkarchiso and for the /runneos-repo link; the package
# build itself runs as the invoking user, because makepkg refuses to run as
# root. Skip the package step with --no-repo when out/repo is already current.
#
# The work directory lives in the checkout, not in /tmp: on this workstation
# /tmp is a 6.7 GiB tmpfs, and the unpacked airootfs alone needs ~5 GiB, so
# mksquashfs dies with "No space left on device" even though the root
# filesystem has plenty of room. Override with ARCHISO_WORK_DIR if needed.
set -euo pipefail
cd "$(dirname "$0")/.."
root=$PWD
work_dir=${ARCHISO_WORK_DIR:-$root/work/archiso}

# The work directory is deleted before every build, so refuse to touch
# anything that is not inside the checkout's own work/ directory.
case "$work_dir" in
	"$root"/work/*) ;;
	*) echo "отказ: work_dir вне $root/work: $work_dir" >&2; exit 1 ;;
esac

build_repo=1
if [[ "${1:-}" == "--no-repo" ]]; then
	build_repo=0
fi

if (( build_repo )); then
	./scripts/build-repo.sh
fi

sudo ln -sfn "$root/out/repo" /runneos-repo
echo "/runneos-repo -> $root/out/repo"

# Stage the installer into the live medium so the image can actually install
# RunneOS. The files are copied from installer/ rather than kept as a second
# copy in the profile, so there is one source of truth. The destination is
# listed in .gitignore because it is generated.
installer_dest="$root/iso/airootfs/usr/share/runneos/installer"
rm -rf -- "$installer_dest"
mkdir -p "$installer_dest"
cp -a "$root/installer/user_configuration.json" "$root/installer/runneos-install" \
      "$root/installer/README.md" "$installer_dest/"
echo "установщик добавлен в образ: $installer_dest"

# The live medium has to resolve the runneos-* packages by itself. mkarchiso
# reads iso/pacman.conf only while building, and it never copies that file into
# the image: the live system keeps the stock pacman.conf from the pacman
# package. Without the two steps below the medium carries no [runneos] at all,
# `runneos-install` refuses to start, and archinstall cannot install the RunneOS
# packages it is asked for.
repo_dest="$root/iso/airootfs/runneos-repo"
rm -rf -- "$repo_dest"
mkdir -p "$repo_dest"
cp -a "$root/out/repo/." "$repo_dest/"
echo "репозиторий добавлен в образ: $repo_dest ($(du -sh "$repo_dest" | cut -f1))"

install -Dm644 "$root/iso/pacman.conf" "$root/iso/airootfs/etc/pacman.conf"
echo "конфигурация pacman для live-системы: iso/airootfs/etc/pacman.conf"

if [[ -e "$work_dir" ]]; then
	echo "удаляю прежний рабочий каталог: $work_dir"
	sudo rm -rf -- "$work_dir"
fi
mkdir -p "$work_dir"

sudo mkarchiso -v -w "$work_dir" -o out iso

# mkarchiso writes a checksum only for the squashfs inside the image. The file
# next to the ISO is what the install recipe and the verification steps use, and
# a stale one is worse than none: it makes a fresh image look corrupt.
newest=$(ls -t out/runneos-*-x86_64.iso | head -1)
name=$(basename "$newest")
sudo sh -c "cd out && sha256sum '$name' > '$name.sha256'"
echo "контрольная сумма: out/$name.sha256"
