"""Межагентные каналы (v1.8): передача задачи от агента к агенту.

Честные границы v1:
- передача — это запись в журнал целевого агента (сессия, системное сообщение,
  событие `task.received`); никаких скрытых ходов;
- целевой агент выполняет задачу только при явном `run=True` (CLI `--run`), и этот
  ход проходит через все ворота обычного `ask` — доступность, авторизацию, ворота
  политик и песочницу целевого агента;
- отправителю остаётся след `task.sent` в его открытой сессии, если она есть;
- передача самому себе запрещена: петля — не канал.

CLI: `runneos-agents send FROM TO PROMPT [--run] [--new] [--json]`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from .model import TurnResult
from .orchestrator import Orchestrator
from .store import Store

__all__ = ["HandoffResult", "delegate"]


@dataclass(frozen=True)
class HandoffResult:
    """Итог передачи: что записано и (при `run`) чем закончился ход целевого агента."""

    ok: bool
    handoff_id: str
    target_session_id: str
    from_agent: str
    to_agent: str
    error: str | None = None
    turn: TurnResult | None = None


def delegate(
    store: Store,
    orchestrator: Orchestrator,
    *,
    from_agent: str,
    to_agent: str,
    prompt: str,
    run: bool = False,
    new_session: bool = False,
    model: str | None = None,
    timeout: float | None = None,
) -> HandoffResult:
    """Передать задачу целевому агенту; при `run` — выполнить его ход через ворота."""
    from_id = str(from_agent or "").strip()
    to_id = str(to_agent or "").strip()
    if not str(prompt or "").strip():
        return _fail("", from_id, to_id, "", "пустая задача")
    if not from_id or not to_id:
        return _fail("", from_id, to_id, "", "нужны оба агента: FROM и TO")
    if from_id == to_id:
        return _fail("", from_id, to_id, "", "нельзя передать задачу самому себе")
    if store.get_agent(from_id) is None:
        return _fail("", from_id, to_id, "", f"неизвестный агент-отправитель: {from_id}")
    if store.get_agent(to_id) is None:
        return _fail("", from_id, to_id, "", f"неизвестный целевой агент: {to_id}")

    text = str(prompt).strip()
    handoff_id = uuid.uuid4().hex[:12]
    session = store.latest_open_session(to_id) if not new_session else None
    if session is None:
        session = store.create_session(to_id, title=f"Задача от {from_id}")
    event = store.append_event(
        session.id,
        "task.received",
        {"from": from_id, "text": text, "handoff_id": handoff_id},
    )
    store.add_message(
        session.id,
        "system",
        f"Задача передана агентом {from_id} (handoff {handoff_id}): {text}",
        event_id=event.id,
    )
    sender_session = store.latest_open_session(from_id)
    if sender_session is not None:
        store.append_event(
            sender_session.id,
            "task.sent",
            {"to": to_id, "text": text, "handoff_id": handoff_id},
        )

    turn: TurnResult | None = None
    if run:
        turn = orchestrator.ask(
            to_id,
            f"[задача от {from_id}] {text}",
            session_id=session.id,
            model=model,
            timeout=timeout,
            new_session=False,
        )
        return HandoffResult(
            ok=turn.ok,
            handoff_id=handoff_id,
            target_session_id=session.id,
            from_agent=from_id,
            to_agent=to_id,
            error=turn.error,
            turn=turn,
        )
    return HandoffResult(
        ok=True,
        handoff_id=handoff_id,
        target_session_id=session.id,
        from_agent=from_id,
        to_agent=to_id,
    )


def _fail(
    handoff_id: str, from_id: str, to_id: str, session_id: str, error: str
) -> HandoffResult:
    return HandoffResult(
        ok=False,
        handoff_id=handoff_id,
        target_session_id=session_id,
        from_agent=from_id,
        to_agent=to_id,
        error=error,
    )
