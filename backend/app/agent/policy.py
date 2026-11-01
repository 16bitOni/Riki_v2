"""Who may do what. Every tool has a risk tier; the tier, not the model, decides.

- safe:     runs immediately (reads, Riki's own memory).
- write:    changes something outside Riki; waits for the user's approval (in the app or on the phone).
- external: contacts or acts toward other people; always refused (hard rule).

Unknown tools default to ``write``. Only chat runs can be approved; evals, heartbeats and schedules never are.

An approval is a database row, so it outlives the run that asked: you can answer hours later from any device, and if
Riki restarts while it waits, the run starts again and finds the same row instead of asking twice (a change that
already ran is never run a second time: its result is stored and handed back). "Allow for 30 minutes" is a grant on
the chat and the tool. Tools that move money are never granted. ``ask_user`` uses the same mechanism for a question.
"""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Awaitable, Callable

from sqlalchemy import select, update

from ..db import Approval, ApprovalGrant, Database, Run, utcnow
from ..tracing import RUN, redact

log = logging.getLogger(__name__)

SAFE, WRITE, EXTERNAL = "safe", "write", "external"
RISKS = (SAFE, WRITE, EXTERNAL)
GRANT_MINUTES = 30
ASK = "ask_user"

# Futures live only in this process; the database row is the truth (a decision made while no run is waiting still counts).
_PENDING: dict[str, asyncio.Future[bool]] = {}
# Called with the approval dict when a new request is waiting (the phone channel sends buttons from here).
NOTIFIERS: list[Callable[[dict], Awaitable[None]]] = []
# Set while the app shuts down: waiting runs are left waiting, so the next start can pick them up.
SHUTTING_DOWN = False


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    approval_id: str | None = None  # record the outcome on this row once the tool has run
    memo: str | None = None  # the action already happened in an earlier attempt: this is what it returned


_MONEY_WORDS = {"transfer", "withdraw", "pay", "payment", "payout", "buy", "sell", "purchase", "checkout", "trade",
                "invest", "redeem", "subscribe"}
_READ_START = {"get", "list", "search", "read", "fetch", "find", "lookup", "describe", "query", "show", "check", "view"}


def moves_money(name: str) -> bool:
    """place_order, withdraw_funds, createPayment, checkout...: a tool that spends or moves money. Whatever its app says,
    it is never auto-run, never covered by a grant, and its card says so. get_orders / list_payments only read."""
    w = [x for x in re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()) if x]
    if not w or w[0] in _READ_START:
        return False
    return bool(_MONEY_WORDS & set(w)) or ("order" in w and w[0] in {"place", "create", "modify", "submit", "cancel"})


def args_hash(tool: str, args: dict) -> str:
    return hashlib.sha256(json.dumps([tool, args], sort_keys=True, default=str).encode()).hexdigest()


def _denied(status: str) -> str:
    return f"The user did not approve this action ({status}). Do not retry it."


async def _set_run_status(db: Database, run_id: str, frm: str, to: str) -> None:
    async with db.session() as s:
        await s.execute(update(Run).where(Run.id == run_id, Run.status == frm).values(status=to))
        await s.commit()


async def _obtain(db: Database, tool: str, args: dict, span_id: str, timeout_s: float, *, money: bool,
                  grantable: bool) -> tuple[Approval, bool]:
    """The approval row for this request and whether it is approved. Looks first for a live grant, then for the same
    request made by this run or the run it continues (after a restart), then asks."""
    ctx = RUN.get()
    key, now = args_hash(tool, args), utcnow()
    lineage = [ctx.run_id, *ctx.lineage]
    created = False
    async with db.session() as s:
        if grantable and not money and ctx.thread_id:
            grant = await s.scalar(select(ApprovalGrant).where(
                ApprovalGrant.thread_id == ctx.thread_id, ApprovalGrant.tool == tool, ApprovalGrant.expires_at > now)
                .limit(1))
            if grant:
                row = Approval(run_id=ctx.run_id, span_id=span_id, tool=tool, args=redact(args), status="approved",
                               decided_at=now, via="grant", thread_id=ctx.thread_id, args_hash=key, money=money)
                s.add(row)
                await s.commit()
                return row, True
        row = await s.scalar(select(Approval).where(
            Approval.run_id.in_(lineage), Approval.tool == tool, Approval.args_hash == key,
            Approval.status != "expired").order_by(Approval.created_at.desc()).limit(1))
        if row is not None and row.status == "pending":
            row.run_id, row.span_id = ctx.run_id, span_id  # the request the restarted run is now making
            await s.commit()
        elif row is not None:
            return row, row.status == "approved"
        else:
            row = Approval(run_id=ctx.run_id, span_id=span_id, tool=tool, args=redact(args), thread_id=ctx.thread_id,
                           args_hash=key, money=money, expires_at=now + timedelta(seconds=timeout_s))
            s.add(row)
            await s.commit()
            created = True
    # Order matters: the run is marked waiting and the future exists BEFORE anyone is told, so an answer (or a shutdown)
    # arriving the instant the card appears finds everything in place. All of it sits inside the try, so a Stop at any
    # point still cleans up.
    status = "expired"  # also what a Stop (cancellation) leaves behind
    try:
        await _set_run_status(db, ctx.run_id, "running", "waiting")
        ctx.parked = True
        future = asyncio.get_running_loop().create_future()
        _PENDING[row.id] = future
        async with db.session() as s:  # answered while the run was being parked?
            current = await s.get(Approval, row.id)
        if current is not None and current.status != "pending" and not future.done():
            future.set_result(current.status == "approved")
        event = {"id": row.id, "span_id": span_id, "tool": tool, "args": row.args, "timeout_s": timeout_s, "money": money}
        ctx.emit("approval", approval=event)
        if created:
            for notify in NOTIFIERS:
                asyncio.get_running_loop().create_task(_safely(notify, event))
        status = "approved" if await asyncio.wait_for(future, timeout_s) else "denied"
    except TimeoutError:
        pass
    finally:  # runs on cancellation too, so a stopped run never leaves a card nobody can answer
        _PENDING.pop(row.id, None)
        ctx.parked = False
        if SHUTTING_DOWN and status == "expired":
            ctx.left_waiting = True  # left pending: the next start resumes this run and this very row
        else:
            if status == "expired":
                async with db.session() as s:
                    await s.execute(update(Approval).where(Approval.id == row.id, Approval.status == "pending")
                                    .values(status="expired", decided_at=utcnow()))
                    await s.commit()
            await _set_run_status(db, ctx.run_id, "waiting", "running")
            ctx.emit("approval_done", approval={"id": row.id, "status": status})
    async with db.session() as s:
        row = await s.get(Approval, row.id)
    return row, status == "approved"


