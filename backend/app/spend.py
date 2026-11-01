"""Today's spend against the daily budget (RIKI_DAILY_BUDGET_USD). Only runs with a price count (see RIKI_PRICES).

ok     under 80%: everything runs.
soft   80% or more: background work (reflection, lessons, thinking cycles, scheduled tasks) pauses.
hard   100% or more: chat answers at low effort, and says so once a day.
"""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from .tenancy import who

from .db import Run

_CACHE: dict[str, tuple[float, float]] = {}  # day -> (when read, dollars); the sum is cheap but not free
SOFT, HARD = 0.8, 1.0


async def spend_today(rt) -> float:
    tz = ZoneInfo(rt.settings.timezone)
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    key = start.isoformat()
    cached = _CACHE.get(key)
    if cached and time.monotonic() - cached[0] < 30:
        return cached[1]
    async with rt.db.session() as s:
        total = float(await s.scalar(select(func.coalesce(func.sum(Run.cost_usd), 0)).where(Run.created_at >= start)) or 0)
    _CACHE.clear()
    _CACHE[key] = (time.monotonic(), total)
    return total


def state_for(spent: float, budget: float | None) -> str:
    if not budget:
        return "ok"
    return "hard" if spent >= budget * HARD else "soft" if spent >= budget * SOFT else "ok"


async def budget_state(rt) -> str:
    person = who()
    budget = rt.settings.member_daily_budget_usd if person and person.member else rt.settings.daily_budget_usd
    return state_for(await spend_today(rt), budget) if budget else "ok"
