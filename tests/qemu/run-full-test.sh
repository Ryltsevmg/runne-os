#!/bin/bash
# Full end-to-end install test: boot the freshly built medium, partition an empty
# disk, install RunneOS with archinstall, and report what landed on the target.
#
# Run after ./scripts/build.sh finished. Override the artefact with RUNNEOS_ISO.
set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd)
cd "$here"

repo=$(cd "$here/../.." && pwd)
if [[ -z ${RUNNEOS_ISO:-} ]]; then
	RUNNEOS_ISO=$(ls -t "$repo"/out/runneos-*-x86_64.iso | head -1)
	export RUNNEOS_ISO
fi
echo "=== артефакт ==="
ls -la "$RUNNEOS_ISO"

# Диск для целевой системы: разреженный файл на 30 ГиБ, реально занимает
# установленная система. Стенд предполагал, что disk.img уже лежит в рабочем
# каталоге с прошлого прогона — на чистой машине это не так.
work=${RUNNEOS_QEMU_WORK:-$HOME/.cache/runneos-qemu}
if [[ ! -e "$work/disk.img" ]]; then
	mkdir -p "$work"
	echo "=== создаю пустой диск: $work/disk.img (30G, разреженный) ==="
	# truncate вместо qemu-img: тот живёт в qemu-tools, который не нужен
	# самому стенду; для raw-образа результат идентичен.
	truncate -s 30G "$work/disk.img"
fi

python3 vm.py stop 2>/dev/null || true
python3 vm.py extract
python3 vm.py start-live
python3 vm.py install
echo "=== установка завершена, останавливаю live-ВМ ==="
python3 vm.py stop
