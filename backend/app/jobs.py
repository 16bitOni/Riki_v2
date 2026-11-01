"""Background work is still Riki's work: it gets a run, so it is traced, priced and listed in Activity like a chat."""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from sqlalchemy import update

from .agent.graph import as_text
from .db import Run, Span, new_id
from .memory import lenient
from .tracing import RUN, RunContext, run_cost, span

log = logging.getLogger(__name__)


async def job_run(rt, source: str, title: str, work: Callable[[RunContext], Awaitable[Any]],
                  *, thread_id: str | None = None) -> tuple[str, Any]:
    """Run ``work(ctx)`` as a traced run of its own. Never raises; returns (run_id, result or None)."""

    async def go() -> tuple[str, Any]:
        ctx = RunContext(run_id=new_id(), source=source, memory_user=rt.settings.user_id, thread_id=thread_id,
                         effort="low")
        RUN.set(ctx)  # own task: the context var must not leak into the caller
        started = time.perf_counter()
        async with rt.db.session() as db:
            db.add(Run(id=ctx.run_id, thread_id=thread_id, source=source, input=title[:2000], model=rt.settings.model,
                       prompt_version=rt.prompt_version, effort="low"))
            await db.commit()
        status, error, result = "ok", None, None
        try:
            result = await work(ctx)
        except Exception as exc:
            log.exception("%s job failed", source)
            status, error = "error", f"{type(exc).__name__}: {exc}"[:2000]
        cost, unpriced = run_cost(ctx.spans, rt.settings.prices)
        try:
            async with rt.db.session() as db:
                db.add_all([Span(**sp) for sp in ctx.spans])
                await db.execute(update(Run).where(Run.id == ctx.run_id).values(
                    status=status, error=error, output=result if isinstance(result, str) else None,
                    latency_ms=int((time.perf_counter() - started) * 1000), tokens_in=ctx.tokens_in,
                    tokens_out=ctx.tokens_out, tokens_cached=ctx.tokens_cached, cost_usd=cost,
                    checks={"unpriced_models": unpriced} if unpriced else {}))
                await db.commit()
        except Exception:
            log.exception("could not record %s job %s", source, ctx.run_id)
        return ctx.run_id, result

    return await asyncio.create_task(go())


async def call(rt, tier: str, system: str, user: str, schema=None, purpose: str = ""):
    """One model call inside a job run: a span with tokens, so it shows up in the trace and the bill.
    With ``schema`` the reply is parsed into it; without, the text comes back."""
    from langchain_core.messages import HumanMessage, SystemMessage

    ctx = RUN.get()
    messages = [SystemMessage(system), HumanMessage(user)]
    async with span("llm", rt.model_names[tier], input={"purpose": purpose, "chars": len(user)}) as sp:
        if schema is None:
            raw = await rt.models[tier].ainvoke(messages)
            parsed = as_text(raw.content)
        else:
            out = await rt.models[tier].with_structured_output(lenient(schema), include_raw=True).ainvoke(messages)
            raw, parsed = out["raw"], out.get("parsed")
            if parsed is None:
                raise ValueError(f"the model's reply did not fit {schema.__name__}: {out.get('parsing_error')}")
        usage = getattr(raw, "usage_metadata", None) or {}
        cached = (usage.get("input_token_details") or {}).get("cache_read", 0) or 0
        sp.update(tokens_in=usage.get("input_tokens", 0), tokens_out=usage.get("output_tokens", 0), tokens_cached=cached,
                  output=parsed if isinstance(parsed, str) else parsed.model_dump())
        ctx.tokens_in += sp["tokens_in"]
        ctx.tokens_out += sp["tokens_out"]
        ctx.tokens_cached += cached
    return parsed
