"""Look inside one run: what it was asked, every tool call with its ref and status, and what it answered.

    uv run python -m evals.show_run <run_id>        # the id eval failures print, or from the run's trace

Read-only. Use it when a failure's reason is unclear (for example "cites evidence that does not exist: t5": was t5 a
call that failed, or one that never happened?).
"""

import asyncio
import sys

from sqlalchemy import select

from app.config import get_settings
from app.db import Database, Run, Span


async def main(run_id: str) -> int:
    db = Database(get_settings().database_url)
    try:
        async with db.session() as s:
            run = await s.get(Run, run_id)
            if run is None:
                print("no such run")
                return 1
            spans = (await s.scalars(select(Span).where(Span.run_id == run_id).order_by(Span.started_at))).all()
    finally:
        await db.close()
    print(f"{run.status}  {run.model}  prompt {run.prompt_version}  {run.latency_ms} ms  "
          f"{run.tokens_in + run.tokens_out} tokens  cost {run.cost_usd}\nchecks: {run.checks}\n\nASKED: {run.input}\n")
    for sp in spans:
        if sp.kind == "tool":
            out = str(sp.output)[:160].replace("\n", " ")
            print(f"  [{sp.ref}] {sp.name} {sp.status}" + (f"  error: {sp.error[:120]}" if sp.error else "") + f"\n      -> {out}")
    print(f"\nANSWER:\n{run.output}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1])) if len(sys.argv) == 2 else print(__doc__) or 1)
