"""One user message -> one run: recall memory, run the main agent, persist the trace, learn."""

import asyncio
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlalchemy import select, update

from .agent.graph import AgentSpec, as_text
from .agent.prompts import VERIFY
from .checks import NOT_EVIDENCE, citation_check, drop_found, evidence, grounding_input, is_source, parse_unsupported
from .db import Message, Run, Span, Thread, new_id, utcnow
from .runtime import Runtime
from .tracing import RUN, RunContext, clean, run_cost, scrub, span

log = logging.getLogger(__name__)


# Greetings, thanks, acknowledgements: answered from the persona alone, with no memory lookup and no tools.
_SMALL_TALK = re.compile(
    r"^\s*(?:(?:hi+|hey+|hello+|yo|sup|hola|namaste|good\s?(?:morning|afternoon|evening|night|day)|thanks?|"
    r"thank\s?you|thx|ty|bye+|goodbye|see\s?you|ok+|okay|k|cool|nice|great|got\s?it|lol|haha|"
    r"how\s(?:are|r)\s(?:you|u)|what'?s\s?up|wassup)\b[\s,!?.…]*)+$", re.I)


def is_small_talk(text: str, agent_name: str) -> bool:
    return bool(_SMALL_TALK.match(re.sub(re.escape(agent_name), "", text, flags=re.I)))


def with_ids(text: str, memories) -> str:
    """riki-mem leaves ids out of the context block; the model needs them to cite a memory as [m:<id>]."""
    for m in memories:
        text = re.sub(rf"^(- \[[^\]\n]*\] {re.escape(m.content)}[^\n]*)$", rf"\1  (id: {m.id})", text, flags=re.M)
    return text


def title_for(text: str) -> str:
    return text.strip().splitlines()[0][:80] or "New chat"


async def ensure_thread(rt: Runtime, thread_id: str | None, first_message: str) -> str:
    async with rt.db.session() as s:
        if thread_id and await s.get(Thread, thread_id):
            return thread_id
        thread = Thread(title=title_for(first_message))
        s.add(thread)
        await s.commit()
        return thread.id


MIN_HISTORY_TURNS = 4  # however long they are, so a pasted document never makes Riki forget the last exchange


async def _history(rt: Runtime, thread_id: str) -> list:
    """Recent turns as plain text, as many as fit in ``history_chars`` (never fewer than MIN_HISTORY_TURNS, never more
    than ``history_turns``). Short banter keeps its texture; one long paste does not drown the rest.
    Tool calls stay in the trace, out of future context."""
    async with rt.db.session() as s:
        rows = (await s.scalars(select(Message).where(Message.thread_id == thread_id)
                                .order_by(Message.created_at.desc()).limit(2 * rt.settings.history_turns))).all()
    kept, size = [], 0
    for i, m in enumerate(rows):  # newest first; cut only between exchanges, so a reply keeps its question
        size += len(m.content)
        if i % 2 == 0 and i >= 2 * MIN_HISTORY_TURNS and size > rt.settings.history_chars:
            break
        kept.append(m)
    note = lambda m: f"{m.content}\n[{len(m.images)} image(s) attached]" if m.images else m.content  # noqa: E731
    return [HumanMessage(note(m)) if m.role == "user" else AIMessage(m.content) for m in reversed(kept)]


async def core_context(rt: Runtime, user: str) -> str:
    """What small talk knows: the core blocks (who they are, how they like things, the people and projects that
    matter), no search and no model call. Greetings used to get nothing, so "hey" met a stranger."""
    try:
        blocks = await asyncio.wait_for(rt.memory.get_blocks(user), 2)
    except Exception:
        return ""
    lines = [f"{label}:\n" + "\n".join(f"- {t}" for t in texts) for label, texts in (blocks or {}).items() if texts]
    return f"## About {rt.settings.user_name} (core memory)\n" + "\n".join(lines)[:1500] if lines else ""


async def _sources(rt: Runtime, ctx: RunContext, thread_id: str) -> list[dict]:
    """This run's evidence. A follow-up that looked nothing up ("how many were there?") is checked against the
    last run in this chat that did, since that is what it answers from."""
    if any(is_source(sp) for sp in ctx.spans):
        return ctx.spans
    async with rt.db.session() as db:
        prev = await db.scalar(select(Span.run_id).join(Run, Run.id == Span.run_id).where(
            Run.thread_id == thread_id, Span.kind == "tool", Span.status == "ok", Span.name.not_in(NOT_EVIDENCE))
            .order_by(Span.started_at.desc()).limit(1))
        rows = (await db.scalars(select(Span).where(Span.run_id == prev))).all() if prev else []
    return ctx.spans + [{"kind": r.kind, "name": r.name, "ref": None, "status": r.status, "output": r.output}
                        for r in rows]


