"""Воркспейс агента: каталог, манифест и минимальная изоляция по договорённости.

Один агент — один воркспейс. Агент работает только внутри своего каталога.
Файловая изоляция уровня ОС — следующий слой после песочницы v1.5 (лимиты ресурсов
через systemd-run --scope, модуль sandbox.py): здесь только раскладка каталогов
и явные границы, чтобы следующий срез было куда встроить.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .model import Agent, now_iso

__all__ = ["Workspace", "agent_workspace_path", "ensure_workspace", "read_manifest", "write_manifest"]

MANIFEST_NAME = "agent.json"
README_NAME = "README.md"

_README = """# Воркспейс агента RunneOS

Этот каталог — рабочая область одного агента. Всё, что агент делает с файлами,
происходит здесь. Внешние каталоги агент не должен трогать без явного разрешения.

- `agent.json` — манифест: кто это и когда создан.
- `runs/` — журналы прогонов (создаётся при первом запуске).
"""


class Workspace:
    """Разложенный по каталогам воркспейс агента."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @property
    def manifest_path(self) -> Path:
        return self.path / MANIFEST_NAME

    @property
    def runs_dir(self) -> Path:
        return self.path / "runs"

    def exists(self) -> bool:
        return self.path.is_dir()

    def read_manifest(self) -> dict:
        return read_manifest(self.path)

    def __str__(self) -> str:  # удобно в логах
        return str(self.path)


def agent_workspace_path(root: Path, agent_id: str) -> Path:
    """Путь воркспейса агента внутри корня данных."""
    return Path(root) / "workspaces" / agent_id


def read_manifest(path: Path) -> dict:
    """Прочитать манифест воркспейса; пустой словарь, если его нет или он повреждён."""
    manifest = Path(path) / MANIFEST_NAME
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_manifest(path: Path, manifest: dict) -> None:
    """Атомарно записать манифест (tmp-файл + rename рядом с целью)."""
    target = Path(path) / MANIFEST_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(MANIFEST_NAME + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def ensure_workspace(agent: Agent, root: Path, *, git: bool = True) -> Workspace:
    """Создать и подготовить воркспейс агента. Идемпотентно.

    Существующий каталог не очищается: чужая работа не удаляется. Манифест обновляется,
    `created_at` в нём сохраняется от первого создания.
    """
    path = Path(agent.workspace) if agent.workspace else agent_workspace_path(root, agent.id)
    workspace = Workspace(path)
    path.mkdir(parents=True, exist_ok=True)
    (path / "runs").mkdir(exist_ok=True)

    previous = read_manifest(path)
    manifest = {
        "agent_id": agent.id,
        "name": agent.name,
        "role": agent.role,
        "provider": agent.provider,
        "created_at": previous.get("created_at") or agent.created_at or now_iso(),
        "updated_at": now_iso(),
    }
    # Секции политики и песочницы пишет владелец воркспейса, не пакет:
    # ensure_workspace не должен терять их при обновлении служебных полей.
    # Значения переносятся как есть.
    for owner_key in ("policy", "sandbox"):
        if owner_key in previous:
            manifest[owner_key] = previous[owner_key]
    write_manifest(path, manifest)
    readme = path / README_NAME
    if not readme.exists():
        readme.write_text(_README, encoding="utf-8")

    if git and not (path / ".git").exists():
        _init_git(path)
    return workspace


def _init_git(path: Path) -> bool:
    """Локальный git-репозиторий в воркспейсе: агент может откатывать свои правки.

    Тихо ничего не делает, если git недоступен: это удобство, а не зависимость.
    """
    try:
        subprocess.run(
            ["git", "init", "--quiet", "--initial-branch=main", str(path)],
            check=True,
            capture_output=True,
            timeout=20,
        )
        (path / ".gitignore").write_text("runs/\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(path), "add", "-A"],
            check=True,
            capture_output=True,
            timeout=20,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "-c",
                "user.name=RunneOS Agents",
                "-c",
                "user.email=agents@runneos.local",
                "commit",
                "--quiet",
                "-m",
                "воркспейс агента создан",
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True
