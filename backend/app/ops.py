"""Operations: recent logs for the UI, and the heartbeat (a scheduled check-in run).

The heartbeat is Riki's own initiative: every N minutes (set on the Heartbeat page) it runs your
checklist as a turn in the "Heartbeat" thread. It never gets approvals (nobody is there), so it can
look and suggest but not change anything.
"""

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING

from sqlalchemy import select

from .db import Message, Persona, Run, Thread, new_id, utcnow
from .tracing import redact

if TYPE_CHECKING:
    from .runtime import Runtime

log = logging.getLogger(__name__)


class LogBuffer(logging.Handler):
    """Keeps the last N log records in memory (redacted) for the System page."""

    def __init__(self, size: int = 500) -> None:
        super().__init__(level=logging.INFO)
        self.records: deque[dict] = deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING and not record.name.startswith("app"):
            return  # library INFO chatter isn't useful here
        text = record.getMessage()
        if record.exc_info:
            text += "\n" + logging.Formatter().formatException(record.exc_info).splitlines()[-1]
        self.records.append({"at": record.created, "level": record.levelname, "logger": record.name,
                             "message": redact(text)})


LOGS = LogBuffer()


async def own_thread(rt: "Runtime", kind: str, title: str) -> str:
    """The one thread of a kind Riki keeps for itself (check-ins, setup); created on first use."""
    async with rt.db.session() as s:
        thread = await s.scalar(select(Thread).where(Thread.kind == kind).limit(1))
        if thread is None:
            thread = Thread(title=title, kind=kind)
            s.add(thread)
            await s.commit()
        return thread.id


async def beat(rt: "Runtime", persona: Persona) -> str:
    """Run the heartbeat checklist once. Returns the run id."""
    from .turn import start_run  # turn imports runtime; avoid the cycle at import time

    prompt = persona.heartbeat_prompt.strip() or (
        "Heartbeat check-in. Look over what you know about me and anything time-sensitive. "
        "If something needs my attention, say what and why in 1-3 short bullets. If nothing does, say so in one line.")
    thread = await own_thread(rt, "heartbeat", "Heartbeat")
    if rt.active_run(thread):  # the last check-in is still going
        return ""
    ctx = start_run(rt, thread, prompt, source="heartbeat", learn=False)
    await rt.runs[ctx.run_id][1]
    return ctx.run_id


async def resume_waiting_runs(rt: "Runtime") -> int:
    """On startup: a run that was waiting for your OK when Riki last stopped is started again (its approval is still
    there, and anything it already did is not done twice; see agent/policy.py). Anything older than a day is closed."""
    from .turn import start_run

    cutoff = utcnow() - timedelta(hours=24)
    async with rt.db.session() as s:
        waiting = (await s.scalars(select(Run).where(Run.status == "waiting"))).all()
        resume, images = [], {}
        for r in waiting:
            fresh = r.source == "chat" and r.thread_id and r.created_at > cutoff
            r.status, r.error = "error", ("Riki restarted while this waited for you; it continues as a new run."
                                          if fresh else "Interrupted: waited too long, or was not a chat.")
            if fresh:
                resume.append(r)
                m = await s.scalar(select(Message).where(Message.run_id == r.id, Message.role == "user").limit(1))
                images[r.id] = (m.images or None) if m else None
        await s.commit()
    started = 0
    for r in resume:
        if rt.active_run(r.thread_id) is None:
            start_run(rt, r.thread_id, r.input, images=images.get(r.id), effort=r.effort, resume_of=r.id)
            started += 1
    return started


async def interrupt_stale_runs(db) -> None:
    """On startup: a run left 'running' by a previous process will never finish. Say so in its chat.
    (Runs left 'waiting' for you are different: resume_waiting_runs continues them.)"""
    async with db.session() as s:
        for run in (await s.scalars(select(Run).where(Run.status == "running"))).all():
            run.status, run.error = "error", "Interrupted: Riki restarted while this was running."
            if run.thread_id:
                s.add(Message(thread_id=run.thread_id, role="assistant", run_id=run.id,
                              content="I was interrupted when Riki restarted, so this didn't finish. Ask me again."))
        await s.commit()


def policy_shutting_down() -> bool:
    from .agent import policy

    return policy.SHUTTING_DOWN


def in_quiet_hours(hour: int, spec: str) -> bool:
    """"22-8" covers 22:00 to 07:59 (wraps midnight); "0-0" means never quiet."""
    start, end = (int(x) % 24 for x in spec.split("-"))
    return start <= hour < end if start <= end else hour >= start or hour < end


async def background_work(rt: "Runtime") -> None:
    """Riki thinking about you between chats: a fresh read, lessons from quiet chats, a thinking cycle. Each part
    decides for itself whether there is anything new (no input, no model call), and all pause when the day's budget
    is mostly spent."""
    from .cognition import learn_lessons, refresh_reflection, run_cycle
    from .spend import budget_state

    if await budget_state(rt) != "ok":
        return
    for name, job in (("reflection", refresh_reflection), ("lessons", learn_lessons), ("cycle", run_cycle)):
        try:
            await job(rt)
        except Exception:
            log.exception("background %s failed", name)


async def heartbeat_loop(rt: "Runtime", tick_s: float = 60) -> None:
    """Once a minute: schedules that came due, every few minutes the thinking work, and the check-in if one is due.
    Never dies on an error."""
    from .schedules import fire_due

    ticks, busy = 0, False

    async def think():
        nonlocal busy
        try:
            await background_work(rt)
        finally:
            busy = False

    while True:
        await asyncio.sleep(tick_s)
        ticks += 1
        try:
            await fire_due(rt)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("schedules failed")
        if ticks % 5 == 0 and not busy and not policy_shutting_down():
            busy = True
            rt.spawn(think())
        try:
            async with rt.db.session() as s:
                persona = await s.get(Persona, 1)
                last = await s.scalar(select(Run.created_at).where(Run.source == "heartbeat")
                                      .order_by(Run.created_at.desc()).limit(1))
            hour = datetime.now(ZoneInfo(rt.settings.timezone)).hour
            if persona and persona.heartbeat_minutes > 0 and not in_quiet_hours(hour, rt.settings.quiet_hours) and (
                    last is None or utcnow() - last >= timedelta(minutes=persona.heartbeat_minutes)):
                await beat(rt, persona)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("heartbeat failed")
