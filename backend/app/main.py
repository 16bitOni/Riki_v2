"""HTTP API. Run: uvicorn app.main:app --host 127.0.0.1 --port 8000"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, func, select, text

from riki_memory import MemoryCreate

from .agent import policy
from .agent.prompts import ONBOARD
from .apps import CATALOG, CATEGORIES, ROLES, search_directory
from .config import Effort, get_settings
from .db import Approval, Artifact, Connection, EvalRun, Feedback, Message, Persona, Run, Span, Thread, new_id, utcnow
from .ops import LOGS, beat, own_thread
from .runtime import Runtime
from .tracing import clean, scrub
from .turn import ensure_thread, start_run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger().addHandler(LOGS)
log = logging.getLogger(__name__)

COOKIE = "riki_session"
HexId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


@asynccontextmanager
async def lifespan(app: FastAPI):
    rt = getattr(app.state, "rt", None) or Runtime(get_settings())  # tests pre-set app.state.rt
    await rt.start()
    app.state.rt = rt
    yield
    await rt.stop()


app = FastAPI(title="Riki", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def invalid_request(_: Request, exc: RequestValidationError):
    """FastAPI's default 422 echoes the rejected value back; for a pasted key or password that must not happen."""
    return JSONResponse({"detail": [{k: v for k, v in e.items() if k not in ("input", "ctx")} for e in exc.errors()]},
                        status_code=422)


def rt_dep(request: Request) -> Runtime:
    return request.app.state.rt


def session_token(rt: Runtime) -> str:
    # Derived from the password: changing the password logs every session out.
    return hmac.new(rt.settings.password.get_secret_value().encode(), b"riki-session-v1", hashlib.sha256).hexdigest()


def require_auth(request: Request, rt: Runtime = Depends(rt_dep)) -> Runtime:
    if not hmac.compare_digest(request.cookies.get(COOKIE, ""), session_token(rt)):
        raise HTTPException(401, "login required")
    return rt


Auth = Annotated[Runtime, Depends(require_auth)]


# --- auth -------------------------------------------------------------------------------------

class LoginIn(BaseModel):
    password: str = Field(max_length=200)


@app.post("/api/login")
async def login(body: LoginIn, response: Response, rt: Runtime = Depends(rt_dep)):
    if not hmac.compare_digest(body.password.encode(), rt.settings.password.get_secret_value().encode()):
        await asyncio.sleep(1)  # slows guessing
        raise HTTPException(401, "wrong password")
    response.set_cookie(COOKIE, session_token(rt), httponly=True, samesite="strict",
                        secure=rt.settings.cookie_secure, max_age=30 * 86400)
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
async def me(rt: Auth):
    s, p = rt.settings, rt.persona
    return {"name": s.user_name, "agent_name": p.name, "hue": p.hue, "model": s.model, "smart_model": s.smart_model,
            "avatar": f"/api/persona/avatar?v={int(p.updated_at.timestamp())}" if p.avatar_type else None,
            "prompt_version": rt.prompt_version, "max_depth": s.max_depth, "quiet_hours": s.quiet_hours,
            "timezone": s.timezone, "default_effort": s.default_effort, "role": p.role, "onboarded": p.onboarded}


# --- persona and heartbeat --------------------------------------------------------------------

class PersonaIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    soul: str = Field("", max_length=4000)
    hue: int = Field(ge=0, le=359)
    heartbeat_minutes: int = Field(ge=0, le=24 * 60)
    heartbeat_prompt: str = Field("", max_length=2000)


def _persona(p: Persona) -> dict:
    return {k: getattr(p, k) for k in ("name", "soul", "hue", "heartbeat_minutes", "heartbeat_prompt", "updated_at")}


@app.get("/api/persona")
async def get_persona(rt: Auth):
    return _persona(rt.persona)


@app.put("/api/persona")
async def put_persona(body: PersonaIn, rt: Auth):
    async with rt.db.session() as s:
        p = await s.get(Persona, 1)
        for k, v in body.model_dump().items():
            setattr(p, k, v.strip() if isinstance(v, str) else v)
        p.updated_at = utcnow()
        await s.commit()
    return _persona(await rt.reload_persona())


