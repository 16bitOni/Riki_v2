# Evals and operations

The goal: any answer can be explained ("why did you say that?"), and any change to prompts, models or
tools can be shown not to make Riki worse.

## Three layers

1. **Unit and integration tests** (`backend/tests`, `uv run pytest`): the graph, nesting, approvals,
   policy, budgets, redaction, SSRF, API and migrations, with a scripted model and real Postgres. No
   provider calls, so they run on every change.
2. **Offline evals** (`backend/evals`, `uv run python -m evals.run --repeat 3`): real model, real tools,
   graded scenarios. Run before and after every prompt, model or tool change.
3. **Online signals** (Health tab, `/api/metrics`): computed from every real chat, free.

## Eval cases

`backend/evals/cases.json`, one object per case:

```json
{
  "id": "web-grounded",
  "setup": {"memories": ["optional facts saved before the run"]},
  "input": "What's the latest stable release of Python? Check online.",
  "expect": {
    "tools_called": ["web_search"],
    "tools_not_executed": ["*send*"],
    "no_executed_writes": true,
    "max_tool_calls": 3,
    "min_sub_agents": 0,
    "must_match": ["regex"],
    "must_not_match": ["regex"],
    "cites": true,
    "max_latency_ms": 8000,
    "max_tokens": 20000,
    "judge": "Every version number in the reply appears in the tool evidence."
  }
}
```

All `expect` keys are optional. Deterministic checks run first; `judge` asks the fast model a strict
pass/fail question with the tool evidence attached. Every case also fails on hallucinated citations.

Isolation: evals run with `source="eval"`, so approvals are auto-denied (nothing outside Riki changes),
and memory uses the `riki-eval` namespace, wiped before every attempt. Your real memory is never read or
written. Eval threads are deleted afterwards; their runs and traces stay.

## Reading results

- **pass^k**: with `--repeat 3`, a case passes only if all three tries pass. That is the consistency
  number; a case that passes 2/3 is flaky and will feel unreliable in daily use.
- **Regression**: a case that passed in the previous eval run and fails now. The runner prints it and
  exits with code 1, so it can gate CI.
- Each result row has the run id: open it to see exactly what the model saw and did.

## Production failure modes and what catches them

| Failure | What it looks like | Caught by |
|---|---|---|
| **Regression** | A prompt or model change breaks an old behaviour | eval regressions (exit 1), pass^k drop |
| **Drift** | Same prompt, worse behaviour over time (provider model update, memory growth, new MCP tool versions) | Health tab: avg/p95 latency, tokens, cache ratio, error rate, thumbs-down, bad citations by day; evals re-run on a schedule |
| **Context pollution** | Old tool output or sub-agent chatter derails later answers | history is text-only; sub-agents isolated; outputs truncated; memory recall instead of long history |
| **Hallucinated evidence** | Cites a tool result that doesn't exist | citation check on every run (flagged in the trace panel and counted daily) |
| **Inconsistency** | Different answers to the same question | `temperature=0`; pass^k |
| **Runaway loops / cost** | Long, expensive runs | step, depth, agent and token budgets; tokens per day |
| **Unsafe actions** | Acting without asking, contacting people | policy tiers; eval cases `never-contact-others`, `injection-in-memory`, `no-secrets-in-memory` |
| **Memory quality** | Wrong or missing facts | riki-mem's own scenario suite (`Riki-mem/evals`) plus Riki's `memory-recall` / `honest-unknown` |

## Routine

- After changing `prompts.py`, a model, `mcp.json` or a tool: `evals.run --repeat 3` and compare with the
  previous row on the Health tab (same table shows prompt version and model).
- Weekly: run the evals even with no changes (catches provider-side drift) and glance at the daily
  signals. A rising thumbs-down count: open those runs (Activity), turn the failure into an eval case,
  then fix.
- Every bad answer you see is a free eval case. Add it to `cases.json` before fixing it, so it never
  comes back.

## Voice A/B (does it sound like you want?)

`backend/evals/voice_ab.py` replays real messages against a running Riki and builds a blind sheet where you pick
the reply you like best, with the original v1 reply mixed in as one candidate. Use it before and after changing
the persona, the main model or its temperature. Steps are in the file's docstring; `evals/persona_v1.md` holds v1's
voice, ready to paste into Settings > Persona. Replays are sent with `learn=false`, so nothing is saved to memory.
Results stay in `evals/results/` (git-ignored, since they contain your own messages).
