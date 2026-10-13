#!/bin/bash
# Publish a sanitized, code-only public snapshot of this repository to GitHub.
#
# - публикуемая часть: packages/, iso/, installer/, scripts/, tests/,
#   .github/, LICENSE, README.md, .gitignore;
# - НЕ публикуется: слой знаний агентов (.agents/, .runneos/), внутренние
#   протоколы (docs/, migrations/, tests/results-*.md), агентские инструкции
#   (AGENTS.md, CLAUDE.md, GEMINI.md);
# - история публичного репозитория — один snapshot-коммит за запуск (личная
#   почта из локальной истории не попадает на GitHub);
# - гейт: если в staged-дереве найден личный идентификатор — публикация
#   отменяется.
#
# Переменные: RUNNEOS_PUBLISH_REPO, RUNNEOS_PUBLISH_BRANCH, RUNNEOS_PUBLISH_STAGE.
set -euo pipefail
cd "$(dirname "$0")/.."
root=$PWD
stage=${RUNNEOS_PUBLISH_STAGE:-$root/out/github-publish}
repo=${RUNNEOS_PUBLISH_REPO:-https://github.com/Ryltsevmg/runne-os.git}
branch=${RUNNEOS_PUBLISH_BRANCH:-main}

pattern='ryltsevmg|tail71ee4f|yandex|keenetik|sourcecraft|omarchy'

rm -rf "$stage"
mkdir -p "$stage"
git archive HEAD | tar -x -C "$stage"

rm -rf "$stage"/.agents "$stage"/.runneos "$stage"/docs "$stage"/migrations \
       "$stage"/agents "$stage"/AGENTS.md "$stage"/CLAUDE.md "$stage"/GEMINI.md
rm -f "$stage"/tests/results-*.md

# Гейт приватности
if grep -rniE "$pattern" "$stage" --exclude-dir=.git . | grep -q .; then
	echo "ОТКАЗ: в staged-дереве найдены личные идентификаторы:" >&2
	grep -rniE "$pattern" "$stage" --exclude-dir=.git . | head -10 >&2
	exit 1
fi

cd "$stage"
git init -q -b "$branch"
git add -A
GIT_AUTHOR_NAME="RunneOS" GIT_AUTHOR_EMAIL="agent@runneos.local" \
GIT_COMMITTER_NAME="RunneOS" GIT_COMMITTER_EMAIL="agent@runneos.local" \
git commit -q -m "RunneOS public snapshot $(date -u +%Y-%m-%dT%H:%MZ)"
git remote add origin "$repo" 2>/dev/null || git remote set-url origin "$repo"
git push --force origin "$branch"
echo "опубликовано: $repo ($branch)"
