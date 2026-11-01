"""Reminders and scheduled tasks. "Remind me at 5", "every weekday at 9, tell me what's on today".

- remind: Riki tells you the text at that time (no model call).
- task: Riki does the task then (read-only: a scheduled run can look, never change anything) and tells you the result.
- repeat: none, daily, weekdays or weekly. Not cron: it covers nearly every personal use and has no parsing bugs.

The heartbeat loop (ops.py) checks every minute. A schedule that came due while Riki was off fires once when it is back,
marked late, and then continues from now.
"""

import logging
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from langchain_core.tools import tool
from sqlalchemy import select

from .agent.policy import SAFE
from .agent.tools import ToolEntry
from .db import Schedule, utcnow
from .notify import notify

log = logging.getLogger(__name__)

MAX_ACTIVE = 100
LATE_AFTER = timedelta(minutes=5)


def next_occurrence(prev: datetime, repeat: str, tz: str, now: datetime) -> datetime | None:
    """The next time after ``now`` that a repeating schedule fires, at the same local time of day; None if it doesn't repeat."""
    if repeat == "none":
        return None
    zone = ZoneInfo(tz)
    t = prev.astimezone(zone)
    while True:
        t = t + timedelta(days=7 if repeat == "weekly" else 1)
        if repeat == "weekdays" and t.weekday() >= 5:
            continue
        if t.astimezone(ZoneInfo("UTC")) > now:
            return t.astimezone(ZoneInfo("UTC"))


def parse_when(when: str, tz: str, now: datetime) -> datetime:
    """ISO text to an aware time; no zone means the user's own. Raises ValueError with a message for the model."""
    try:
        t = datetime.fromisoformat(when.strip())
    except ValueError:
        raise ValueError("`when` must be an ISO date and time like 2026-11-03T09:00 (use current_time to work out 'tomorrow').")
    if t.tzinfo is None:
        t = t.replace(tzinfo=ZoneInfo(tz))
    if t < now - timedelta(minutes=2):
        raise ValueError("That time has already passed.")
    if t > now + timedelta(days=366):
        raise ValueError("Too far ahead: schedule things within a year.")
    return t


def schedule_tools(rt) -> list[ToolEntry]:
    s = rt.settings

    def local(t: datetime) -> str:
        return t.astimezone(ZoneInfo(s.timezone)).strftime("%a %d %b %H:%M")

    @tool
    async def schedule(when: str, text: str, kind: Literal["remind", "task"] = "remind",
                       repeat: Literal["none", "daily", "weekdays", "weekly"] = "none") -> str:
        """Schedule a reminder or a task for later. `when`: ISO date and time in the user's timezone, e.g.
        2026-11-03T09:00 (call current_time first to work out "tomorrow" or "in 2 hours"). kind=remind: the user gets
        `text` as a message at that time. kind=task: at that time Riki does `text` (read-only: it can look things up but
        changes and sends nothing) and messages the user the result. repeat: none, daily, weekdays or weekly."""
        try:
            at = parse_when(when, s.timezone, utcnow())
        except ValueError as exc:
            return str(exc)
        async with rt.db.session() as db:
            active = len((await db.scalars(select(Schedule.id).where(Schedule.status == "active"))).all())
            if active >= MAX_ACTIVE:
                return f"There are already {MAX_ACTIVE} active schedules. Cancel some first."
            row = Schedule(kind=kind, text=text.strip()[:1000], next_at=at, repeat=repeat)
            db.add(row)
            await db.commit()
        again = "" if repeat == "none" else f", then {repeat}"
        return f"Scheduled {kind} (id {row.id[:8]}) for {local(at)}{again}: {text.strip()[:80]}"

    @tool
    async def list_schedules() -> str:
        """The user's active reminders and scheduled tasks, soonest first."""
        async with rt.db.session() as db:
            rows = (await db.scalars(select(Schedule).where(Schedule.status == "active")
                                     .order_by(Schedule.next_at).limit(50))).all()
        return "\n".join(f"- {r.id[:8]} {local(r.next_at)} [{r.kind}{'' if r.repeat == 'none' else ', ' + r.repeat}] {r.text[:100]}"
                         for r in rows) or "Nothing scheduled."

    @tool
    async def cancel_schedule(id: str) -> str:
        """Cancel a reminder or scheduled task by its id (the first 8 characters from list_schedules are enough)."""
        async with rt.db.session() as db:
            rows = (await db.scalars(select(Schedule).where(Schedule.status == "active", Schedule.id.startswith(id.strip())))).all()
            if len(rows) != 1:
                return "No single active schedule matches that id." if rows else "No active schedule with that id."
            rows[0].status = "cancelled"
            await db.commit()
        return f"Cancelled: {rows[0].text[:80]}"

    return [ToolEntry(schedule, SAFE), ToolEntry(list_schedules, SAFE), ToolEntry(cancel_schedule, SAFE)]


async def fire_due(rt) -> int:
    """Claim what is due (so two ticks never fire the same one), advance repeating ones, then deliver. Returns how many."""
    from .ops import own_thread
    from .spend import budget_state
    from .turn import start_run

    now = utcnow()
    due: list[tuple[str, str, str, bool]] = []  # id, kind, text, late
    async with rt.db.session() as db:
        rows = (await db.scalars(select(Schedule).where(Schedule.status == "active", Schedule.next_at <= now)
                                 .order_by(Schedule.next_at).limit(5).with_for_update(skip_locked=True))).all()
        for r in rows:
            due.append((r.id, r.kind, r.text, now - r.next_at > LATE_AFTER))
            nxt = next_occurrence(r.next_at, r.repeat, rt.settings.timezone, now)
            r.last_fired_at = now
            if nxt is None:
                r.status = "done"
            else:
                r.next_at = nxt
        await db.commit()
    for sid, kind, text, late in due:
        try:
            if kind == "remind":
                await notify(rt, f"Reminder{' (late)' if late else ''}: {text}")
            elif await budget_state(rt) == "hard":
                await notify(rt, f"I skipped the scheduled task \"{text[:80]}\": today's budget is used up.")
            else:
                thread = await own_thread(rt, "heartbeat", "Check-ins")
                ctx = start_run(rt, thread, f"Scheduled task: {text}", source="schedule", learn=False, effort="low")
                rt.spawn(_deliver_task(rt, ctx, sid))
        except Exception:
            log.exception("could not fire schedule %s", sid)
    return len(due)


async def _deliver_task(rt, ctx, schedule_id: str) -> None:
    """A task's reply is already in the check-in thread (the app shows it); this sends it to the phone."""
    output = await rt.runs[ctx.run_id][1]
    async with rt.db.session() as db:
        row = await db.get(Schedule, schedule_id)
        if row is not None:
            row.last_run_id = ctx.run_id
            await db.commit()
    await notify(rt, output, run_id=ctx.run_id, in_app=False)
