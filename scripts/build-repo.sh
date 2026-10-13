#!/bin/bash
# Build every local RunneOS package and publish them as the pacman repository
# that iso/pacman.conf consumes as [runneos].
#
# Run as a normal user: makepkg refuses to build as root. The output directory
# out/ is created by mkarchiso, so if it is root-owned, give yourself the repo
# subdirectory first:  sudo mkdir -p out/repo && sudo chown $USER out/repo
#
# Result: out/repo/$arch/runneos.db.tar.zst plus exactly the packages built by
# this run — stale artifacts from earlier builds are removed first, so the
# published repository never mixes versions.
#
# runneos-vicinae is skipped by default: it is a heavy Qt/C++ build, the
# desktop metapackage does not need it, and the component is not installed on
# the workstation. Include it with:  ./scripts/build-repo.sh --with-vicinae
set -euo pipefail

cd "$(dirname "$0")/.."
root=$PWD
arch=${ARCH:-x86_64}
repo="$root/out/repo/$arch"
with_vicinae=0
if [[ "${1:-}" == "--with-vicinae" ]]; then
	with_vicinae=1
fi

mkdir -p "$repo"

# makepkg --nodeps skips build-time dependency checks; runtime `depends` are
# still recorded in the packages and resolved by pacman at install time. The
# build host must already provide the runtime dependencies, otherwise the
# packages' own check() steps fail.
built=(); skipped=(); failed=(); fresh=()
shopt -s nullglob

for dir in "$root"/packages/*/; do
	name=$(basename "$dir")
	[[ -f "$dir/PKGBUILD" ]] || continue
	if [[ "$name" == runneos-vicinae && $with_vicinae == 0 ]]; then
		skipped+=("$name"); continue
	fi
	printf '=== %s ===\n' "$name"
	# Drop this package's older artifacts so the repository only ever carries
	# the result of the current run.
	rm -f "$dir"/*.pkg.tar.zst
	if ( cd "$dir" && makepkg -f --noconfirm --nodeps ); then
		built+=("$name")
		# Publish only what this run produced, and skip makepkg debug splits.
		for artifact in "$dir"*.pkg.tar.zst; do
			[[ "$artifact" == *-debug-* ]] && continue
			fresh+=("$artifact")
		done
	else
		failed+=("$name")
	fi
done

# A package that is already published must not silently disappear: that would
# remove a working version from every client, so its failure aborts publication
# before anything is touched. A *new* package that fails has never been
# published, so it cannot regress anyone — it is reported and skipped, which
# keeps a work-in-progress package from blocking everyone else's updates.
blocking=(); skipped_failures=()
for name in "${failed[@]:-}"; do
	[[ -n $name ]] || continue
	if compgen -G "$repo/$name-*.pkg.tar.zst" >/dev/null; then
		blocking+=("$name")
	else
		skipped_failures+=("$name")
	fi
done
if (( ${#blocking[@]} )); then
	printf 'НЕ СОБРАЛИСЬ (уже публиковались): %s\n' "${blocking[*]}" >&2
	echo 'Публикация не выполнялась: прежний репозиторий оставлен без изменений.' >&2
	exit 1
fi
if (( ${#skipped_failures[@]} )); then
	printf 'предупреждение: новые пакеты не собрались и не публикуются: %s\n' "${skipped_failures[*]}" >&2
fi

# Fresh repository: old package files and databases must not survive, or the
# ISO could resolve an obsolete version.
rm -f "$repo"/*.pkg.tar.zst "$repo"/runneos.db* "$repo"/runneos.files*

if (( ${#fresh[@]} == 0 )); then
	echo 'нет собранных пакетов' >&2
	exit 1
fi
cp -f "${fresh[@]}" "$repo/"
repo-add -q "$repo/runneos.db.tar.zst" "$repo"/*.pkg.tar.zst

echo
echo "собрано: ${#built[@]}  пропущено: ${#skipped[@]}  ошибок: ${#failed[@]}"
echo "репозиторий: $repo"
echo "пакетов в репозитории: $(ls -1 "$repo"/*.pkg.tar.zst | wc -l)"