async def _safely(fn, *args) -> None:
    try:
        await fn(*args)
    except Exception:
        log.exception("approval notifier failed")


async def authorize(db: Database, risk: str, tool: str, args: dict, span_id: str, timeout_s: float,
                    *, money: bool = False) -> Decision:
    """Whether this call may run. ``memo`` set means it already ran in an earlier attempt: use that result."""
    if risk == SAFE:
        return Decision(True)
    if risk == EXTERNAL:
        return Decision(False, "Refused by policy: Riki never contacts or acts toward other people.")
    ctx = RUN.get()
    if ctx.source != "chat":  # evals, heartbeats and schedules run with nobody there to approve
        return Decision(False, f"Refused: nobody is here to approve changes during a {ctx.source} run. Suggest it instead.")
    row, allowed = await _obtain(db, tool, args, span_id, timeout_s, money=money, grantable=True)
    if not allowed:
        return Decision(False, _denied(row.status), row.id)
    if row.executed_at is not None:
        return Decision(True, approval_id=row.id, memo=row.result or "")
    return Decision(True, approval_id=row.id)


async def record_result(db: Database, approval_id: str, text: str) -> None:
    """The action ran: keep what it returned, so a restart that replays the run does not do it again."""
    async with db.session() as s:
        await s.execute(update(Approval).where(Approval.id == approval_id)
                        .values(executed_at=utcnow(), result=text[:20_000]))
        await s.commit()


async def ask(db: Database, question: str, options: list[str], span_id: str, timeout_s: float) -> str:
    """ask_user: one question with 2-4 choices, answered in the app or on the phone. Returns the answer."""
    ctx = RUN.get()
    if ctx.source != "chat":
        return "Nobody is here to answer. Make the most reasonable assumption, say what you assumed, and go on."
    row, answered = await _obtain(db, ASK, {"question": question, "options": options}, span_id, timeout_s,
                                  money=False, grantable=False)
    if answered and row.result is not None:
        return row.result
    return "The user skipped the question. Make the most reasonable assumption, say what you assumed, and go on."


async def decide(db: Database, approval_id: str, approve: bool, *, answer: str | None = None,
                 grant: bool = False) -> bool:
    """Resolve a pending approval (in the app or on the phone). False if it is no longer waiting."""
    now = utcnow()
    async with db.session() as s:
        row = await s.get(Approval, approval_id)
        if row is None or row.status != "pending":
            return False
        row.status, row.decided_at = ("approved" if approve else "denied"), now
        if answer is not None:
            row.result = answer[:500]
        if approve and grant and row.thread_id and row.tool != ASK and not row.money:
            s.add(ApprovalGrant(thread_id=row.thread_id, tool=row.tool, expires_at=now + timedelta(minutes=GRANT_MINUTES)))
        await s.commit()
    future = _PENDING.get(approval_id)
    if future is not None and not future.done():
        future.set_result(approve)
    return True


async def expire_stale(db: Database) -> None:
    """On startup: requests nobody answered within their time can never be answered."""
    now = utcnow()
    async with db.session() as s:
        await s.execute(update(Approval).where(Approval.status == "pending", Approval.expires_at < now)
                        .values(status="expired", decided_at=now))
        await s.commit()