async def check_grounding(rt: Runtime, ctx: RunContext, thread_id: str, question: str, reply: str) -> list[str]:
    """Claims in the reply that its evidence does not back. One model call; never raises."""
    try:
        proof = evidence(await _sources(rt, ctx, thread_id)) or "(none: nothing was looked up)"
        async with span("llm", rt.model_names["fast"],
                        input={"purpose": "grounding check", "evidence_chars": len(proof)}) as sp:
            now = datetime.now(ZoneInfo(rt.settings.timezone)).strftime("%A %d %B %Y, %H:%M %Z")
            apps = [a for a, st in rt.apps.state.items() if st.get("state") == "connected"]
            msg = await asyncio.wait_for(rt.models["fast"].ainvoke([SystemMessage(VERIFY), HumanMessage(
                grounding_input(question, proof, reply, now=now, spans=ctx.spans, apps=apps))]),
                rt.settings.tool_timeout_s)
            usage = msg.usage_metadata or {}
            sp.update(tokens_in=usage.get("input_tokens", 0), tokens_out=usage.get("output_tokens", 0))
            ctx.tokens_in += sp["tokens_in"]
            ctx.tokens_out += sp["tokens_out"]
            sp["output"] = unsupported = drop_found(parse_unsupported(as_text(msg.content)), proof)
            return unsupported
    except Exception:
        log.exception("grounding check failed for run %s; the reply goes out unchecked", ctx.run_id)
        return []

def start_run(rt: Runtime, thread_id: str, text: str, *, source: str = "chat", learn: bool = True,
              images: list[str] | None = None, effort: str | None = None) -> RunContext:
    """Start one run in the background and register it, so any client can follow it (also after navigating
    away or refreshing), stop it, or be told it is busy. Finished runs stay followable for two minutes."""
    ctx = RunContext(run_id=new_id(), source=source, thread_id=thread_id,
                     effort=effort or rt.settings.default_effort)
    task = rt.spawn(run_turn(rt, ctx, thread_id, text, learn=learn, images=images))
    rt.runs[ctx.run_id] = (ctx, task)
    task.add_done_callback(lambda _: asyncio.get_running_loop().call_later(120, rt.runs.pop, ctx.run_id, None))
    return ctx


async def _save(rt: Runtime, ctx: RunContext, thread_id: str, output: str, status: str, error: str | None,
                latency: int, checks: dict, cost: float | None, spans: bool) -> None:
    async with rt.db.session() as db:
        if spans:
            db.add_all([Span(**sp) for sp in ctx.spans])
        db.add(Message(thread_id=thread_id, role="assistant", content=output, run_id=ctx.run_id))
        await db.execute(update(Run).where(Run.id == ctx.run_id).values(
            status=status, output=output, error=error, latency_ms=latency, tokens_in=ctx.tokens_in,
            tokens_out=ctx.tokens_out, tokens_cached=ctx.tokens_cached, cost_usd=cost, checks=checks))
        await db.commit()


