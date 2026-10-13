#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
skill_source="$repo_root/.agents/skills/runneos"
test -f "$skill_source/SKILL.md"

skill_targets=("$HOME/.agents/skills/runneos" "${CODEX_HOME:-$HOME/.codex}/skills/runneos" "$HOME/.claude/skills/runneos")
for skill_target in "${skill_targets[@]}"; do
  if [[ -e "$skill_target" || -L "$skill_target" ]]; then
    if [[ -L "$skill_target" && "$(readlink -f -- "$skill_target")" == "$skill_source" ]]; then
      continue
    fi
    printf 'Existing skill left untouched: %s\n' "$skill_target" >&2
    exit 1
  fi
done
for skill_target in "${skill_targets[@]}"; do
  if [[ ! -L "$skill_target" ]]; then
    mkdir -p -- "$(dirname -- "$skill_target")"
    ln -s -- "$skill_source" "$skill_target"
  fi
  printf 'runneos: %s -> %s\n' "$skill_target" "$skill_source"
done
