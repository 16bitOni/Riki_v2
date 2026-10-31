"""Offline eval suite: replays scenario cases through the real agent and grades them.

    uv run python -m evals.run                      # all cases once
    uv run python -m evals.run --repeat 3           # consistency: a case passes only if all 3 tries pass
    uv run python -m evals.run --case greeting      # one case

Each attempt runs with source="eval": approvals are auto-denied (nothing outside Riki changes) and
memory uses a throwaway namespace that is wiped before every attempt. Results are stored in
eval_runs; a case that passed last time and fails now is a regression and the exit code is 1.
"""

import argparse
import asyncio
import fnmatch
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel
from riki_memory import MemoryCreate
from sqlalchemy import delete, select

from app.agent.prompts import JUDGE
from app.checks import citation_check
from app.config import get_settings
from app.db import EvalRun, Thread, new_id
from app.runtime import Runtime
from app.tracing import RunContext, clean, scrub
from app.turn import ensure_thread, run_turn

CASES = Path(__file__).with_name("cases.json")
EVAL_MEMORY_USER = "riki-eval"


class Verdict(BaseModel):
    passed: bool
    reason: str


def check_case(expect: dict, output: str, spans: list[dict], latency_ms: int, tokens: int) -> list[str]:
    """Deterministic checks. Returns failure messages (empty list = pass)."""
    fails = []
    tools = [s for s in spans if s["kind"] == "tool"]
    executed = [s["name"] for s in tools if s["status"] == "ok"]
    called = [s["name"] for s in tools]
    agents = sum(1 for s in spans if s["kind"] == "agent") - 1  # minus the main agent
    for name in expect.get("tools_called", []):
        if name not in called:
            fails.append(f"expected tool {name} to be called; called {called}")
    for pattern in expect.get("tools_not_executed", []):
        hits = [n for n in executed if fnmatch.fnmatch(n, pattern)]
        if hits:
            fails.append(f"tools matching {pattern!r} must not run; ran {hits}")
    if expect.get("no_executed_writes") and any(s["status"] == "ok" and (s["meta"] or {}).get("risk") != "safe"
                                                 for s in tools):
        fails.append("a non-safe tool was executed")
    if "max_tool_calls" in expect and len(tools) > expect["max_tool_calls"]:
        fails.append(f"{len(tools)} tool calls > {expect['max_tool_calls']}")
    if agents < expect.get("min_sub_agents", 0):
        fails.append(f"{agents} sub-agents < {expect['min_sub_agents']}")
    for rx in expect.get("must_match", []):
        if not re.search(rx, output, re.I):
            fails.append(f"reply does not match /{rx}/")
    for rx in expect.get("must_not_match", []):
        if re.search(rx, output, re.I):
            fails.append(f"reply matches forbidden /{rx}/")
    cites = citation_check(output, spans)
    if cites["invalid_citations"]:
        fails.append(f"hallucinated citations {cites['invalid_citations']}")
    if expect.get("cites") and cites["citations"] == 0:
        fails.append("reply has no citations")
    if latency_ms > expect.get("max_latency_ms", 10**9):
        fails.append(f"latency {latency_ms}ms > {expect['max_latency_ms']}ms")
    if tokens > expect.get("max_tokens", 10**9):
        fails.append(f"tokens {tokens} > {expect['max_tokens']}")
    return fails


async def judge(rt: Runtime, rubric: str, message: str, output: str, spans: list[dict]) -> Verdict:
    # Wide enough for a whole fetched page: a judge that sees half of the evidence calls the other half invented.
    evidence = "\n".join(f"[{s['ref']}] {s['name']}: {json.dumps(s['output'], default=str)[:8000]}"
                         for s in spans if s["kind"] == "tool")[:40_000] or "(no tools used)"
    now = datetime.now(ZoneInfo(rt.settings.timezone)).strftime("%A %d %B %Y, %H:%M %Z")
    prompt = [SystemMessage(JUDGE), HumanMessage(
        f"Rubric: {rubric}\n\nUser message:\n{message}\n\nAgent reply:\n{output}\n\nTool evidence:\n{evidence}\n\n"
        f"Context the agent is always given (you cannot see the rest of it): the current date and time, {now}, and "
        "the user's stored memories. Using the date, time or weekday is not inventing a fact.")]
    try:  # the stronger model grades; a fast model grading itself is noisy (same reply, three different verdicts)
        return await rt.models["smart"].with_structured_output(Verdict).ainvoke(prompt)
    except Exception as exc:  # e.g. the smart model cannot do structured output on this gateway
        print(f"  (smart judge failed: {type(exc).__name__}; using the fast model)", file=sys.stderr)
        return await rt.models["fast"].with_structured_output(Verdict).ainvoke(prompt)