AVATAR_MAX_BYTES = 5 * 1024 * 1024
_AVATAR_MAGIC = [(b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
                 (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif")]


def avatar_type(data: bytes) -> str | None:
    """Type from the file's own bytes. Only raster formats: SVG could carry script."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return next((mime for magic, mime in _AVATAR_MAGIC if data.startswith(magic)), None)


@app.put("/api/persona/avatar")
async def put_avatar(request: Request, rt: Auth):
    """Raw image bytes as the body (PNG, JPEG, GIF or WebP, up to 5 MB)."""
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > AVATAR_MAX_BYTES:
            raise HTTPException(413, "That image is over 5 MB.")
    mime = avatar_type(bytes(data))
    if mime is None:
        raise HTTPException(415, "Use a PNG, JPEG, GIF or WebP image.")
    async with rt.db.session() as s:
        p = await s.get(Persona, 1)
        p.avatar, p.avatar_type, p.updated_at = bytes(data), mime, utcnow()
        await s.commit()
    await rt.reload_persona()
    return {"ok": True}


@app.delete("/api/persona/avatar")
async def delete_avatar(rt: Auth):
    async with rt.db.session() as s:
        p = await s.get(Persona, 1)
        p.avatar, p.avatar_type, p.updated_at = None, None, utcnow()
        await s.commit()
    await rt.reload_persona()
    return {"ok": True}


@app.get("/api/persona/avatar")
async def get_avatar(rt: Auth):
    async with rt.db.session() as s:
        row = (await s.execute(select(Persona.avatar, Persona.avatar_type).where(Persona.id == 1))).one_or_none()
    if row is None or row.avatar is None:
        raise HTTPException(404, "no avatar")
    # the URL carries ?v=<updated_at>, so it can be cached for good
    return Response(row.avatar, media_type=row.avatar_type,
                    headers={"Cache-Control": "private, max-age=31536000, immutable",
                             "X-Content-Type-Options": "nosniff"})


@app.get("/api/heartbeat")
async def heartbeat_feed(rt: Auth, limit: int = Query(20, ge=1, le=100)):
    async with rt.db.session() as s:
        rows = (await s.scalars(select(Run).where(Run.source == "heartbeat")
                                .order_by(Run.created_at.desc()).limit(limit))).all()
    return [_row(r) for r in rows]


@app.post("/api/heartbeat/run")
async def heartbeat_now(rt: Auth):
    if any(a["source"] == "heartbeat" for a in rt.active.values()):
        raise HTTPException(409, "a heartbeat is already running")
    rt.spawn(beat(rt, rt.persona))
    return {"ok": True}


# --- setup: what you use Riki for, your apps, and what Riki learns from them -------------------

class OnboardingIn(BaseModel):
    role: str | None = Field(None, max_length=24)  # an id from apps.json roles; none = keep
    about: str = Field("", max_length=2000)  # in your words: learned like a chat message
    done: bool = False

    @field_validator("about")
    @classmethod
    def no_keys(cls, v: str) -> str:
        return scrub(clean(v), "[secret removed]")


def _onboarding(rt: Runtime) -> dict:
    return {"done": rt.persona.onboarded, "role": rt.persona.role, "roles": list(ROLES.values())}


@app.get("/api/onboarding")
async def onboarding(rt: Auth):
    return _onboarding(rt)


@app.post("/api/onboarding")
async def save_onboarding(body: OnboardingIn, rt: Auth):
    if body.role and body.role not in ROLES:
        raise HTTPException(422, "unknown role")
    async with rt.db.session() as s:
        p = await s.get(Persona, 1)
        if body.role:
            p.role = body.role
            p.heartbeat_prompt = p.heartbeat_prompt or ROLES[body.role]["checkin"]  # never overwrite yours
        p.onboarded, p.updated_at = body.done, utcnow()
        await s.commit()
    if body.about.strip():  # extracted like any chat turn, so secrets and small talk are skipped the same way
        rt.spawn(rt.memory.remember(rt.settings.user_id, body.about.strip(), "Noted.", name=rt.settings.user_name,
                                    turn_key=f"onboarding/{new_id()}"))
    await rt.reload_persona()
    return _onboarding(rt)


@app.post("/api/onboarding/scan")
async def onboarding_scan(rt: Auth):
    """Riki reads your connected apps and proposes facts about you. It runs as source "onboarding", so every
    change is refused in code (nobody approves during it), and nothing is learned until you pick facts below."""
    thread = await own_thread(rt, "onboarding", "Getting to know you")
    if busy := rt.active_run(thread):
        return {"run_id": busy.run_id}
    ctx = start_run(rt, thread, ONBOARD.format(name=rt.settings.user_name), source="onboarding", learn=False)
    return {"run_id": ctx.run_id}


class FactsIn(BaseModel):
    facts: list[Annotated[str, Field(min_length=3, max_length=300)]] = Field(max_length=30)
    run_id: HexId | None = None  # the scan they came from: shown as their source in Memory


@app.post("/api/onboarding/facts")
async def save_facts(body: FactsIn, rt: Auth):
    """Save the facts you ticked. Each one is your confirmation, so they are stored as stated, not re-extracted."""
    source = None
    if body.run_id:
        async with rt.db.session() as s:
            run = await s.get(Run, body.run_id)
        source = f"{run.thread_id}/{run.id}" if run and run.source == "onboarding" else None
    saved = 0
    for fact in dict.fromkeys(" ".join(f.split()) for f in body.facts):
        if scrub(fact, "\0") != fact:  # a key-shaped string is never a fact about you
            continue
        await rt.memory.save(MemoryCreate(user_id=rt.settings.user_id, content=clean(fact), kind="fact",
                                          importance=3, source_key=source))
        saved += 1
    return {"saved": saved}


# --- what the system is doing (the "engineer layer") ------------------------------------------

@app.get("/api/status")
async def status(rt: Auth):
    """One call for the shell: avatar mood, badges, and the System page overview."""
    started = time.perf_counter()
    db_ok = True
    try:
        async with rt.db.session() as s:
            await s.execute(text("select 1"))
    except Exception:
        db_ok = False
    db_ms = round((time.perf_counter() - started) * 1000, 1)
    day = text("""
        select count(*) as runs, count(*) filter (where status = 'error') as errors,
               percentile_cont(0.5) within group (order by latency_ms)::int as p50_ms,
               percentile_cont(0.95) within group (order by latency_ms)::int as p95_ms,
               coalesce(sum(tokens_in + tokens_out), 0)::int as tokens,
               coalesce(sum(tokens_cached)::float / nullif(sum(tokens_in), 0), 0) as cache_ratio
        from runs where source = 'chat' and created_at > now() - interval '24 hours'""")
    last_day, pending, errors, last_beat = {}, 0, [], None
    if db_ok:
        async with rt.db.session() as s:
            last_day = dict((await s.execute(day)).mappings().one())
            pending = await s.scalar(select(func.count()).select_from(Approval).where(Approval.status == "pending"))
            errors = [{"run_id": r.id, "at": r.created_at, "error": r.error, "input": r.input[:120]}
                      for r in (await s.scalars(select(Run).where(Run.status == "error")
                                                .order_by(Run.created_at.desc()).limit(5))).all()]
            last_beat = await s.scalar(select(Run).where(Run.source == "heartbeat")
                                       .order_by(Run.created_at.desc()).limit(1))
    by_risk: dict[str, int] = {}
    for e in rt.registry.entries.values():
        by_risk[e.risk] = by_risk.get(e.risk, 0) + 1
    p = rt.persona
    return {
        "ok": db_ok and all(m["ok"] for m in rt.mcp_status.values()),
        "uptime_s": int(time.time() - rt.started_at),
        "db": {"ok": db_ok, "latency_ms": db_ms},
        "models": {"main": rt.settings.model, "smart": rt.settings.smart_model, "embedding": rt.settings.embedding_model},
        "prompt_version": rt.prompt_version,
        "mcp": rt.mcp_status,
        "tools": {"total": len(rt.registry.entries), "by_risk": by_risk},
        "memory_writes": rt.memory.write_stats.as_dict() if hasattr(rt.memory, "write_stats") else {},
        "last_24h": last_day,
        "active": list(rt.active.values()),
        "pending_approvals": pending,
        "recent_errors": errors,
        "warnings": sum(1 for r in LOGS.records if r["level"] in ("WARNING", "ERROR", "CRITICAL")),
        "heartbeat": {"every_min": p.heartbeat_minutes,
                      "last": {"run_id": last_beat.id, "at": last_beat.created_at, "status": last_beat.status}
                      if last_beat else None},
    }


@app.get("/api/logs")
async def logs(rt: Auth, level: str = Query("INFO", pattern="^(INFO|WARNING|ERROR)$"),
               limit: int = Query(200, ge=1, le=500)):
    order = {"INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    rows = [r for r in LOGS.records if order.get(r["level"], 0) >= order[level]]
    return rows[-limit:][::-1]


async def _approvals(rt: Runtime, status: str) -> list[dict]:
    q = select(Approval, Run.input, Run.thread_id).join(Run, Run.id == Approval.run_id)
    q = q.where(Approval.status == "pending").order_by(Approval.created_at) if status == "pending" else \
        q.where(Approval.status != "pending").order_by(Approval.decided_at.desc()).limit(50)
    async with rt.db.session() as s:
        rows = (await s.execute(q)).all()
    risks = {n: e.risk for n, e in rt.registry.entries.items()}
    return [{**_row(a), "request": inp, "thread_id": tid, "risk": risks.get(a.tool, "write")} for a, inp, tid in rows]


@app.get("/api/approvals")
async def approvals(rt: Auth, status: str = Query("pending", pattern="^(pending|decided)$")):
    """The approvals inbox: what is waiting on you, with what you asked for."""
    return await _approvals(rt, status)


@app.get("/api/live")
async def live(rt: Auth):
    """What is happening right now, from the server's point of view: the shell polls this, so the status
    and any approval are right on every page and in every chat, not only where the run was started."""
    return {"active": sorted(rt.active.values(), key=lambda a: a["started_at"]),
            "asks": await _approvals(rt, "pending")}


@app.get("/api/health")
async def health(rt: Runtime = Depends(rt_dep)):
    async with rt.db.session() as s:
        await s.execute(text("select 1"))
    return {"ok": True}


# --- chat -------------------------------------------------------------------------------------

_IMAGE = re.compile(r"^data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/]+={0,2}$")


class ChatIn(BaseModel):
    message: str = Field("", max_length=20_000)
    thread_id: HexId | None = None  # none = a new side chat
    images: list[str] = Field(default_factory=list, max_length=4)  # data: URLs; the browser shrinks them first
    effort: Effort | None = None  # none = RIKI_DEFAULT_EFFORT
    learn: bool = True  # false: answer, but do not save anything from this message (replays, experiments)

    @field_validator("message")
    @classmethod
    def printable(cls, v: str) -> str:
        return scrub(clean(v), "[secret removed]")  # a pasted key must never reach the model, memory or the database

    @field_validator("images")
    @classmethod
    def small_images(cls, v: list[str]) -> list[str]:
        if any(len(i) > 4_000_000 or not _IMAGE.match(i) for i in v):  # ~3 MB each; png, jpeg, webp or gif only
            raise ValueError("images must be png, jpeg, webp or gif, under 3 MB each")
        return v

    @model_validator(mode="after")
    def not_empty(self) -> "ChatIn":
        if not self.message.strip() and not self.images:
            raise ValueError("message is empty")
        return self


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
async def chat(body: ChatIn, rt: Auth):
    """Streams run events (tokens, spans, approvals) as SSE. The run keeps going if the client drops, and
    GET /api/runs/{id}/stream picks it up again. A chat takes one message at a time."""
    thread_id = await ensure_thread(rt, body.thread_id, body.message or "Image")
    if busy := rt.active_run(thread_id):  # no await from here to start_run, so two fast POSTs can't both pass
        raise HTTPException(409, {"message": "Riki is still working on your last message in this chat.",
                                  "run_id": busy.run_id})
    ctx = start_run(rt, thread_id, body.message, images=body.images, effort=body.effort, learn=body.learn)
    return _follow(ctx, 0, first={"type": "thread", "thread_id": thread_id, "run_id": ctx.run_id})


def _follow(ctx, start: int, first: dict | None = None) -> StreamingResponse:
    async def stream():
        if first:  # not part of the replayable log: the replay starts at the run's own first event
            yield _sse(first)
        async for event in ctx.follow(start):
            yield ": keep-alive\n\n" if event is None else _sse(event)  # e.g. while waiting on an approval

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}/stream")
async def follow_run(run_id: HexId, rt: Auth, start: int = Query(0, ge=0, alias="from")):
    """Re-attach to a run (after navigating away, a refresh or a dropped connection): replays its events
    from ``from`` and goes on live. 404 once it has been finished for a while; its messages are in the chat."""
    entry = rt.runs.get(run_id)
    if entry is None:
        raise HTTPException(404, "this run is no longer live")
    return _follow(entry[0], start)


@app.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: HexId, rt: Auth):
    """Stop a run that is working or waiting for an approval. It ends cleanly and says so in the chat."""
    entry = rt.runs.get(run_id)
    if entry is None or entry[1].done():
        raise HTTPException(409, "this run is not running")
    entry[1].cancel()
    return {"ok": True}


class DecisionIn(BaseModel):
    approve: bool


@app.post("/api/approvals/{approval_id}")
async def decide(approval_id: HexId, body: DecisionIn, rt: Auth):
    if not policy.decide(approval_id, body.approve):
        raise HTTPException(409, "this approval is no longer pending")
    return {"ok": True}


# --- threads, runs, traces --------------------------------------------------------------------

_THREADS_SQL = text("""
    select t.id, t.title, t.kind, t.created_at, m.content as preview, m.role as preview_role,
           coalesce(m.created_at, t.created_at) as last_at
    from threads t
    left join lateral (select content, role, created_at from messages where thread_id = t.id
                       order by created_at desc limit 1) m on true
    where t.kind = any(:kinds)
    order by last_at desc limit :limit""")


_MD_NOISE = re.compile(r"\[(?:t\d+|m:[0-9a-f]{8})\]|[*_`#>|]|-{3,}|\[([^\]]*)\]\([^)]*\)")


def plain_preview(text: str | None) -> str:
    """One readable line from a markdown reply: no table pipes, emphasis or citation refs."""
    text = " ".join(_MD_NOISE.sub(lambda m: m.group(1) or " ", text or "").split())
    return re.sub(r" ([.,;:!?)])", r"\1", text)[:140]  # a dropped [t1] must not leave "calendar ."


async def _threads(rt: Runtime, kinds: list[str], limit: int) -> list[dict]:
    async with rt.db.session() as s:
        rows = (await s.execute(_THREADS_SQL, {"kinds": kinds, "limit": limit})).mappings().all()
    return [{**r, "preview": plain_preview(r["preview"])} for r in rows]


@app.get("/api/threads")
async def threads(rt: Auth):
    """Side chats, most recently active first, each with its last message as a preview."""
    return await _threads(rt, ["side"], 200)


class RenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)


@app.patch("/api/threads/{thread_id}")
async def rename_thread(thread_id: HexId, body: RenameIn, rt: Auth):
    async with rt.db.session() as s:
        t = await s.get(Thread, thread_id)
        if t is None or t.kind != "side":
            raise HTTPException(404, "no such side chat")
        t.title = body.title.strip()
        await s.commit()
    return {"ok": True}


@app.get("/api/threads/main")
async def main_thread(rt: Auth):
    """The one ongoing conversation (created on first use)."""
    async with rt.db.session() as s:
        t = await s.scalar(select(Thread).where(Thread.kind == "main").limit(1))
        if t is None:
            t = Thread(title=rt.persona.name, kind="main")
            s.add(t)
            await s.commit()
    return {"id": t.id, "title": t.title, "kind": t.kind, "created_at": t.created_at}


@app.get("/api/threads/{thread_id}/messages")
async def thread_messages(thread_id: HexId, rt: Auth):
    async with rt.db.session() as s:
        rows = (await s.scalars(select(Message).where(Message.thread_id == thread_id)
                                .order_by(Message.created_at))).all()
        run_ids = [m.run_id for m in rows if m.run_id and m.role == "assistant"]
        scores = dict((await s.execute(select(Feedback.run_id, Feedback.score).where(
            Feedback.run_id.in_(run_ids)))).all())
        runs_ = {r.id: r for r in (await s.execute(select(Run.id, Run.latency_ms, Run.error).where(Run.id.in_(run_ids)))).all()}
        steps: dict[str, list] = {}
        for sp in (await s.scalars(select(Span).where(Span.run_id.in_(run_ids), Span.kind != "llm")
                                   .order_by(Span.started_at))).all():
            # just enough for the step list under each reply; the full trace is /api/runs/{id}
            steps.setdefault(sp.run_id, []).append(
                {"id": sp.id, "parent_id": sp.parent_id, "kind": sp.kind, "name": sp.name, "ref": sp.ref,
                 "status": sp.status, "started_at": sp.started_at,
                 "input": sp.input if isinstance(sp.input, dict) else None})
    return [{"id": m.id, "role": m.role, "content": m.content, "images": m.images or [], "run_id": m.run_id,
             "feedback": scores.get(m.run_id) if m.role == "assistant" else None, "created_at": m.created_at,
             **({"steps": steps.get(m.run_id, []), "latency_ms": getattr(runs_.get(m.run_id), "latency_ms", None),
                 "error": getattr(runs_.get(m.run_id), "error", None)}
                if m.role == "assistant" else {})}
            for m in rows[-200:]]


@app.delete("/api/threads/{thread_id}")
async def delete_thread(thread_id: HexId, rt: Auth):
    """Delete a topic chat. The main chat and the check-in thread are permanent, so for those the messages
    are cleared instead. What Riki learned stays in Memory; what it did stays in Activity."""
    if rt.active_run(thread_id):
        raise HTTPException(409, "Riki is still working in this chat. Stop it first.")
    async with rt.db.session() as s:
        thread = await s.get(Thread, thread_id)
        if thread is None:
            raise HTTPException(404, "no such chat")
        if thread.kind == "side":
            await s.delete(thread)
        else:
            await s.execute(delete(Message).where(Message.thread_id == thread_id))
        await s.commit()
    return {"ok": True, "cleared": thread.kind != "side"}


@app.delete("/api/runs/{run_id}/messages/{role}")
async def delete_message(run_id: HexId, role: Literal["user", "assistant"], rt: Auth):
    """Remove one message (your question or Riki's reply) from its chat. The run, its trace and anything learned
    from it stay in Activity and Memory."""
    async with rt.db.session() as s:
        rows = (await s.scalars(select(Message).where(Message.run_id == run_id, Message.role == role))).all()
        if not rows:
            raise HTTPException(404, "no such message")
        if rt.active_run(rows[0].thread_id):
            raise HTTPException(409, "Riki is still working in this chat. Stop it first.")
        for m in rows:
            await s.delete(m)
        await s.commit()
    return {"ok": True}


def _row(obj) -> dict:
    return {c.key: getattr(obj, c.key) for c in obj.__table__.columns}


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: HexId, rt: Auth):
    async with rt.db.session() as s:
        run = await s.get(Run, run_id)
        if run is None:
            raise HTTPException(404, "no such run")
        spans = (await s.scalars(select(Span).where(Span.run_id == run_id).order_by(Span.started_at))).all()
        approvals = (await s.scalars(select(Approval).where(Approval.run_id == run_id))).all()
        fb = await s.get(Feedback, run_id)
    return {"run": _row(run), "spans": [_row(x) for x in spans], "approvals": [_row(a) for a in approvals],
            "feedback": _row(fb) if fb else None}


class FeedbackIn(BaseModel):
    score: int = Field(ge=-1, le=1)
    comment: str = Field("", max_length=2000)


@app.post("/api/runs/{run_id}/feedback")
async def feedback(run_id: HexId, body: FeedbackIn, rt: Auth):
    async with rt.db.session() as s:
        if await s.get(Run, run_id) is None:
            raise HTTPException(404, "no such run")
        await s.merge(Feedback(run_id=run_id, score=body.score, comment=body.comment))
        await s.commit()
    return {"ok": True}


@app.get("/api/tools")
async def tools(rt: Auth):
    core = set(rt.settings.core_tools)
    return [{"name": n, "risk": e.risk, "source": e.source, "core": n in core,
             "description": e.tool.description} for n, e in sorted(rt.registry.entries.items())]


# --- artifacts (Markdown files and designed pages Riki made) -----------------------------------

# The page runs sandboxed (an opaque origin with no cookies), so the agent's HTML can never reach the API.
_VIEW_CSP = ("sandbox allow-scripts allow-modals; default-src 'none'; script-src 'unsafe-inline'; "
             "style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com data:; "
             "img-src https: data:")
# Lets the viewer's PDF button print the page itself rather than the app around it.
_PRINT = "<script>addEventListener('message',e=>e.data==='riki-print'&&print())</script>"


def _artifact(a, content: bool = False) -> dict:
    return {"id": a.id, "thread_id": a.thread_id, "run_id": a.run_id, "title": a.title, "kind": a.kind,
            "updated_at": a.updated_at, **({"content": a.content} if content else {})}


async def _get_artifact(rt: Runtime, artifact_id: str) -> Artifact:
    async with rt.db.session() as s:
        a = await s.get(Artifact, artifact_id)
    if a is None or a.kind not in ("md", "html"):  # rows from the old doc/slides/sheet tool stay hidden
        raise HTTPException(404, "no such artifact")
    return a


@app.get("/api/artifacts")
async def list_artifacts(rt: Auth, thread_id: HexId | None = None):
    q = (select(Artifact.id, Artifact.thread_id, Artifact.run_id, Artifact.title, Artifact.kind, Artifact.updated_at)
         .where(Artifact.kind.in_(("md", "html"))).order_by(Artifact.updated_at.desc()).limit(200))
    async with rt.db.session() as s:
        rows = (await s.execute(q.where(Artifact.thread_id == thread_id) if thread_id else q)).all()
    return [_artifact(a) for a in rows]


@app.get("/api/artifacts/{artifact_id}")
async def get_artifact(artifact_id: HexId, rt: Auth):
    return _artifact(await _get_artifact(rt, artifact_id), content=True)


@app.get("/api/artifacts/{artifact_id}/view")
async def view_artifact(artifact_id: HexId, rt: Auth):
    """The page as the agent wrote it, sandboxed, for the viewer's iframe."""
    a = await _get_artifact(rt, artifact_id)
    if a.kind != "html":
        raise HTTPException(404, "no such page")
    return HTMLResponse(a.content + _PRINT, headers={"Content-Security-Policy": _VIEW_CSP,
                                                    "X-Content-Type-Options": "nosniff"})


# --- apps (your accounts, connected as MCP servers) -------------------------------------------

AppId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")]
_APP_FIELDS = ("id", "name", "category", "icon", "blurb", "auth", "url", "token_label", "token_help", "popular",
               "inspect_args")


def _app_view(rt: Runtime, app: dict, row: Connection | None, uses: dict) -> dict:
    connected = bool(row and row.enabled)
    state = rt.apps.view_state(app["id"]) if connected else {"state": "off", "error": None, "auth_url": None,
                                                              "tools": []}
    view = {k: app.get(k) for k in _APP_FIELDS}
    if app["id"] not in CATALOG:  # an address you typed can carry a key in its path or query: show only the host
        u = urlparse(app["url"])
        view["url"] = view["blurb"] = f"{u.scheme}://{u.hostname}"
    return {**view, "connected": connected,
            "mode": row.mode if row else app.get("mode", "ask"), "uses_week": uses.get(app["id"], 0), **state}


@app.get("/api/apps")
async def list_apps(rt: Auth):
    rows = {r.id: r for r in await rt.apps.rows()}
    uses = await rt.apps.uses_this_week()
    defs = [*CATALOG.values(), *(rt.apps.definition(r) for r in rows.values() if r.id not in CATALOG)]
    return {"categories": [*CATEGORIES, "Added by you"], "apps": [_app_view(rt, a, rows.get(a["id"]), uses) for a in defs]}


class ConnectIn(BaseModel):
    token: str | None = Field(None, max_length=4000)
    client_id: str | None = Field(None, max_length=300)
    client_secret: str | None = Field(None, max_length=300)
    mode: Literal["read", "ask"] | None = None


def _callback_url(request: Request) -> str:
    # The address you opened Riki at (Vite on :5173 in dev proxies it), so the app sends you back here.
    origin = request.headers.get("origin") or str(request.base_url)
    return f"{origin.rstrip('/')}/api/apps/callback"


@app.post("/api/apps/{app_id}/connect")
async def connect_app(app_id: AppId, body: ConnectIn, request: Request, rt: Auth):
    row = await rt.apps.row(app_id)
    app_def = CATALOG.get(app_id) or (rt.apps.definition(row) if row else None)
    if app_def is None:
        raise HTTPException(404, "no such app")
    async with rt.db.session() as s:
        r = await s.get(Connection, app_id) or Connection(id=app_id, name=app_def["name"], url=app_def["url"],
                                                           auth=app_def["auth"], mode=app_def.get("mode", "ask"))
        r.enabled, r.mode, r.updated_at = True, body.mode or r.mode or app_def.get("mode", "ask"), utcnow()
        s.add(r)
        await s.commit()
    secret, redirect_uri = await rt.apps.secret(app_id), _callback_url(request)
    changes: dict = {"redirect_uri": redirect_uri}
    if body.token:
        changes["token"] = body.token.strip()
    if app_def["auth"] == "google":
        if body.client_id and body.client_secret:
            changes["client"] = {"client_id": body.client_id.strip(), "client_secret": body.client_secret.strip(),
                                 "redirect_uris": [redirect_uri], "token_endpoint_auth_method": "client_secret_post",
                                 "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}
        elif not secret.get("client"):
            raise HTTPException(400, "Add your Google OAuth client ID and secret first.")
    elif secret.get("client") and redirect_uri not in secret["client"].get("redirect_uris", []):
        changes["client"] = None  # you opened Riki at a new address: register again for it
    if app_def["auth"] == "token" and not (body.token or secret.get("token")):
        raise HTTPException(400, f"Paste your {app_def.get('token_label', 'key')} first.")
    await rt.apps.save_secret(app_id, **changes)
    return await rt.apps.connect(app_id)


class AppIn(BaseModel):
    mode: Literal["read", "ask"]


@app.patch("/api/apps/{app_id}")
async def update_app(app_id: AppId, body: AppIn, rt: Auth):
    async with rt.db.session() as s:
        row = await s.get(Connection, app_id)
        if row is None:
            raise HTTPException(404, "not connected")
        row.mode, row.updated_at = body.mode, utcnow()
        await s.commit()
    # re-register its tools under the new mode; tokens are kept, so no sign-in
    return await rt.apps.connect(app_id, interactive=False) if row.enabled else {"state": "off"}


@app.delete("/api/apps/{app_id}")
async def disconnect_app(app_id: AppId, rt: Auth):
    """Disconnect and delete its tokens. What it already did stays in Activity."""
    await rt.apps.disconnect(app_id)
    async with rt.db.session() as s:
        await s.execute(delete(Connection).where(Connection.id == app_id))
        await s.commit()
    rt.apps.state.pop(app_id, None)
    return {"ok": True}


@app.get("/api/apps/callback", response_class=HTMLResponse)
async def app_callback(request: Request, state: str = Query("", max_length=500), code: str | None = None,
                       error: str | None = None):
    """Where an app sends you after sign-in. No login cookie here (it's a cross-site redirect); the one-time
    OAuth state ties it to the sign-in Riki started."""
    ok = request.app.state.rt.apps.finish_sign_in(state, code, error)
    message = "Connected. You can close this window." if ok and not error else \
        "That sign-in didn't go through. Close this window and try again from Riki."
    return HTMLResponse(
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<title>Riki</title><body style="margin:0;height:100vh;display:grid;place-items:center;background:#000;'
        f'color:#f5f5f7;font:17px -apple-system,system-ui,sans-serif"><p>{message}</p>'
        '<script>setTimeout(() => window.close(), 900)</script>', status_code=200 if ok else 400)


@app.get("/api/apps/directory")
async def app_directory(rt: Auth, q: str = Query(..., min_length=2, max_length=100)):
    try:
        return await search_directory(q)
    except Exception as exc:
        raise HTTPException(502, f"The MCP Registry didn't answer: {type(exc).__name__}") from exc


class CustomAppIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    url: str = Field(pattern=r"^(https://|http://(localhost|127\.0\.0\.1)(:\d+)?/)\S{1,480}$")
    token: str | None = Field(None, max_length=4000)
    header: str | None = Field(None, pattern=r"^[A-Za-z0-9-]{1,60}$")


@app.post("/api/apps/custom")
async def add_custom_app(body: CustomAppIn, request: Request, rt: Auth):
    """Any remote MCP server by its address: from the MCP Registry search or pasted by you."""
    base = re.sub(r"[^a-z0-9]+", "_", body.name.lower()).strip("_")[:30] or "app"
    base = base if base[0].isalpha() else f"app_{base}"
    taken = set(CATALOG) | {r.id for r in await rt.apps.rows()} | {
        e.source.split(":", 1)[-1] for e in rt.registry.entries.values()} | set(rt.registry.entries)
    app_id = next(c for c in [base, *(f"{base}_{i}" for i in range(2, 99))] if c not in taken)
    async with rt.db.session() as s:
        s.add(Connection(id=app_id, name=body.name.strip(), url=body.url, auth="token" if body.token else "oauth",
                         mode="ask"))
        await s.commit()
    await rt.apps.save_secret(app_id, redirect_uri=_callback_url(request), token=body.token and body.token.strip(),
                              header=body.header)
    return {"id": app_id, **await rt.apps.connect(app_id)}


# --- memory (the user's control over what Riki knows) -----------------------------------------

def _memory(m) -> dict:
    return {"id": m.id, "content": m.content, "kind": m.kind, "importance": m.importance,
            "tags": m.tags, "event_at": m.event_at, "created_at": m.created_at,
            "source": m.source_key}  # "<thread>/<run>" for memories learned from a chat: provenance


@app.get("/api/memories")
async def memories(rt: Auth, q: str = Query("", max_length=500), limit: int = Query(100, ge=1, le=500),
                   offset: int = Query(0, ge=0)):
    if q:
        found, _ = await rt.memory.recall(rt.settings.user_id, q)
        return [_memory(m) for m in found]
    return [_memory(m) for m in await rt.memory.list_memories(rt.settings.user_id, limit=limit, offset=offset)]


class MemoryEditIn(BaseModel):
    content: str = Field(min_length=3, max_length=2000)


@app.patch("/api/memories/{memory_id}")
async def correct_memory(memory_id: HexId, body: MemoryEditIn, rt: Auth):
    """"That's wrong": saves your wording as the new version; the old one is kept as history."""
    m = await rt.memory.edit(rt.settings.user_id, memory_id, content=body.content.strip())
    if m is None:
        raise HTTPException(404, "no such memory")
    return _memory(m)


@app.delete("/api/memories/{memory_id}")
async def delete_memory(memory_id: HexId, rt: Auth):
    return {"deleted": await rt.memory.delete(rt.settings.user_id, memory_id)}


# --- evals and health metrics -----------------------------------------------------------------

@app.get("/api/evals")
async def evals(rt: Auth):
    async with rt.db.session() as s:
        rows = (await s.scalars(select(EvalRun).order_by(EvalRun.created_at.desc()).limit(50))).all()
    return [_row(r) for r in rows]


@app.get("/api/metrics")
async def metrics(rt: Auth, days: int = Query(30, ge=1, le=365)):
    """Daily drift signals for real chats: latency, tokens, cache hits, errors, bad citations,
    refusals, thumbs-down."""
    sql = text("""
        with d as (select run_id, count(*) as denied from spans where status = 'denied' group by run_id)
        select date_trunc('day', r.created_at)::date as day,
               count(*) as runs,
               avg(r.latency_ms)::int as avg_latency_ms,
               percentile_cont(0.95) within group (order by r.latency_ms)::int as p95_latency_ms,
               avg(r.tokens_in + r.tokens_out)::int as avg_tokens,
               coalesce(sum(r.cost_usd), 0)::float as cost_usd,
               coalesce(sum(r.tokens_cached)::float / nullif(sum(r.tokens_in), 0), 0) as cache_ratio,
               avg((r.status = 'error')::int)::float as error_rate,
               count(*) filter (where jsonb_array_length(coalesce(r.checks->'invalid_citations', '[]')) > 0)
                 as bad_citations,
               coalesce(sum(d.denied), 0)::int as denied_tools,
               count(f.run_id) filter (where f.score < 0) as thumbs_down
        from runs r left join feedback f on f.run_id = r.id left join d on d.run_id = r.id
        where r.source = 'chat' and r.created_at > now() - make_interval(days => :days)
        group by 1 order by 1""")
    async with rt.db.session() as s:
        rows = (await s.execute(sql, {"days": days})).mappings().all()
    return [dict(r) for r in rows]


@app.get("/api/cost")
async def cost(rt: Auth, days: int = Query(30, ge=1, le=365)):
    """Spend per day and per kind of run (chat, eval, heartbeat, onboarding), so background work shows up."""
    sql = text("""
        select date_trunc('day', created_at)::date as day, source, count(*) as runs,
               coalesce(sum(cost_usd), 0)::float as cost_usd,
               coalesce(sum(tokens_in + tokens_out), 0)::int as tokens,
               count(*) filter (where cost_usd is null) as unpriced_runs
        from runs where created_at > now() - make_interval(days => :days)
        group by 1, 2 order by 1, 2""")
    async with rt.db.session() as s:
        rows = (await s.execute(sql, {"days": days})).mappings().all()
    return [dict(r) for r in rows]


# --- Today, activity, export: the trust surfaces -----------------------------------------------

def _day_start(rt: Runtime) -> datetime:
    now = datetime.now(ZoneInfo(rt.settings.timezone))
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def _memories_since(rt: Runtime, since: datetime, limit: int = 200) -> list:
    return [m for m in await rt.memory.list_memories(rt.settings.user_id, limit=limit) if m.created_at >= since]


@app.get("/api/today")
async def today(rt: Auth):
    """The home screen: what needs you, what to continue, what Riki noticed and learned today."""
    start = _day_start(rt)
    async with rt.db.session() as s:
        pending = (await s.execute(select(Approval.id, Approval.tool, Run.input).join(Run, Run.id == Approval.run_id)
                                   .where(Approval.status == "pending").order_by(Approval.created_at))).all()
        beat_run = await s.scalar(select(Run).where(Run.source == "heartbeat", Run.status == "ok")
                                  .order_by(Run.created_at.desc()).limit(1))
        counts = (await s.execute(text("""
            select count(*) filter (where r.source = 'chat') as messages,
                   (select count(*) from spans sp join runs r2 on r2.id = sp.run_id
                     where sp.kind = 'tool' and sp.status = 'ok' and r2.created_at >= :start
                       and coalesce(sp.meta->>'risk', 'safe') <> 'safe') as actions
            from runs r where r.created_at >= :start"""), {"start": start})).mappings().one()
    learned = await _memories_since(rt, start)
    return {
        "needs_you": [{"id": a.id, "tool": a.tool, "request": a.input[:140]} for a in pending],
        "continue": await _threads(rt, ["main", "side"], 4),
        "nudge": {"run_id": beat_run.id, "at": beat_run.created_at, "text": beat_run.output}
        if beat_run and beat_run.created_at >= start else None,
        "suggest": ROLES.get(rt.persona.role, {}).get("prompts", []),  # starters for what you said you use Riki for
        "learned": {"count": len(learned), "items": [_memory(m) for m in learned[:3]]},
        "today": dict(counts),
    }


_ACTIVITY_SQL = text("""
    select s.id, s.run_id, s.name, s.status, s.input, coalesce(s.meta->>'risk', 'safe') as risk,
           s.started_at as at, r.input as request, r.thread_id, r.source
    from spans s join runs r on r.id = s.run_id
    where s.kind = 'tool' and (coalesce(s.meta->>'risk', 'safe') <> 'safe' or s.name = 'remember')
      and (:pat = '' or s.name ilike :pat or s.input::text ilike :pat or r.input ilike :pat)
    order by s.started_at desc limit :limit""")


# Runs worth showing: still going, failed or stopped, or that did something beyond answering (used a tool
# or asked a helper). Plain replies live in their chat. Spans are saved when a run ends, so a run that is
# still going (or waiting for your OK) is only visible here, from the runs and approvals tables.
_RUNS_SQL = text("""
    select r.id, r.created_at as at, r.input as request, r.status, r.latency_ms, r.error, r.thread_id, r.source,
           (select count(*) from spans s where s.run_id = r.id and s.kind = 'agent' and s.parent_id is not null) as helpers,
           (select count(*) from spans s where s.run_id = r.id and s.kind = 'tool'
              and s.name not in ('spawn_agent', 'load_tools')) as steps,
           (select count(*) from approvals a where a.run_id = r.id and a.status = 'pending') as waiting
    from runs r
    where r.source in ('chat', 'heartbeat', 'onboarding') and (:pat = '' or r.input ilike :pat or r.output ilike :pat)
      and (r.status <> 'ok' or exists (select 1 from spans s where s.run_id = r.id
                                       and ((s.kind = 'tool' and s.name <> 'load_tools')
                                            or (s.kind = 'agent' and s.parent_id is not null))))
    order by r.created_at desc limit :limit""")


async def _all_memories(rt: Runtime) -> list:
    out = []
    while batch := await rt.memory.list_memories(rt.settings.user_id, limit=500, offset=len(out)):
        out += batch
    return out


@app.get("/api/activity")
async def activity(rt: Auth, q: str = Query("", max_length=200), limit: int = Query(100, ge=1, le=500)):
    """Everything done on your behalf: requests Riki worked on (also the ones still going or that failed),
    actions that changed something (allowed, refused or failed) and memories learned."""
    pat = f"%{q}%" if q else ""
    async with rt.db.session() as s:
        actions = [{"type": "action", **r} for r in (await s.execute(_ACTIVITY_SQL, {"pat": pat, "limit": limit})).mappings()]
        runs_ = [{"type": "run", **r} for r in (await s.execute(_RUNS_SQL, {"pat": pat, "limit": limit})).mappings()]
    memories = [{"type": "memory", "at": m.created_at, **_memory(m)}
                for m in (await _all_memories(rt) if q else await rt.memory.list_memories(rt.settings.user_id, limit=limit))
                if not q or q.lower() in m.content.lower()]
    return sorted(actions + runs_ + memories, key=lambda e: e["at"], reverse=True)[:limit]


@app.get("/api/export")
async def export_all(rt: Auth):
    """Everything Riki keeps about you, as one JSON file: chats, memories, actions, files, persona."""
    async with rt.db.session() as s:
        threads_ = (await s.scalars(select(Thread).order_by(Thread.created_at))).all()
        messages = (await s.scalars(select(Message).order_by(Message.created_at))).all()
        approvals_ = (await s.scalars(select(Approval).order_by(Approval.created_at))).all()
        files = (await s.scalars(select(Artifact).where(Artifact.kind.in_(("md", "html")))
                                 .order_by(Artifact.updated_at))).all()
    by_thread: dict[str, list] = {}
    for m in messages:
        by_thread.setdefault(m.thread_id, []).append({"role": m.role, "content": m.content, "at": m.created_at})
    data = {
        "exported_at": utcnow(),
        "persona": _persona(rt.persona),
        "chats": [{"id": t.id, "title": t.title, "kind": t.kind, "created_at": t.created_at,
                   "messages": by_thread.get(t.id, [])} for t in threads_],
        "memories": [_memory(m) for m in await _all_memories(rt)],
        "approvals": [_row(a) for a in approvals_],
        "files": [_artifact(a, content=True) for a in files],
        "apps": [{"id": r.id, "name": r.name, "mode": r.mode, "connected": r.enabled}
                 for r in await rt.apps.rows()],  # never the tokens
    }
    return JSONResponse(json.loads(json.dumps(data, default=str)), headers={
        "Content-Disposition": f'attachment; filename="riki-export-{utcnow():%Y-%m-%d}.json"'})


# --- the built frontend, if present (one process in production) -------------------------------

_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="ui")
