"""Оркестратор: один ход агента от постановки задачи до записанного результата.

Порядок зафиксирован контрактом (INTERFACE.md, раздел 6): событие в журнал → запуск
провайдера → нормализованные события в журнал и в ленту → идентификатор сессии провайдера
→ честный итог. Наружу исключения не летят: ошибка становится TurnResult(ok=False).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .model import (
    Agent,
    Authorization,
    Availability,
    Event,
    ProviderUnavailable,
    TurnResult,
    now_iso,
)
from .policy import load_policy
from .routing import AdapterRegistry, default_registry, resolve_workspace_path
from .sandbox import load_sandbox
from .store import Store
from .workspace import Workspace, ensure_workspace

__all__ = ["Orchestrator", "RunLog", "run_turn"]

#: Виды нормализованных событий, которые попадают в ленту сообщений.
_MESSAGE_KINDS = {"agent_message"}
#: Виды событий, которые считаются «ходом» для финального текста.
_TEXT_KINDS = ("agent_message",)

#: Ключи, значения которых никогда не попадают в файловый журнал прогона.
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")

#: Ограничение длины строки в файловом журнале, чтобы он не разрастался.
_RUN_LOG_VALUE_LIMIT = 4000


class RunLog:
    """Журнал прогона в воркспейсе агента: `<workspace>/runs/<session>.jsonl`.

    База — источник правды; файл рядом с работой агента нужен, чтобы человек мог открыть
    историю прогона без SQLite. Пишется построчно и сразу сбрасывается на диск.
    Если запись невозможна (нет прав, диск), прогон продолжается: журнал — удобство,
    а не условие работы.
    """

    def __init__(self, workspace: Path, session_id: str) -> None:
        self.path = Path(workspace) / "runs" / f"{session_id}.jsonl"

    def write(self, event: Event) -> bool:
        """Дописать событие. False — записать не удалось (не ошибка прогона)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                {
                    "seq": event.seq,
                    "at": event.created_at,
                    "kind": event.kind,
                    "payload": _sanitize_payload(event.payload),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            return False
        return True


class Orchestrator:
    """Связка хранилища, реестра адаптеров и воркспейсов.

    Хранилище переданное снаружи не закрывается: владелец — вызывающий.
    """

    def __init__(self, store: Store, adapters: AdapterRegistry | None = None) -> None:
        self.store = store
        self.adapters = adapters if adapters is not None else default_registry()

    # --- публичное -----------------------------------------------------

    def providers(self) -> list[Availability]:
        """Доступность провайдеров для CLI и панели."""
        return self.adapters.availability()

    def availability(self, provider: str) -> Availability:
        try:
            return self.adapters.get(provider).availability()  # type: ignore[attr-defined]
        except Exception as error:
            return Availability(
                provider=provider,
                available=False,
                reason=f"ошибка проверки доступности: {type(error).__name__}",
            )

    def authorization(self, provider: str) -> Authorization:
        """Авторизация провайдера глазами его адаптера; внешние адаптеры без метода —
        честное «неизвестно», а не догадка."""
        adapter = self.adapters.get(provider)
        method = getattr(adapter, "authorization", None)
        if method is None:
            return Authorization(
                provider=provider,
                authorized=None,
                detail="адаптер не сообщает состояние авторизации",
            )
        try:
            return method()
        except Exception as error:  # noqa: BLE001 — состояние не должно ронять ход
            return Authorization(
                provider=provider,
                authorized=None,
                detail=f"ошибка проверки авторизации: {type(error).__name__}",
            )

    def ask(
        self,
        agent_id: str,
        prompt: str,
        *,
        session_id: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        resume: bool = True,
        new_session: bool = False,
        should_cancel=None,
    ) -> TurnResult:
        """Поставить задачу агенту и дождаться итога одного хода.

        `new_session=True` всегда начинает новую сессию (флаг `--new` в CLI),
        даже если у агента есть открытая. `should_cancel` (v1.10) — колбэк без
        аргументов: истина означает «ход больше не нужен» — служба очереди так
        отменяет задачу; стрим закрывается (процесс провайдера гасится в finally),
        ход завершается честной ошибкой «отменено».
        """
        agent = self.store.get_agent(agent_id)
        if agent is None:
            return TurnResult(ok=False, session_id=session_id or "", error=f"неизвестный агент: {agent_id}")
        if not str(prompt or "").strip():
            return TurnResult(ok=False, session_id=session_id or "", error="пустая задача")

        try:
            session = self._resolve_session(agent, session_id, new_session=new_session)
        except ValueError as error:
            return TurnResult(ok=False, session_id=session_id or "", error=str(error))

        # Воркспейс и файловый журнал готовим сразу: даже честный отказ должен быть записан.
        workspace = self._prepare_workspace(agent)
        run_log = RunLog(workspace, session.id)

        # Событие и сообщение пользователя — до запуска процесса.
        event = self._record(session.id, "user_message", {"text": prompt}, run_log)
        self.store.add_message(session.id, "user", prompt, event_id=event.id)

        availability = self.availability(agent.provider)
        if not availability.available:
            reason = availability.reason or f"провайдер {agent.provider} недоступен"
            self._record(
                session.id,
                "error",
                {"text": reason, "provider": agent.provider, "stage": "availability"},
                run_log,
            )
            self.store.add_message(session.id, "system", reason)
            self.store.set_agent_state(agent.id, "unavailable")
            return TurnResult(
                ok=False,
                session_id=session.id,
                provider_session_id=session.provider_session_id,
                error=reason,
            )

        # Авторизация (INTERFACE v1.4): бинарь есть, но вход может быть не выполнен.
        # Отказ честный, без запуска процесса; unknown не считается отказом.
        auth = self.authorization(agent.provider)
        if auth.authorized is False:
            how = auth.how or f"{agent.provider} login"
            reason = f"провайдер {agent.provider} не авторизован: выполните {how}"
            if auth.detail:
                reason += f" ({auth.detail})"
            self._record(
                session.id,
                "error",
                {"text": reason, "provider": agent.provider, "stage": "authorization", "how": how},
                run_log,
            )
            self.store.add_message(session.id, "system", reason)
            self.store.set_agent_state(agent.id, "unavailable")
            return TurnResult(
                ok=False,
                session_id=session.id,
                provider_session_id=session.provider_session_id,
                error=reason,
            )

        # Ворота политик (INTERFACE v1.3, семантика A): решение о запуске провайдера
        # вычисляется и журналируется до старта процесса. По умолчанию — только аудит;
        # принуждение включают явно (`"enforce": true` в секции policy манифеста),
        # сломанная секция запрещает ход при любом enforce.
        loaded_policy = load_policy(workspace, agent.provider)
        decision = loaded_policy.policy.check_provider(agent.provider)
        gate_payload: dict = {
            "action": "provider-run",
            "provider": agent.provider,
            "allowed": decision.allowed,
            "rule": decision.rule,
            "reason": decision.reason,
            "enforce": loaded_policy.enforce,
            "source": loaded_policy.source,
        }
        if loaded_policy.error:
            gate_payload["policy_error"] = loaded_policy.error
        self._record(session.id, "policy.decision", gate_payload, run_log)
        if loaded_policy.enforce and not decision.allowed:
            refusal = f"ход отклонён политикой: правило {decision.rule} — {decision.reason}"
            if loaded_policy.error:
                refusal += f"; политика сломана: {loaded_policy.error}"
            self._record(session.id, "error", {"text": refusal, "stage": "policy"}, run_log)
            self.store.add_message(session.id, "system", refusal)
            return TurnResult(
                ok=False,
                session_id=session.id,
                provider_session_id=session.provider_session_id,
                error=refusal,
            )

        # Песочница (INTERFACE v1.5): секция `sandbox` манифеста включает лимиты
        # systemd-run --scope для этого хода. Сломанная секция запрещает ход,
        # неопознанный режим — тоже: непонятная песочница не лучше её отсутствия.
        loaded_sandbox = load_sandbox(workspace)
        if loaded_sandbox.error:
            refusal = f"ход не начат: секция sandbox сломана — {loaded_sandbox.error}"
            self._record(session.id, "error", {"text": refusal, "stage": "sandbox"}, run_log)
            self.store.add_message(session.id, "system", refusal)
            return TurnResult(
                ok=False,
                session_id=session.id,
                provider_session_id=session.provider_session_id,
                error=refusal,
            )
        sandbox_prefix = None
        if loaded_sandbox.enabled():
            # Слой bwrap (v1.6) монтирует rw воркспейс и каталоги провайдера поверх
            # read-only корня; список каталогов знает адаптер.
            adapter = self.adapters.get(agent.provider)
            dirs_method = getattr(adapter, "data_dirs", None)
            try:
                data_dirs = list(dirs_method()) if dirs_method else []
            except Exception as error:  # noqa: BLE001 — состояние не должно ронять ход
                data_dirs = []
                self._record(
                    session.id,
                    "error",
                    {
                        "text": f"не удалось получить каталоги провайдера: {type(error).__name__}",
                        "stage": "sandbox",
                    },
                    run_log,
                )
            sandbox_prefix = loaded_sandbox.argv_prefix(workspace=workspace, data_dirs=data_dirs)

        self.store.set_agent_state(agent.id, "running")
        self.store.set_session_state(session.id, "open")

        collected: list[dict] = []
        provider_session_id = session.provider_session_id
        error_text: str | None = None
        usage: dict | None = None
        transient_errors: list[str] = []
        canceled = False

        try:
            stream = self._open_stream(
                agent,
                workspace,
                prompt,
                session,
                model=model,
                timeout=timeout,
                resume=resume,
                sandbox_prefix=sandbox_prefix,
            )
            for raw in stream:
                if should_cancel is not None and should_cancel():
                    canceled = True
                    error_text = "ход отменён: задача отменена пользователем"
                    self._record(
                        session.id,
                        "error",
                        {"text": error_text, "stage": "canceled"},
                        run_log,
                    )
                    break
                event_dict = dict(raw or {})
                kind = str(event_dict.pop("kind", "raw"))
                stored = self._record(session.id, kind, event_dict, run_log)
                collected.append({"kind": kind, **event_dict})
                if kind in _MESSAGE_KINDS and event_dict.get("text"):
                    self.store.add_message(
                        session.id, "agent", str(event_dict["text"]), event_id=stored.id
                    )
                if kind == "provider_session" and event_dict.get("provider_session_id"):
                    provider_session_id = str(event_dict["provider_session_id"])
                    self.store.set_provider_session_id(session.id, provider_session_id)
                if kind == "usage" and isinstance(event_dict.get("usage"), dict):
                    usage = dict(event_dict["usage"])
                if kind == "error":
                    message = str(event_dict.get("text") or "провайдер сообщил об ошибке")
                    if event_dict.get("transient"):
                        # Провайдер сам сообщил о повторе и может ещё ответить: это не провал.
                        transient_errors.append(message)
                    else:
                        error_text = message
        except ProviderUnavailable as error:
            error_text = error.reason
            self._record(session.id, "error", {"text": error_text, "stage": "start"}, run_log)
            self.store.add_message(session.id, "system", error_text)
        except KeyboardInterrupt:
            self.store.set_agent_state(agent.id, "idle")
            raise
        except Exception as error:  # адаптер сломался — это наша ошибка, но не трассировка в UI
            error_text = f"сбой провайдера: {type(error).__name__}: {error}"
            self._record(session.id, "error", {"text": error_text, "stage": "stream"}, run_log)
            self.store.add_message(session.id, "system", error_text)

        text = "".join(
            str(item.get("text") or "") for item in collected if item.get("kind") in _TEXT_KINDS
        ).strip()
        exit_code = _last_exit_code(collected)
        if canceled:
            outcome = "canceled"
        else:
            outcome, error_text = _decide_outcome(
            text=text,
            error_text=error_text,
            exit_code=exit_code,
            transient_errors=transient_errors,
        )

        self._record(
            session.id,
            "turn.finished",
            {
                "ok": outcome == "ok",
                "outcome": outcome,
                "text": text,
                "usage": usage,
                "provider_session_id": provider_session_id,
                "exit_code": exit_code,
                "transient_errors": transient_errors,
                "error": error_text,
            },
            run_log,
        )
        if outcome == "canceled":
            self.store.set_agent_state(agent.id, "idle")
            self.store.set_session_state(session.id, "error")
        else:
            self.store.set_agent_state(agent.id, "idle" if outcome == "ok" else "error")
            if outcome == "ok":
                self.store.touch_session(session.id)
            else:
                self.store.set_session_state(session.id, "error")

        return TurnResult(
            ok=outcome == "ok",
            session_id=session.id,
            provider_session_id=provider_session_id,
            text=text,
            error=error_text,
            usage=usage,
            exit_code=exit_code,
        )

    # --- внутреннее ----------------------------------------------------

    def _resolve_session(self, agent: Agent, session_id: str | None, *, new_session: bool = False):
        if session_id:
            session = self.store.get_session(session_id)
            if session is None:
                raise ValueError(f"неизвестная сессия: {session_id}")
            if session.agent_id != agent.id:
                raise ValueError("сессия принадлежит другому агенту")
            return session
        if new_session:
            return self.store.create_session(agent.id, title=_session_title())
        existing = self.store.latest_open_session(agent.id)
        if existing is not None:
            return existing
        return self.store.create_session(agent.id, title=_session_title())

    def _prepare_workspace(self, agent: Agent) -> Path:
        workspace = resolve_workspace_path(agent, self.store.root)
        ensure_workspace(agent.replace(workspace=workspace), self.store.root)
        if agent.workspace != workspace:
            # Путь воркспейса — часть состояния агента: без записи следующая сессия
            # не нашла бы воркспейс, а doctor показал бы его отсутствующим.
            self.store.upsert_agent(agent.replace(workspace=workspace))
        return workspace

    def _record(
        self, session_id: str, kind: str, payload: dict, run_log: "RunLog | None" = None
    ) -> Event:
        """Событие сначала в базу, потом в файловый журнал воркспейса."""
        event = self.store.append_event(session_id, kind, payload)
        if run_log is not None:
            run_log.write(event)
        return event

    def _open_stream(
        self,
        agent: Agent,
        workspace: Path,
        prompt: str,
        session,
        *,
        model: str | None,
        timeout: float | None,
        resume: bool,
        sandbox_prefix: list[str] | None = None,
    ) -> Iterator[dict]:
        adapter = self.adapters.get(agent.provider)
        can_resume = bool(resume and session.provider_session_id)
        if can_resume:
            return adapter.resume(  # type: ignore[attr-defined]
                workspace,
                session.provider_session_id,
                prompt,
                model=model,
                timeout=timeout,
                sandbox_prefix=sandbox_prefix,
            )
        return adapter.start(  # type: ignore[attr-defined]
            workspace,
            prompt,
            model=model,
            instructions=agent.instructions,
            timeout=timeout,
            sandbox_prefix=sandbox_prefix,
        )



def _decide_outcome(
    *, text: str, error_text: str | None, exit_code: int | None, transient_errors: list[str]
) -> tuple[str, str | None]:
    """Итог хода по фактам, а не по последнему сообщению.

    Правила (наблюдение 2026-10-12: codex при обрыве потока печатает «Reconnecting... N/M»,
    а затем успешно отвечает):
    - постоянная ошибка (конверт `turn.failed`, отказ старта, сбой адаптера) — провал;
    - ответ получен — ход успешен, даже если по пути были временные сообщения;
    - ненулевой код возврата без ответа — провал;
    - ответа нет и ошибок не было — провайдер завершился молча.
    """
    if error_text:
        return "error", error_text
    if text:
        return "ok", None
    if exit_code not in (None, 0):
        return "error", f"провайдер завершился с кодом {exit_code} и не дал ответа"
    return "error", "провайдер завершился, не дав ответа"

def run_turn(store: Store, agent_id: str, prompt: str, **kwargs: Any) -> TurnResult:
    """Удобная обёртка: один ход без создания Orchestrator вручную."""
    return Orchestrator(store).ask(agent_id, prompt, **kwargs)


def _session_title() -> str:
    return "Сессия " + time.strftime("%Y-%m-%d %H:%M", time.localtime())


def _last_exit_code(collected: list[dict]) -> int | None:
    for item in reversed(collected):
        if item.get("exit_code") is not None:
            try:
                return int(item["exit_code"])
            except (TypeError, ValueError):
                return None
    return None


def _sanitize_payload(payload: dict, depth: int = 0) -> dict:
    """Убрать из payload всё похожее на секрет и обрезать слишком длинные строки.

    Файловый журнал читает человек, и он остаётся на диске: значения ключей вида
    `*_KEY`, `*_TOKEN`, `authorization` заменяются, длинные тексты укорачиваются.
    В SQLite payload пишется как есть — там редакция идёт по значению, в адаптерах.
    """
    result: dict = {}
    for key, value in (payload or {}).items():
        name = str(key)
        if any(marker in name.upper() for marker in _SECRET_MARKERS):
            result[name] = "<скрыто>"
            continue
        result[name] = _sanitize_value(value, depth)
    return result


def _sanitize_value(value: object, depth: int) -> object:
    if isinstance(value, dict):
        if depth > 4:  # защита от неожиданной глубины
            return "<слишком глубоко>"
        return _sanitize_payload(value, depth + 1)
    if isinstance(value, list):
        return [_sanitize_value(item, depth) for item in value[:200]]
    if isinstance(value, str) and len(value) > _RUN_LOG_VALUE_LIMIT:
        return value[:_RUN_LOG_VALUE_LIMIT] + f"… [обрезано, всего {len(value)} символов]"
    return value