async def run_turn(rt: Runtime, ctx: RunContext, thread_id: str, text: str, *, learn: bool = True,
                   images: list[str] | None = None) -> str:
    """Must run with RUN set to ``ctx`` (call inside its own task). Never raises; errors land in the run."""
    s = rt.settings
    ctx.memory_user = memory_user = ctx.memory_user or s.user_id
    ctx.thread_id = ctx.thread_id or thread_id
    RUN.set(ctx)
    started = time.perf_counter()
    text = clean(text)
    ctx.seen = text
    history = await _history(rt, thread_id)
    async with rt.db.session() as db:
        db.add(Run(id=ctx.run_id, thread_id=thread_id, source=ctx.source, input=text, model=s.model,
                   prompt_version=rt.prompt_version))
        db.add(Message(thread_id=thread_id, role="user", content=text, images=images or None, run_id=ctx.run_id))
        thread = await db.get(Thread, thread_id)
        if thread and thread.kind == "side" and len(thread.title.split()) < 3 and len(text.split()) >= 3:
            thread.title = title_for(text)  # "hi" is not a title; the first real message is
        await db.commit()
    ctx.emit("run_start", run_id=ctx.run_id, thread_id=thread_id)
    rt.active[ctx.run_id] = {"run_id": ctx.run_id, "thread_id": thread_id, "source": ctx.source,
                             "input": text[:200], "started_at": utcnow()}

    cost = None
    output, status, error, known, unsupported = "", "ok", None, [], None
    try:
        memory_text, small = "", is_small_talk(text, rt.persona.name)
        if small:
            memory_text = await core_context(rt, memory_user)
        else:
            try:
                async with span("memory", "recall", input=text) as sp:
                    recent = "\n".join(f"{'User' if isinstance(m, HumanMessage) else 'Riki'}: {m.content}"
                                       for m in history[-4:])
                    memory_text, known = await asyncio.wait_for(
                        rt.memory.context(memory_user, text, recent_chat=recent, name=s.user_name),
                        s.memory_timeout_s)
                    memory_text = with_ids(memory_text, known)
                    sp["output"] = memory_text
            except TimeoutError:  # the model still has recall_memory if it needs it
                log.warning("memory recall took over %ss; answering without it", s.memory_timeout_s)
            except Exception:
                log.exception("memory recall failed; answering without it")
        now = datetime.now(ZoneInfo(s.timezone)).strftime("%A %d %B %Y, %H:%M %Z")
        framed = f"<context>\nNow: {now}\n{memory_text}\n</context>\n\n{text}"
        message = HumanMessage([{"type": "text", "text": framed},
                                *({"type": "image_url", "image_url": {"url": u}} for u in images)] if images else framed)
        output = await asyncio.wait_for(
            rt.agent.run(AgentSpec(rt.persona.name, rt.main_system, "fast", 0), [*history, message],
                         [] if small else list(s.core_tools)), s.run_timeout_s)
        if not small:
            unsupported = await check_grounding(rt, ctx, thread_id, text, output)
    except TimeoutError:
        status, error = "error", f"run timed out after {s.run_timeout_s:g}s"
        output = "I stopped: this took longer than allowed. Ask me again, or break it into smaller steps."
    except asyncio.CancelledError:  # the Stop button (or shutdown): finish cleanly instead of vanishing
        status, output = "stopped", "Stopped. Nothing more will happen for this message."
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
    except Exception as exc:
        log.exception("run %s failed", ctx.run_id)
        status, error = "error", f"{type(exc).__name__}: {exc}"
        output = "Sorry, something broke on my side. The trace for this message has the details."

    # From here on nothing may stop the client from getting run_end, or the UI waits forever.
    try:
        rt.active.pop(ctx.run_id, None)
        if "[secret removed]" in text:  # said by the code, not left to the model: it must never look accepted
            output = f"The key you pasted was removed and not saved. Use the connect card or the Apps tab.\n\n{output}"
        if unsupported:  # said by the code, so a made-up detail can never pass as checked
            output += "\n\n> ⚠️ **Not verified:** I couldn't find these in what I read: " + "; ".join(unsupported)
        output = scrub(clean(output), "[secret removed]")
        latency = int((time.perf_counter() - started) * 1000)
        checks = citation_check(output, ctx.spans) | ({} if unsupported is None else {"unsupported": unsupported})
        cost, unpriced = run_cost(ctx.spans, s.prices)
        if unpriced:
            checks = checks | {"unpriced_models": unpriced}
        if checks["invalid_citations"]:
            log.warning("run %s cites evidence that does not exist: %s", ctx.run_id, checks["invalid_citations"])
        try:
            await _save(rt, ctx, thread_id, output, status, error, latency, checks, cost, spans=True)
        except Exception:
            log.exception("saving run %s failed; saving the answer without its trace", ctx.run_id)
            error = (error or "") + " (the step-by-step trace could not be saved)"
            await _save(rt, ctx, thread_id, output, status, error.strip(), latency, checks, cost, spans=False)
    except Exception:
        log.exception("could not record run %s at all", ctx.run_id)
        latency, checks = int((time.perf_counter() - started) * 1000), {}
    finally:
        ctx.emit("run_end", run_id=ctx.run_id, status=status, output=output, error=error, latency_ms=latency,
                 tokens_in=ctx.tokens_in, tokens_out=ctx.tokens_out, tokens_cached=ctx.tokens_cached,
                 cost_usd=cost, checks=checks, finished_at=utcnow().isoformat())
    if learn and status == "ok":
        rt.spawn(rt.memory.remember(memory_user, text, output, known=known, turn_key=f"{thread_id}/{ctx.run_id}"))
    return output
