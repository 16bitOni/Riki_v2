"""Who may do what. Every tool has a risk tier; the tier, not the model, decides.

- safe:     runs immediately (reads, Riki's own memory).
- write:    changes something outside Riki; waits for the user's approval in the UI.
- external: contacts or acts toward other people; always refused (hard rule).

Unknown tools default to ``write``. Only chat runs can be approved; evals and heartbeats never are.
"""

import asyncio
import logging

from sqlalchemy import update

from ..db import Approval, Database, utcnow
from ..tracing import RUN, redact

log = logging.getLogger(__name__)

SAFE, WRITE, EXTERNAL = "safe", "write", "external"
RISKS = (SAFE, WRITE, EXTERNAL)

# Approval futures live only in this process: a restart denies whatever was pending (fail closed).
_PENDING: dict[str, asyncio.Future[bool]] = {}


async def authorize(db: Database, risk: str, tool: str, args: dict, span_id: str,
                    timeout_s: float) -> tuple[bool, str]:
    """Return (allowed, reason shown to the model when not allowed)."""
    if risk == SAFE:
        return True, ""
    if risk == EXTERNAL:
        return False, "Refused by policy: Riki never contacts or acts toward other people."
    ctx = RUN.get()
    if ctx.source != "chat":  # evals and heartbeats run with nobody there to approve
        return False, f"Refused: nobody is here to approve changes during a {ctx.source} run. Suggest it instead."

    approval = Approval(run_id=ctx.run_id, span_id=span_id, tool=tool, args=redact(args))
    async with db.session() as s:
        s.add(approval)
        await s.commit()
    future = asyncio.get_running_loop().create_future()
    _PENDING[approval.id] = future
    ctx.emit("approval", approval={"id": approval.id, "span_id": span_id, "tool": tool,
                                   "args": approval.args, "timeout_s": timeout_s})
    status = "expired"  # also what a Stop (cancellation) leaves behind
    try:
        allowed = await asyncio.wait_for(future, timeout_s)
        status = "approved" if allowed else "denied"
    except TimeoutError:
        allowed = False
    finally:  # runs on cancellation too, so a stopped run never leaves a card nobody can answer
        _PENDING.pop(approval.id, None)
        async with db.session() as s:
            await s.execute(update(Approval).where(Approval.id == approval.id)
                            .values(status=status, decided_at=utcnow()))
            await s.commit()
        ctx.emit("approval_done", approval={"id": approval.id, "status": status})
    return allowed, "" if allowed else f"The user did not approve this action ({status}). Do not retry it."


def decide(approval_id: str, approve: bool) -> bool:
    """Resolve a pending approval. False if it is no longer waiting."""
    future = _PENDING.get(approval_id)
    if future is None or future.done():
        return False
    future.set_result(approve)
    return True


async def expire_stale(db: Database) -> None:
    """On startup: approvals left pending by a previous process can never be answered."""
    async with db.session() as s:
        await s.execute(update(Approval).where(Approval.status == "pending")
                        .values(status="expired", decided_at=utcnow()))
        await s.commit()