async def attempt(rt: Runtime, case: dict) -> dict:
    await rt.memory.delete_user(EVAL_MEMORY_USER)
    for content in case.get("setup", {}).get("memories", []):
        await rt.memory.save(MemoryCreate(user_id=EVAL_MEMORY_USER, content=content))
    # evals run against the real app state; a case can pretend an app is not connected (restored below)
    hidden = {a: rt.apps.state.get(a) for a in case.get("setup", {}).get("disconnected_apps", [])}
    for app in hidden:
        rt.apps.state[app] = {"state": "disconnected"}
    message = scrub(clean(case["input"]), "[secret removed]")  # what main.ChatIn does before a message reaches a run
    thread_id = await ensure_thread(rt, None, f"[eval] {case['id']}")
    # same limits as real chats unless a case says otherwise (RunContext alone defaults to the largest, "high")
    ctx = RunContext(run_id=new_id(), source="eval", memory_user=EVAL_MEMORY_USER,
                     effort=case.get("effort") or rt.settings.default_effort)
    started = time.perf_counter()
    try:
        # own task: run_turn sets the RUN context var, which must not leak between attempts
        output = await asyncio.create_task(run_turn(rt, ctx, thread_id, message, learn=False))
    finally:
        for app, original in hidden.items():
            rt.apps.state.pop(app, None)
            if original is not None:
                rt.apps.state[app] = original
        async with rt.db.session() as s:  # runs and spans stay (thread_id -> null); the chat thread goes
            await s.execute(delete(Thread).where(Thread.id == thread_id))
            await s.commit()
    latency = int((time.perf_counter() - started) * 1000)
    expect = case.get("expect", {})
    fails = check_case(expect, output, ctx.spans, latency, ctx.tokens_in + ctx.tokens_out)
    if expect.get("judge"):
        verdict = await judge(rt, expect["judge"], message, output, ctx.spans)
        if not verdict.passed:
            fails.append(f"judge: {verdict.reason}")
    return {"run_id": ctx.run_id, "passed": not fails, "fails": fails, "latency_ms": latency,
            "tokens": ctx.tokens_in + ctx.tokens_out, "output": output[:2000]}


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeat", type=int, default=1, help="tries per case; pass^k needs all to pass")
    ap.add_argument("--case", action="append", help="only these case ids")
    args = ap.parse_args(argv)

    cases = [c for c in json.loads(CASES.read_text()) if not args.case or c["id"] in args.case]
    rt = Runtime(get_settings())
    await rt.start()
    for _ in range(100):  # saved apps reconnect in the background; let them settle so cases see one stable state
        if not any(st.get("state") == "connecting" for st in rt.apps.state.values()):
            break
        await asyncio.sleep(0.2)
    try:
        results = []
        for case in cases:
            tries = [await attempt(rt, case) for _ in range(args.repeat)]
            ok = sum(t["passed"] for t in tries)
            results.append({"id": case["id"], "passed": ok == len(tries), "passes": ok, "tries": tries})
            mark = "PASS" if ok == len(tries) else "FAIL"
            print(f"{mark} {case['id']:<28} {ok}/{len(tries)}  "
                  f"{sum(t['latency_ms'] for t in tries) // len(tries)}ms  "
                  f"{sum(t['tokens'] for t in tries) // len(tries)} tok")
            for t in tries:
                for f in t["fails"]:
                    print(f"     - {f}")

        async with rt.db.session() as s:
            previous = await s.scalar(select(EvalRun).order_by(EvalRun.created_at.desc()).limit(1))
            before = {r["id"]: r["passed"] for r in (previous.results if previous else [])}
            regressions = [r["id"] for r in results if before.get(r["id"]) and not r["passed"]]
            s.add(EvalRun(prompt_version=rt.prompt_version, model=rt.settings.model, repeat=args.repeat,
                          passed=sum(r["passed"] for r in results), total=len(results), results=results,
                          regressions=regressions))
            await s.commit()
    finally:
        await rt.stop()

    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} cases passed (pass^{args.repeat}), prompt {rt.prompt_version}")
    if regressions:
        print(f"REGRESSIONS vs previous run: {', '.join(regressions)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
