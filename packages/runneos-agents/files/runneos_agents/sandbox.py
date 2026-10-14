"""Песочница хода агента: лимиты ресурсов через systemd-run --user --scope (v1).

Решение владельца 2026-10-13 — срез «песочница» плана «политики → firstboot → песочница».
Проверено на systemd 262: scope-процесс передаётся менеджеру пользователя (PPID —
systemd --user, своя сессия), поэтому завершение хода останавливает юнит явно —
`systemctl --user stop <unit>` — с уникальным именем `--unit=`; killpg процессной
группы systemd-run payload не достаёт. Лимиты при этом настоящие: cgroup юнита
(MemoryMax/TasksMax/CPUQuota; контроллеры cpu/memory/pids делегированы user-slice).

Принципы (те же, что у политик):
- выключено по умолчанию: без секции `sandbox` в манифесте воркспейса ход идёт как
  раньше (у codex своя песочница записи workspace-write);
- сломанная секция запрещает ход (fail-closed), причина названа — никогда не
  «работаем с непонятной песочницей»;
- это лимиты, а не забор: файловой изоляции и сетевых правил в v1 нет (ProtectHome
  сломал бы codex — он пишет сессии в ~/.codex; сеть нужна для вызовов API);
  io-лимиты недоступны — контроллер io не делегирован в user-slice (systemd 262).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .workspace import read_manifest

__all__ = ["LoadedSandbox", "SandboxLimits", "load_sandbox"]

#: Команда запуска transient-юнита; ищется в PATH вызывающего, как любой CLI.
SYSTEMD_RUN = "systemd-run"

#: Префикс имени scope-юнита хода; суффикс добавляется на каждый ход.
UNIT_PREFIX = "runneos-agents-"

#: Режимы файловой изоляции поверх лимитов (v1.6). "none" — только лимиты.
FILESYSTEM_NONE = "none"
FILESYSTEM_BWRAP = "bwrap"
FILESYSTEM_MODES = (FILESYSTEM_NONE, FILESYSTEM_BWRAP)
BWRAP = "bwrap"

#: Дефолты при включённой секции без явных значений (решение владельца 2026-10-13).
DEFAULT_MEMORY_MAX = "2G"
DEFAULT_TASKS_MAX = 128

_CPU_QUOTA_RE = re.compile(r"^\d{1,3}%$")


def new_unit_name() -> str:
    """Уникальное имя scope-юнита одного хода (без суффикса .scope)."""
    return UNIT_PREFIX + uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class SandboxLimits:
    """Ресурсные рамки хода: то, что превращается в свойства юнита."""

    memory_max: str
    tasks_max: int
    cpu_quota: str | None = None


@dataclass(frozen=True)
class LoadedSandbox:
    """Песочница агента в том виде, в каком её прочитали для хода.

    `mode` — `off` (секции нет, поведение прежнее), `systemd-run` (лимиты заданы)
    или `broken` (секция сломана: ход запрещён, причина в `error`).
    `filesystem` — слой изоляции файлов поверх лимитов (v1.6): `none` или `bwrap`.
    """

    mode: str
    limits: SandboxLimits | None = None
    filesystem: str = FILESYSTEM_NONE
    error: str | None = None

    def enabled(self) -> bool:
        return self.mode == "systemd-run" and self.limits is not None

    def argv_prefix(
        self,
        *,
        workspace: Path | str | None = None,
        data_dirs: Sequence[Path | str] = (),
        systemd_run: str = SYSTEMD_RUN,
        bwrap: str = BWRAP,
    ) -> list[str]:
        """Префикс командной строки провайдера; пустой список, когда песочница выключена.

        Имя юнита уникально для вызова: по нему ход останавливает юнит при завершении
        (`systemctl --user stop`), потому что scope-процесс принадлежит менеджеру
        systemd и не попадает под killpg процессной группы systemd-run (проверено
        на systemd 262: PPID процесса — менеджер, своя сессия).

        Слой bwrap (v1.6, `filesystem: "bwrap"`): вся система только чтение, rw —
        воркспейс и каталоги провайдера (`data_dirs`); сеть не изолируется (нужна
        для API). Требует воркспейс: bwrap без rw-каталога работы смысла не имеет.
        """
        if not self.enabled():
            return []
        limits = self.limits
        prefix = [
            systemd_run,
            "--user",
            "--scope",
            f"--unit={new_unit_name()}",
            f"-pMemoryMax={limits.memory_max}",
            f"-pTasksMax={limits.tasks_max}",
        ]
        if limits.cpu_quota:
            prefix.append(f"-pCPUQuota={limits.cpu_quota}")
        if self.filesystem == FILESYSTEM_BWRAP:
            if workspace is None:
                raise ValueError("слой bwrap требует воркспейс, а получен None")
            root = Path(workspace).expanduser().absolute()
            layer = [
                bwrap,
                "--die-with-parent",
                "--ro-bind", "/", "/",
                "--dev", "/dev",
                "--proc", "/proc",
                "--tmpfs", "/tmp",
                "--bind", str(root), str(root),
            ]
            for directory in data_dirs:
                text = str(Path(directory).expanduser())
                layer += ["--bind-try", text, text]
            prefix += layer + ["--"]
        return prefix

    def summary(self) -> str:
        """Короткое человеческое состояние для `doctor` и панели."""
        if self.mode == "off":
            return "выключена (поведение прежнее; включается секцией sandbox в agent.json)"
        if self.mode == "broken":
            return f"сломана, ход запрещён: {self.error}"
        limits = self.limits
        text = f"systemd-run: MemoryMax={limits.memory_max}, TasksMax={limits.tasks_max}"
        if limits.cpu_quota:
            text += f", CPUQuota={limits.cpu_quota}"
        if self.filesystem == FILESYSTEM_BWRAP:
            text += " + bwrap (файлы только чтение, кроме воркспейса и каталогов провайдера)"
        return text


def load_sandbox(workspace: Path | str | None) -> LoadedSandbox:
    """Песочница агента для хода: из секции `sandbox` манифеста воркспейса.

    Секция отсутствует — off. Секция сломана (не объект, `mode` не `systemd-run`,
    лимиты не того типа) — fail-closed: ход запрещён, причина названа. Значения
    лимитов передаются systemd как есть (полномочия проверить их у него), но типы
    проверяются здесь, чтобы опечатка не превратилась в отсутствие лимитов.
    """
    if workspace is None:
        return LoadedSandbox(mode="off")
    section = read_manifest(workspace).get("sandbox")
    if section is None:
        return LoadedSandbox(mode="off")
    if not isinstance(section, dict):
        return LoadedSandbox(
            mode="broken", error=f"секция sandbox — объект, а не {type(section).__name__}"
        )
    mode = section.get("mode")
    if mode != "systemd-run":
        return LoadedSandbox(
            mode="broken",
            error=f"неизвестный режим песочницы: {mode!r}; ожидается 'systemd-run'",
        )

    memory_max = section.get("memory_max", DEFAULT_MEMORY_MAX)
    if not isinstance(memory_max, str) or not memory_max.strip():
        return LoadedSandbox(mode="broken", error="memory_max должен быть непустой строкой (например '2G')")
    tasks_max = section.get("tasks_max", DEFAULT_TASKS_MAX)
    if isinstance(tasks_max, bool) or not isinstance(tasks_max, int) or tasks_max < 1:
        return LoadedSandbox(mode="broken", error="tasks_max должен быть целым числом ≥ 1")
    cpu_quota = section.get("cpu_quota")
    if cpu_quota is not None:
        if not isinstance(cpu_quota, str) or not _CPU_QUOTA_RE.match(cpu_quota.strip()):
            return LoadedSandbox(
                mode="broken", error=f"cpu_quota ожидается как 'NN%' (например '80%'), а не {cpu_quota!r}"
            )
        cpu_quota = cpu_quota.strip()
    filesystem = section.get("filesystem", FILESYSTEM_NONE)
    if filesystem not in FILESYSTEM_MODES:
        return LoadedSandbox(
            mode="broken",
            error=f"неизвестный filesystem: {filesystem!r}; ожидается 'none' или 'bwrap'",
        )
    return LoadedSandbox(
        mode="systemd-run",
        limits=SandboxLimits(
            memory_max=memory_max.strip(), tasks_max=tasks_max, cpu_quota=cpu_quota
        ),
        filesystem=filesystem,
    )
