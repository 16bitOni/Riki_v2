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


async def heartbeat_thread(rt: "Runtime") -> str:
    async with rt.db.session() as s:
        thread = await s.scalar(select(Thread).where(Thread.kind == "heartbeat").limit(1))
        if thread is None:
            thread = Thread(title="Heartbeat", kind="heartbeat")
            s.add(thread)
            await s.commit()
        return thread.id


async def beat(rt: "Runtime", persona: Persona) -> str:
    """Run the heartbeat checklist once. Returns the run id."""
    from .turn import start_run  # turn imports runtime; avoid the cycle at import time

    prompt = persona.heartbeat_prompt.strip() or (
        "Heartbeat check-in. Look over what you know about me and anything time-sensitive. "
        "If something needs my attention, say what and why in 1-3 short bullets. If nothing does, say so in one line.")
    thread = await heartbeat_thread(rt)
    if rt.active_run(thread):  # the last check-in is still going
        return ""
    ctx = start_run(rt, thread, prompt, source="heartbeat", learn=False)
    await rt.runs[ctx.run_id][1]
    return ctx.run_id


async def interrupt_stale_runs(db) -> None:
    """On startup: a run left 'running' by a previous process will never finish. Say so in its chat."""
    async with db.session() as s:
        for run in (await s.scalars(select(Run).where(Run.status == "running"))).all():
            run.status, run.error = "error", "Interrupted: Riki restarted while this was running."
            if run.thread_id:
                s.add(Message(thread_id=run.thread_id, role="assistant", run_id=run.id,
                              content="I was interrupted when Riki restarted, so this didn't finish. Ask me again."))
        await s.commit()


def in_quiet_hours(hour: int, spec: str) -> bool:
    """"22-8" covers 22:00 to 07:59 (wraps midnight); "0-0" means never quiet."""
    start, end = (int(x) % 24 for x in spec.split("-"))
    return start <= hour < end if start <= end else hour >= start or hour < end


async def heartbeat_loop(rt: "Runtime", tick_s: float = 60) -> None:
    """Checks once a minute whether a heartbeat is due. Never dies on an error."""
    while True:
        await asyncio.sleep(tick_s)
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
