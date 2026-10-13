"""Общая основа CLI-адаптеров RunneOS.

Контракт: packages/runneos-agents/INTERFACE.md, раздел 5 (FROZEN v1).

Здесь живёт всё, что одинаково у всех провайдеров:

* `Availability` и `ProviderUnavailable` берутся из ядра (`runneos_agents.model`) и только
  реэкспортируются: у ядра и адаптеров должен быть один тип, а не две похожие копии;
* поиск бинаря: сначала PATH, затем стандартные каталоги пользователя
  (`~/.local/bin`, `/usr/local/bin`, `/usr/bin`);
* редактирование секретов: значения переменных окружения с KEY/TOKEN/SECRET/... и узнаваемые
  формы токенов не попадают ни в события, ни в логи адаптера;
* запуск внешнего CLI: построчный JSONL из stdout, хвост stderr, таймаут с убийством процесса;
* `CliJsonlAdapter` — каркас start/resume; провайдер задаёт только argv и разбор строк.

Провайдер вызывается как внешний процесс: его код в пакет не копируется, чужие данные
(`~/.codex`, ключи) пакет не читает и не хранит.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Protocol

from ..model import Availability, ProviderUnavailable

__all__ = [
    "Adapter",
    "Availability",
    "CliJsonlAdapter",
    "MASK",
    "ProviderUnavailable",
    "RunContext",
    "collect_secrets",
    "default_search_dirs",
    "find_binary",
    "probe_version",
    "redact",
    "redact_argv",
    "redact_object",
    "stream_process",
    "terminate_process",
]

logger = logging.getLogger("runneos_agents.adapters")

#: Чем заменяются секреты в текстах и событиях.
MASK = "<скрыто>"

#: Признаки секретной переменной окружения (по имени переменной).
SECRET_KEY_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH")

#: Значения короче этого не скрываем: слишком общая строка, замены пойдут во вред.
MIN_SECRET_LENGTH = 4

#: Дополнительная страховка: узнаваемые формы токенов в любом тексте.
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}"),
)

#: Сколько последних строк stderr держим для сообщения об ошибке.
STDERR_TAIL_LINES = 25
#: Сколько ждём процесс после SIGTERM, прежде чем добить SIGKILL.
TERMINATE_GRACE = 2.0
#: Сколько ждём завершения процесса после закрытия его вывода.
EXIT_GRACE = 5.0
#: Сколько ждём фоновые потоки чтения при выходе.
THREAD_JOIN_GRACE = 1.0
#: Признак конца потока в очереди читающих потоков.
_EOF = object()


class Adapter(Protocol):
    """Протокол адаптера провайдера (INTERFACE.md, раздел 5)."""

    provider: str

    def availability(self) -> Availability:
        """Доступен ли провайдер на этой машине: честное состояние вместо выдуманного ответа."""
        ...

    def start(
        self,
        ws: Path,
        prompt: str,
        *,
        model: str | None = None,
        instructions: str = "",
        timeout: float | None = None,
    ) -> Iterator[dict]:
        """Начать новый ход агента в каталоге `ws`."""
        ...

    def resume(
        self,
        ws: Path,
        provider_session_id: str,
        prompt: str,
        *,
        model: str | None = None,
        timeout: float | None = None,
    ) -> Iterator[dict]:
        """Продолжить сессию провайдера."""
        ...


class RunContext:
    """Состояние одного прогона (одного вызова start/resume).

    Нужно, чтобы провайдер мог копить данные по ходу потока (например, usage opencode
    приходит отдельными шагами) и при этом не хранить это в самом адаптере: один и тот же
    адаптер может обслуживать несколько сессий из разных потоков.
    """

    __slots__ = ("_usage",)

    def __init__(self) -> None:
        self._usage: dict[str, int] = {}

    def add_usage(self, usage: dict[str, Any] | None) -> None:
        """Прибавить числовые поля usage к итогу прогона (ключи сохраняются как есть)."""
        if not isinstance(usage, dict):
            return
        for key, value in usage.items():
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            name = str(key)
            self._usage[name] = self._usage.get(name, 0) + int(value)

    @property
    def usage(self) -> dict[str, int] | None:
        """Итоговый usage прогона или None, если провайдер его не сообщил."""
        return dict(self._usage) or None


# --- секреты ---------------------------------------------------------------


def collect_secrets(environment: dict[str, str] | None = None) -> tuple[str, ...]:
    """Значения переменных окружения, которые нельзя показывать: ключи, токены, пароли.

    Отбор по имени переменной (KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL/AUTH) и длине значения.
    Длинные значения идут первыми, чтобы короткое не разрезало длинное при замене.
    """
    env = dict(environment if environment is not None else os.environ)
    values: set[str] = set()
    for key, value in env.items():
        upper = str(key).upper()
        if not any(marker in upper for marker in SECRET_KEY_MARKERS):
            continue
        if isinstance(value, str) and len(value) >= MIN_SECRET_LENGTH:
            values.add(value)
    return tuple(sorted(values, key=len, reverse=True))


def redact(text: Any, secrets: Sequence[str] = ()) -> str:
    """Убрать из текста значения секретов и узнаваемые формы токенов."""
    result = text if isinstance(text, str) else str(text)
    for value in sorted({str(item) for item in secrets if item}, key=len, reverse=True):
        result = result.replace(value, MASK)
    for pattern in SECRET_PATTERNS:
        result = pattern.sub(MASK, result)
    return result


def redact_object(value: Any, secrets: Sequence[str] = ()) -> Any:
    """Рекурсивно отредактировать JSON-совместимое значение (строки, списки, словари)."""
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {key: redact_object(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_object(item, secrets) for item in value]
    return value


def redact_argv(argv: Sequence[Any], secrets: Sequence[str] = ()) -> list[str]:
    """Аргументы командной строки без секретов — только для журнала и логов."""
    return [redact(str(item), secrets) for item in argv]


# --- поиск бинаря и версия -------------------------------------------------


def default_search_dirs() -> tuple[Path, ...]:
    """Каталоги, куда обычно ставят CLI-провайдеры вне системного PATH."""
    return (Path.home() / ".local/bin", Path("/usr/local/bin"), Path("/usr/bin"))


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def find_binary(name: str, *, search_paths: Sequence[str | Path] | None = None) -> str | None:
    """Найти исполняемый файл провайдера: сначала PATH, затем стандартные каталоги.

    `search_paths=None` — каталоги по умолчанию; пустая последовательность — только PATH
    (нужно тестам, чтобы поиск не зависел от машины).
    """
    binary_name = str(name or "").strip()
    if not binary_name:
        return None
    found = shutil.which(binary_name)
    if found:
        return found
    dirs = default_search_dirs() if search_paths is None else tuple(Path(p) for p in search_paths)
    for directory in dirs:
        candidate = Path(directory).expanduser() / binary_name
        if _is_executable_file(candidate):
            return str(candidate)
    return None


def probe_version(
    binary: str,
    args: Sequence[str] = ("--version",),
    *,
    timeout: float = 10.0,
    env: dict[str, str] | None = None,
) -> str | None:
    """Первая строка вывода `<binary> --version`; None, если получить не удалось.

    Версия — удобство для `provider list`; её отсутствие не делает провайдера недоступным.
    """
    try:
        done = subprocess.run(
            [str(binary), *[str(a) for a in args]],
            capture_output=True,
            text=True,
            timeout=float(timeout),
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    text = (done.stdout or done.stderr or "").strip()
    if not text:
        return None
    line = text.splitlines()[0].strip()
    return line[:200] or None


# --- запуск внешнего процесса ----------------------------------------------


def _signal_process(proc: Any, sig: int) -> None:
    """Послать сигнал процессу (по возможности — всей его группе)."""
    pid = getattr(proc, "pid", None)
    # pid > 1: группу имеет только настоящий процесс. killpg(0) или killpg(-1) убили бы
    # процессы нашего окружения, поэтому такие значения никогда не используем.
    if isinstance(pid, int) and pid > 1:
        try:
            os.killpg(pid, sig)
            return
        except OSError:
            pass  # группы нет или процесс уже умер — пробуем адресно
    sender = getattr(proc, "send_signal", None)
    if callable(sender):
        try:
            sender(sig)
            return
        except OSError:
            pass
    fallback = getattr(proc, "kill" if sig == signal.SIGKILL else "terminate", None)
    if callable(fallback):
        try:
            fallback()
        except OSError:
            pass


def terminate_process(proc: Any, grace: float = TERMINATE_GRACE) -> None:
    """Остановить процесс: сначала SIGTERM, затем, если не ушёл, SIGKILL."""
    try:
        if proc.poll() is not None:
            return
    except Exception:  # noqa: BLE001 — чужой объект процесса не должен ломать адаптер
        return
    _signal_process(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
        return
    except Exception:  # noqa: BLE001
        pass
    _signal_process(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=grace)
    except Exception:  # noqa: BLE001
        pass


def _wait_for_exit(proc: Any, timeout: float = EXIT_GRACE) -> int | None:
    """Код возврата процесса; None, если дождаться не удалось."""
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_process(proc)
        try:
            return proc.wait(timeout=TERMINATE_GRACE)
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001
        return None


def stream_process(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    secrets: Sequence[str] = (),
) -> Iterator[tuple[str, Any]]:
    """Запустить внешний CLI и отдать его вывод по мере поступления.

    Отдаёт кортежи `(вид, значение)`:

    * `("line", dict)` — разобранная строка JSONL;
    * `("badline", str)` — строка stdout, которая не является JSON;
    * `("stderr_tail", str)` — накопленный хвост stderr (один раз в конце);
    * `("timeout", float)` — сработал таймаут, процесс убит;
    * `("exit", int | None)` — код возврата;
    * `("spawn_error", str)` — процесс не удалось запустить.

    Секреты вырезаются из всего, что попадает в `badline`/`stderr_tail`. Если потребитель
    бросает генератор, процесс всё равно останавливается (`finally`).
    """
    command = [str(item) for item in argv]
    try:
        proc = subprocess.Popen(  # noqa: S603 — внешний CLI провайдера, argv без оболочки
            command,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
    except OSError as error:
        yield ("spawn_error", redact(str(error), secrets))
        return

    channel: queue.Queue[tuple[str, Any]] = queue.Queue()

    def read_stream(stream: Any, tag: str) -> None:
        try:
            if stream is not None:
                for line in stream:
                    channel.put((tag, line))
        except Exception:  # noqa: BLE001 — поток мог закрыться вместе с процессом
            pass
        finally:
            channel.put((tag, _EOF))
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass

    threads = [
        threading.Thread(
            target=read_stream, args=(proc.stdout, "out"), name="runneos-adapter-stdout", daemon=True
        ),
        threading.Thread(
            target=read_stream, args=(proc.stderr, "err"), name="runneos-adapter-stderr", daemon=True
        ),
    ]
    for thread in threads:
        thread.start()

    deadline = None if timeout is None else time.monotonic() + max(float(timeout), 0.0)
    finished = 0
    stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
    timed_out: float | None = None

    try:
        while finished < len(threads):
            if deadline is None:
                remaining: float | None = None
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = float(timeout) if timeout is not None else 0.0
                    terminate_process(proc)
                    break
            try:
                tag, line = channel.get(timeout=remaining)
            except queue.Empty:
                timed_out = float(timeout) if timeout is not None else 0.0
                terminate_process(proc)
                break
            if line is _EOF:
                finished += 1
                continue
            text = str(line).rstrip("\r\n")
            if tag == "out":
                if not text.strip():
                    continue
                try:
                    parsed = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    yield ("badline", redact(text, secrets))
                    continue
                if isinstance(parsed, dict):
                    yield ("line", parsed)
                else:
                    yield ("badline", redact(text, secrets))
            else:
                clean = redact(text, secrets)
                if clean.strip():
                    stderr_tail.append(clean)

        if timed_out is not None:
            yield ("timeout", timed_out)
        yield ("stderr_tail", "\n".join(stderr_tail))
        yield ("exit", _wait_for_exit(proc))
    finally:
        terminate_process(proc)
        for thread in threads:
            thread.join(timeout=THREAD_JOIN_GRACE)


# --- каркас адаптера -------------------------------------------------------


class CliJsonlAdapter:
    """Адаптер провайдера, который говорит JSONL в stdout.

    Потомок задаёт `provider`, `binary_name`, `build_argv`, `normalize` и (если провайдер
    сообщает идентификатор сессии не в `thread_id`) `extract_session_id`.
    """

    #: Ключ провайдера в реестре (совпадает с `Agent.provider`).
    provider: str = ""
    #: Имя исполняемого файла для поиска.
    binary_name: str = ""
    #: Аргументы для получения версии.
    version_args: tuple[str, ...] = ("--version",)

    def __init__(
        self,
        binary: str | None = None,
        *,
        env: dict[str, str] | None = None,
        search_paths: Sequence[str | Path] | None = None,
        version_timeout: float = 10.0,
    ) -> None:
        """`binary` — явный путь (иначе поиск по PATH и стандартным каталогам).

        `env` — добавка к окружению процесса; наследуемое окружение не подменяется, поэтому
        `CODEX_HOME` и прочие настройки провайдера уходят дальше как есть.
        """
        self._binary = str(binary) if binary else None
        self._env_extra = {str(key): str(value) for key, value in (env or {}).items()}
        self._search_paths = None if search_paths is None else tuple(search_paths)
        self._version_timeout = float(version_timeout)
        self._version: str | None = None

    # --- окружение и доступность

    def resolve_binary(self) -> str | None:
        """Путь к исполняемому файлу провайдера или None."""
        if self._binary:
            return self._binary if _is_executable_file(Path(self._binary)) else None
        return find_binary(self.binary_name, search_paths=self._search_paths)

    def environment(self) -> dict[str, str]:
        """Окружение процесса: унаследованное плюс явные добавки."""
        env = dict(os.environ)
        env.update(self._env_extra)
        return env

    def secrets(self) -> tuple[str, ...]:
        """Значения секретов текущего окружения (для редактирования)."""
        return collect_secrets(self.environment())

    def missing_reason(self) -> str:
        """Человекочитаемая причина недоступности (без секретов)."""
        return (
            f"бинарь {self.binary_name or self.provider} не найден: "
            "его нет в PATH и в стандартных каталогах (~/.local/bin, /usr/local/bin, /usr/bin)"
        )

    def version(self) -> str | None:
        """Версия провайдера (кэшируется) или None."""
        if self._version is not None:
            return self._version
        binary = self.resolve_binary()
        if not binary:
            return None
        self._version = probe_version(
            binary, self.version_args, timeout=self._version_timeout, env=self.environment()
        )
        return self._version

    def availability(self) -> Availability:
        """Доступность провайдера: не бросает исключений."""
        binary = self.resolve_binary()
        if not binary:
            return Availability(provider=self.provider, available=False, reason=self.missing_reason())
        version: str | None = None
        try:
            version = self.version()
        except Exception:  # noqa: BLE001 — версия не обязательна для работы
            version = None
        return Availability(
            provider=self.provider, available=True, reason="", version=version, binary=binary
        )

    def _require_binary(self) -> str:
        binary = self.resolve_binary()
        if not binary:
            raise ProviderUnavailable(self.provider, self.missing_reason())
        return binary

    # --- контракт адаптера

    def start(
        self,
        ws: Path,
        prompt: str,
        *,
        model: str | None = None,
        instructions: str = "",
        timeout: float | None = None,
    ) -> Iterator[dict]:
        """Начать новый ход. Без бинаря бросает ProviderUnavailable сразу, до генератора."""
        binary = self._require_binary()
        return self._stream(
            binary,
            ws,
            self.compose_prompt(prompt, instructions),
            provider_session_id=None,
            model=model,
            timeout=timeout,
        )

    def resume(
        self,
        ws: Path,
        provider_session_id: str,
        prompt: str,
        *,
        model: str | None = None,
        timeout: float | None = None,
    ) -> Iterator[dict]:
        """Продолжить сессию провайдера."""
        session_id = str(provider_session_id or "").strip()
        if not session_id:
            raise ValueError("resume без идентификатора сессии провайдера")
        binary = self._require_binary()
        return self._stream(
            binary,
            ws,
            self.compose_prompt(prompt, ""),
            provider_session_id=session_id,
            model=model,
            timeout=timeout,
        )

    def compose_prompt(self, prompt: str, instructions: str) -> str:
        """Текст задачи для провайдера.

        Инструкции агента идут преамбулой: у `codex exec` нет подтверждённого флага
        системных инструкций, а выдумывать флаг нельзя.
        """
        task = str(prompt or "")
        role = str(instructions or "").strip()
        if not role:
            return task
        return f"{role}\n\n{task}"

    # --- переопределяемое потомками

    def build_argv(
        self,
        binary: str,
        workspace: Path,
        prompt: str,
        *,
        model: str | None,
        provider_session_id: str | None,
    ) -> list[str]:
        """Собрать командную строку провайдера (без оболочки)."""
        raise NotImplementedError

    def normalize(self, obj: dict, ctx: RunContext) -> Iterator[dict]:
        """Превратить разобранную строку JSONL в нормализованные события."""
        raise NotImplementedError

    def extract_session_id(self, obj: dict) -> str | None:
        """Идентификатор сессии провайдера в строке JSONL (у codex — thread_id)."""
        return None

    # --- внутреннее

    def _stream(
        self,
        binary: str,
        ws: Path,
        prompt: str,
        *,
        provider_session_id: str | None,
        model: str | None,
        timeout: float | None,
    ) -> Iterator[dict]:
        """Общий ход: запуск, разбор, нормализованные события, честный итог."""
        workspace = Path(ws).expanduser().absolute()
        argv = self.build_argv(
            binary,
            workspace,
            prompt,
            model=model,
            provider_session_id=provider_session_id,
        )
        env = self.environment()
        secrets = self.secrets()
        logger.debug("%s: запуск %s", self.provider, " ".join(redact_argv(argv, secrets)))

        ctx = RunContext()
        yield {"kind": "turn.started"}

        known_session = provider_session_id
        error_text: str | None = None
        seen_errors: set[str] = set()
        exit_code: int | None = None
        timed_out: float | None = None
        spawn_error: str | None = None
        stderr_tail = ""

        for tag, payload in stream_process(
            argv, cwd=workspace, env=env, timeout=timeout, secrets=secrets
        ):
            if tag == "line":
                session_id = str(self.extract_session_id(payload) or "")
                if session_id and session_id != known_session:
                    known_session = session_id
                    yield redact_object(
                        {"kind": "provider_session", "provider_session_id": session_id}, secrets
                    )
                for event in self.normalize(payload, ctx) or ():
                    if not isinstance(event, dict) or not event.get("kind"):
                        continue
                    if event.get("kind") == "error":
                        text = str(event.get("text") or "")
                        # Об одном сбое провайдер может сказать дважды (codex: error и
                        # turn.failed с тем же текстом) — в журнал отдаём один раз.
                        if text and text in seen_errors:
                            continue
                        if text:
                            seen_errors.add(text)
                            if error_text is None:
                                error_text = text
                    # Гарантия контракта: ни одно событие не содержит значений секретов.
                    yield redact_object(event, secrets)
            elif tag == "badline":
                yield {"kind": "raw", "payload": {"stdout": payload}}
            elif tag == "stderr_tail":
                stderr_tail = str(payload or "")
            elif tag == "timeout":
                timed_out = float(payload or 0.0)
            elif tag == "exit":
                exit_code = payload if isinstance(payload, int) else None
            elif tag == "spawn_error":
                spawn_error = str(payload)
                error_text = spawn_error
                yield redact_object(
                    {
                        "kind": "error",
                        "text": (
                            f"не удалось запустить {self.binary_name or self.provider}: {spawn_error}"
                        ),
                    },
                    secrets,
                )

        if timed_out is not None:
            yield redact_object(
                {
                    "kind": "error",
                    "text": (
                        f"таймаут {timed_out:g} с: процесс {self.binary_name or self.provider} "
                        "прерван, ход не завершён"
                    ),
                },
                secrets,
            )
        elif spawn_error is None and exit_code not in (None, 0) and error_text is None:
            tail = f": {stderr_tail}" if stderr_tail else ""
            yield redact_object(
                {
                    "kind": "error",
                    "text": (
                        f"{self.binary_name or self.provider} завершился с кодом {exit_code}{tail}"
                    ),
                },
                secrets,
            )

        usage = ctx.usage
        if usage:
            yield redact_object({"kind": "usage", "usage": usage}, secrets)
        # При таймауте код возврата ничего не значит: процесс убит нами, а не завершился сам.
        yield {"kind": "turn.completed", "exit_code": None if timed_out is not None else exit_code}
