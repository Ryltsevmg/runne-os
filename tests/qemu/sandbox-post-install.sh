#!/bin/bash
# Exercise the repository step of runneos-post-install against a fake pacman.conf.
#
# The real step decides what to do with the [runneos] stanza the live medium
# leaves behind — the installer copies the live /etc/pacman.conf into the new
# system, where `file:///runneos-repo/$arch` can never exist. This script runs
# the step in --check mode (nothing is written) with overridable paths and two
# different servers: one whose name does not resolve, and the project server.
#
#   ./sandbox-post-install.sh [path-to-runneos-post-install]
set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd)
script=${1:-$here/../../packages/runneos-config/files/runneos-post-install}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

run_case() {
	local name=$1 server=$2
	mkdir -p "$work/$name"
	cat >"$work/$name/pacman.conf" <<'EOF'
[options]
HoldPkg = pacman glibc

[core]
Include = /etc/pacman.d/mirrorlist

[runneos]
SigLevel = Optional TrustAll
Server = file:///runneos-repo/$arch
EOF
	: >"$work/$name/hosts"
	echo "### случай: $name (server=$server)"
	RUNNEOS_PACMAN_CONF="$work/$name/pacman.conf" \
		RUNNEOS_HOSTS_CONF="$work/$name/hosts" \
		RUNNEOS_REPO_SERVER="$server" \
		RUNNEOS_REPO_IP=100.69.28.21 \
		bash "$script" --check 2>&1 | sed -n '1,8p'
	echo
}

run_case "имя не разрешается (как на ноутбуке вне tailnet)" "https://repo-does-not-resolve.invalid/runneos"
run_case "переопределение сервера работает" "https://example.invalid/runneos" "RUNNEOS_REPO_SERVER"

# With the signing keyring installed the stanza must demand verification
# (Required DatabaseOptional); without it — keep the historical TrustAll.
mkdir -p "$work/keyring"
cat >"$work/keyring/pacman.conf" <<'EOF'
[options]
HoldPkg = pacman glibc

[core]
Include = /etc/pacman.d/mirrorlist

[runneos]
SigLevel = Optional TrustAll
Server = file:///runneos-repo/$arch
EOF
: >"$work/keyring/hosts"
RUNNEOS_PACMAN_CONF="$work/keyring/pacman.conf" \
	RUNNEOS_HOSTS_CONF="$work/keyring/hosts" \
	RUNNEOS_KEYRING_PRESENT=1 \
	bash "$script" --apply >/dev/null 2>&1
if grep -q 'SigLevel = Required DatabaseOptional' "$work/keyring/pacman.conf"; then
	echo "### keyring: сигнатура переключена на Required DatabaseOptional — ok"
else
	echo "### keyring: ОЖИДАЛСЯ Required DatabaseOptional, в конфиге:" >&2
	cat "$work/keyring/pacman.conf" >&2
	exit 1
fi
