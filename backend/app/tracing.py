"""Run-scoped tracing: every agent, model call, tool call and memory lookup becomes a span.

Spans are buffered in memory and written once when the run ends (no DB round trip on the hot
path); live events go to the run's queue so the UI can draw the agent tree as it grows.
Parenting uses a ContextVar, so spans from parallel sub-agents nest correctly on their own.
"""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from .db import new_id, utcnow

MAX_STORED_CHARS = 20_000

_SECRET_PATTERNS = [
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),  # OpenAI / Anthropic style keys
    re.compile(r"\b(ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),  # Slack
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),  # Google API key
    re.compile(r"\btvly-[A-Za-z0-9_\-]{12,}"),  # Tavily
    re.compile(r"\b(?:[sr]k_(?:live|test)_[A-Za-z0-9]{16,}|hf_[A-Za-z0-9]{30,}|gsk_[A-Za-z0-9]{30,})\b"),  # Stripe, HF, Groq
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]
# key=value / "key": "value" pairs; keeps the key so JSON stays valid
_KV_SECRET = re.compile(r'(?i)("?(?:password|passwd|secret|api_key|apikey|token)"?\s*[:=]\s*"?)[^\s",}]{4,}')


def run_cost(spans: list[dict], prices: dict) -> tuple[float | None, list[str]]:
    """Dollars spent on a run's model calls, and the models that had no price (cost is None if none had one).
    Cached tokens are billed at the cached rate and counted inside tokens_in.
    shortcut: only spans are priced, so riki-mem's own extraction and planner calls are not; wrap its provider
    in spans if the memory bill matters."""
    total, priced, unpriced = 0.0, False, []
    for sp in spans:
        if sp["kind"] != "llm" or not (sp["tokens_in"] or sp["tokens_out"]):
            continue
        price = prices.get(sp["name"])
        if price is None:
            if sp["name"] not in unpriced:
                unpriced.append(sp["name"])
            continue
        cached = min(sp["tokens_cached"], sp["tokens_in"])
        rate = price.input if price.cached is None else price.cached
        total += ((sp["tokens_in"] - cached) * price.input + cached * rate + sp["tokens_out"] * price.output) / 1e6
        priced = True
    return (round(total, 6) if priced else None), unpriced


def clean(text: str) -> str:
    """Postgres text and JSONB cannot hold NUL; one in a web page would otherwise lose the whole run."""
    return text.replace("\x00", "")


def scrub(text: str, mark: str = "[REDACTED]") -> str:
    """Replace anything shaped like a known API key, token or private key."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(mark, text)
    return text


def redact(value: Any) -> Any:
    """Strip secrets and cap size before anything is stored or streamed."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    text = clean(text).replace("\\u0000", "")
    text = scrub(_KV_SECRET.sub(r"\1[REDACTED]", text))
    if len(text) > MAX_STORED_CHARS:
        text = text[:MAX_STORED_CHARS] + f"... [truncated {len(text) - MAX_STORED_CHARS} chars]"
    if isinstance(value, str):
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text  # truncation broke the JSON; keep it as text


@dataclass
class RunContext:
    run_id: str
    source: str = "chat"  # "eval" auto-denies every approval
    memory_user: str = ""  # memory namespace for this run (evals use their own)
    thread_id: str | None = None
    effort: str = "high"  # config.EFFORT level; caps depth, helpers, steps and tokens for this run
    # Every event so far, so a client that navigated away, refreshed or lost its connection can replay the
    # run from the start (or from where it stopped) instead of losing it.
    events: list[dict] = field(default_factory=list)
    _tick: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    spans: list[dict] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    agents_spawned: int = 0
    tool_refs: int = 0
    seen: str = ""  # the user's message plus what searches and pages showed: the only URLs fetched without asking
    tainted: bool = False  # this run has read content nobody here wrote (web, files, apps): memory writes then need a yes
    lineage: list[str] = field(default_factory=list)  # runs this one continues (after a restart), newest first
    parked: bool = False  # waiting for you right now
    left_waiting: bool = False  # shut down while waiting for you: not finished, not failed; the next start continues it

    def emit(self, kind: str, **data: Any) -> None:
        self.events.append({"type": kind, **data})
        tick, self._tick = self._tick, asyncio.Event()
        tick.set()

    async def follow(self, start: int = 0, keepalive_s: float = 15):
        """Yield events from index ``start`` until run_end; yield None after each quiet keepalive_s."""
        i = start
        while True:
            while i < len(self.events):
                event = self.events[i]
                i += 1
                yield event
                if event["type"] == "run_end":
                    return
            try:  # no await between the check above and taking _tick, so a new event can't be missed
                await asyncio.wait_for(self._tick.wait(), keepalive_s)
            except TimeoutError:
                yield None

    def next_ref(self) -> str:
        self.tool_refs += 1
        return f"t{self.tool_refs}"


RUN: ContextVar[RunContext] = ContextVar("riki_run")
CURRENT_SPAN: ContextVar[str | None] = ContextVar("riki_span", default=None)


@asynccontextmanager
async def span(kind: str, name: str, input: Any = None, **meta: Any):
    """Record one unit of work. Yields a dict; set ``output``/``status``/tokens on it."""
    ctx = RUN.get()
    s = {
        "id": new_id(), "run_id": ctx.run_id, "parent_id": CURRENT_SPAN.get(), "kind": kind,
        "name": name[:200], "ref": None, "status": "ok", "input": redact(input), "output": None,
        "error": None, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "meta": meta,
        "started_at": utcnow(),
    }
    token = CURRENT_SPAN.set(s["id"])
    ctx.emit("span_start", span={k: s[k] for k in ("id", "parent_id", "kind", "name", "input", "meta")}
             | {"started_at": s["started_at"].isoformat()})
    try:
        yield s
    except asyncio.CancelledError:
        s["status"], s["error"] = "error", "cancelled"
        raise
    except Exception as exc:
        s["status"], s["error"] = "error", f"{type(exc).__name__}: {exc}"[:2000]
        raise
    finally:
        CURRENT_SPAN.reset(token)
        s["ended_at"] = utcnow()
        s["output"] = redact(s["output"])
        ctx.spans.append(s)
        ctx.emit("span_end", span={**s, "started_at": s["started_at"].isoformat(),
                                   "ended_at": s["ended_at"].isoformat()})
