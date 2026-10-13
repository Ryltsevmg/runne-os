#!/bin/bash
# Publish the local [runneos] repository so installed systems can update.
#
#   1. builds the packages and rebuilds out/repo/$arch (scripts/build-repo.sh)
#   2. signs the database and packages when a signing key is configured
#   3. serves out/repo over the tailnet with `tailscale serve`
#   4. prints the client stanza to paste into /etc/pacman.conf
#
# Root is needed only for the `tailscale serve` step. Override the address with
# RUNNEOS_REPO_SERVER when the repository moves.
set -euo pipefail

cd "$(dirname "$0")/.."
root=$PWD
repo_dir=$root/out/repo
arch=${ARCH:-x86_64}
serve_path=${RUNNEOS_SERVE_PATH:-/runneos}
server=${RUNNEOS_REPO_SERVER:-https://$(tailscale status --json 2>/dev/null |
	python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["Self"]["DNSName"].rstrip("."))')$serve_path}

./scripts/build-repo.sh

# Signing is optional but recommended: without it a client must trust the
# repository blindly (SigLevel = Optional TrustAll). See references/repository.md
# for how to create a dedicated key.
if [[ -n "${RUNNEOS_GPG_KEY:-}" ]]; then
	echo "подписываю базу и пакеты ключом $RUNNEOS_GPG_KEY"
	repo-add --sign --key "$RUNNEOS_GPG_KEY" -q "$repo_dir/$arch/runneos.db.tar.zst" "$repo_dir/$arch"/*.pkg.tar.zst
	for package in "$repo_dir/$arch"/*.pkg.tar.zst; do
		[[ $package == *.sig ]] && continue
		gpg --batch --yes --detach-sign --local-user "$RUNNEOS_GPG_KEY" "$package"
	done
	siglevel='Required DatabaseOptional'
else
	echo 'ВНИМАНИЕ: RUNNEOS_GPG_KEY не задан, репозиторий публикуется без подписи.'
	echo '          Клиенту придётся указать SigLevel = Optional TrustAll.'
	siglevel='Optional TrustAll'
fi

# Serving needs root the first time, but the configuration persists in
# tailscaled's state. Re-running `tailscale serve` on every publish would ask for
# sudo each time, so touch it only when the served path is not the current one.
if sudo tailscale serve status 2>/dev/null | grep -qF "$repo_dir"; then
	echo "раздача уже настроена: $serve_path → $repo_dir"
else
	echo "настраиваю раздачу $repo_dir через tailscale serve"
	sudo tailscale serve --bg --set-path "$serve_path" "$repo_dir"
fi

echo
echo "=== адрес ==="
echo "  $server/\$arch"
echo
echo "=== раздел для /etc/pacman.conf на клиенте ==="
cat <<EOF
[runneos]
SigLevel = $siglevel
Server = $server/\$arch
EOF
echo
echo "=== быстрый способ подключить на новой машине ==="
cat <<EOF
printf '\\n[runneos]\\nSigLevel = $siglevel\\nServer = $server/\\\$arch\\n' | sudo tee -a /etc/pacman.conf
sudo pacman -Sy && sudo pacman -S runneos-config && sudo runneos-post-install --apply
EOF
echo
echo "Проверка: curl -sI $server/$arch/runneos.db"
echo "Отключить раздачу: sudo tailscale serve --https=443 off"
