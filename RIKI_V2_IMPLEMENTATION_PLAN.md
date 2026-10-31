# Riki v2: implementation plan

Written 2026-10-31, on top of `RIKI_V1_vs_V2_REPORT.md`. It builds on v2's code as it is today (tables in `backend/app/db.py`, the graph in `agent/graph.py`, the policy in `agent/policy.py`) and follows v2's own `docs/RULES.md`: smallest complete change, safety in code, every unit of work is a span, every behavior change gets a test or an eval.

Time estimates assume one focused person and are rough.

---

## 1. What "stands out" means

Plenty of agents can call tools and remember facts. Riki should win on five things that most assistants do badly, all built on parts that already exist in v1 or v2:

| Pillar | What the user feels | Built from |
|---|---|---|
| **1. A friend who notices** | It messages you first, about something specific, at a sensible time, and backs off when you ignore it. | v1's reflection, cognition and nudge logic, ported small |
| **2. A mind you can see and correct** | "Riki's read on you" is a page you can read and edit. Every memory, lesson and nudge links to the chat that caused it. | v2's Memory tab and traces, plus a reflection table |
| **3. Nothing behind your back** | Changes wait for you; you approve from your phone; anything that touches other people or money never runs on its own; a poisoned web page cannot plant a memory. | v2's risk tiers, plus a taint flag and a money list |
| **4. Finishes real work** | Hand it a vague task, approve a step hours later, it resumes after a restart, asks one good question instead of guessing. | LangGraph's Postgres checkpointer, persistent approvals, `ask_user` |
| **5. Honest about cost and evidence** | Every reply shows time and cost; numbers come from computed counts, not guesses; claims it could not verify are marked quietly. | v2's spans and checks, riki-mem's counting |

Pillars 1 and 2 are the gap from v1. Pillar 4 is the gap your roadmap already names. Pillars 3 and 5 exist and need finishing touches, not new systems.

**Non-goals for this plan:** multi-user, a desktop bridge, more app integrations, a new UI framework, public hosting.

---

## 2. Requirements and budgets

### Functional, in priority order
1. Sounds like Riki: v1's voice, uses what it knows even in small talk.
2. Keeps a living read on you, and learns from your corrections.
3. Reaches you on your phone: chat, reminders, nudges, approvals.
4. Runs tasks that outlive a turn or a restart; asks one question when a wrong guess is expensive.
5. Reads the files you give it.

### Non-functional budgets
These are targets to measure against. They are not promises.

| Budget | Target | How it is measured |
|---|---|---|
| Small-talk reply | p50 under 2.5 s, one model call, no tools | `runs.latency_ms` where the run made no tool calls |
| Normal answer | first token under 2 s; p50 under 8 s | spans |
| Background cost | under 25% of daily tokens; **zero model calls when nothing changed** | `runs.source` breakdown on the Health tab |
| Daily spend | a hard cap you set; background jobs pause first, then chat drops to low effort | new `runs.cost_usd` |
| Safety | no `write` without approval, no `external` ever, no memory write from a tainted run without approval | tests plus evals |
| Trace | every model call, tool, memory lookup, nudge and schedule firing is a span you can open | existing span tree |
| Voice | pass^3 on the voice evals of at least 7 of 8 cases | new eval cases |

---

## 3. Rules for this plan (the compromises)

1. **Port before inventing.** If v1 already solved it (prompts, nudge guards, Telegram formatting), copy the logic and shrink it.
2. **Reuse riki-mem for anything memory-shaped.** Lessons and corrections become `preference` memories, not a new store.
3. **One new table only when a thing has its own lifecycle.** Reflections, schedules, nudges and grants do; lessons do not.
4. **No model call without new input.** Background jobs check "anything new?" in SQL first.
5. **Cut anything whose failure modes outweigh its return.** That means attention threads, SQL mood signals, a plan tool, a sub-agent checkpoint and full cron syntax. Each is in the cut list (section 7) with the condition that would bring it back.
6. **Each phase ends usable** and has an exit check. If a phase misses its exit check, fix that before starting the next one.

---

## 4. Target shape (what changes)

```
            Web UI  ─┐                              ┌─ Telegram (your chat id only)
                     ├─> start_run(source, thread) <┤    chat in / stream out / approve / ask
                     │                              └─ notify(text)   <- schedules, nudges
                     v
   run_turn ── <context>: time + core (profile + reflection) + recalled memories (not for small talk)
       │
       ├─ Agent graph (unchanged nodes) ── depth-0 state checkpointed in Postgres   [NEW]
       │        tools ─> authorize(): safe | write | external | money   [money list, taint flag]
       │                     write/ask_user park the run: status=waiting, resumable   [NEW]
       ├─ checks: citations always; grounding only when there is evidence or checkable facts  [CHANGED]
       └─ cost_usd, latency, tokens on the run   [NEW]

   heartbeat_loop (already ticks every minute) becomes the scheduler:
       ├─ due schedules  ─> notify directly (no model) or a read-only run
       ├─ reflection refresh when there are 5+ new memories
       └─ cognition cycle every 4 h, outside quiet hours, only if something changed:
              think (smart model) -> reflection + short monologue
              act   (fast model)  -> nudge or not, with dedup, backoff, quiet hours
```

New tables: `reflections`, `schedules`, `nudges`, `approval_grants`. New columns: `runs.cost_usd`, `runs.status='waiting'`, and on `approvals`: `args_hash`, `executed_at`, `result`, `expires_at`.

---

## 5. Phases

### Phase 0: Foundation (1-2 days)

| Change | Where | Why |
|---|---|---|
| `git init`. Delete `backend/.playwright-mcp/`, `backend/rfs_snapshot.yml`, `.pytest_cache/`. Commit. Retire `_backup_2026-10-28/` after the first commit. | repo | Nothing else is safe without it |
| Record a baseline: `uv run python -m evals.run --repeat 3` | evals | Every later phase compares against it |
| **Cost per run.** `RIKI_PRICES` setting (input, cached, output price per million tokens, per model string). `cost_usd` is computed at `_save` from tokens. Health tab shows a daily total by `source`. | `config.py`, `turn._save`, migration `0009` | Cost is a product feature, and you cannot set budgets you cannot see |
| **Voice A/B** (from the report, section 5). Three configs, 20 archived v1 messages, blind comparison. | one-off script under `evals/` | Decides the default model and persona before building on them |

**Exit check:** v2 is in git; baseline recorded; cost visible per run; model and persona chosen from the A/B result.

### Phase 1: Sounds like Riki (week 1)

1. **Default persona = v1's voice.** Ship `app/agent/persona_default.md` (v1's `RIKI` prompt up to `Rules:`, minus v1-only tool lines). It is used when `persona.soul` is empty and is shown pre-filled in Settings, so you can still edit it. `PROMPT_VERSION` changes, so run the evals.
2. **Small talk keeps its memory.** Small talk still skips the search and the tools, but now gets a **core block**: the riki-mem profile/core memory plus the latest reflection (Phase 2; until then, the profile). The core block is cached in-process and refreshed when memory changes. It costs a few hundred tokens and no extra model call.
3. **History by size, not by count.** Replace `history_turns=8` with `history_chars` (default 16k), keeping at least 4 turns and at most 40. One setting, one function (`turn._history`).
4. **Grounding check: gated and quiet.**
   - Run it only when the run used evidence tools, or the reply contains numbers, dates, prices or links (a regex gate).
   - Show the result as a small marker under the reply ("2 claims not verified ›", from `runs.checks`) instead of appending a warning to the text.
   - Use the smart model for this check, because a model grading its own answer is weak. The gate keeps the extra cost small.
5. **Evals.**
   - Add 8 voice cases built from v1's register table: tease when light, drop the bit when upset, opinion once then help, no stranger openers, uses a remembered fact naturally in a greeting.
   - Change `small-talk-hi` so recalling something real is allowed and making something up is not.
   - The voice judge uses the smart model.

**Exit check:** voice cases pass^3 on at least 7 of 8; no regressions; small-talk p50 under 2.5 s.

### Phase 2: Knows you (week 2)

1. **Reflection.**
   - New `reflections` table: `id, content, memory_count, run_id, created_at`.
   - `ops.refresh_reflection()` runs when riki-mem holds 5 or more memories newer than the last reflection. It uses v1's `REFLECTION_PROMPT` on the smart model, and the job is a span.
   - Injected into `<context>` on every turn as part of the core block.
   - **Shown on the Memory tab as "Riki's read on you", with Edit.** An edit is saved as a new reflection, and the change is also stored as a preference memory, so it sticks.
2. **Lessons from corrections**, with no new store.
   - A thumbs-down with a comment becomes a riki-mem `preference` memory, after one-tap confirmation in the UI.
   - When a thread goes quiet for 6 h and has at least 6 messages, v1's `SELF_LEARNING_PROMPT` runs (fast model) and saves at most 3 preference memories.
   - Each lesson carries `source_key` = thread/run and shows "learned from this chat" in the Memory tab.
3. **Taint flag (memory poisoning).**
   - `RunContext.tainted` turns true when a tool returns external content (web search, `fetch_url`, browser, any app read).
   - `remember` gets `ask_if=lambda _: RUN.get().tainted`, so it needs approval only in tainted runs.
   - The background riki-mem extraction receives only your message and the final reply. That already holds, so state it in a test.
   - Upstream fix to riki-mem: the extraction prompt saves only what the user states, never instructions (their improvement note, item 7).
4. **Money list.** In `apps.tool_risk`, tool verbs `transfer|withdraw|place_order|buy|pay|checkout` become a `money` flag on the entry.
   - Money tools are always `write`.
   - They are never covered by a grant (Phase 4), and never approvable from a background run.
   - Kite order placement is `external` outright.
   - The approval card shows the amount if the arguments contain one.

**Exit check:**
- The reflection appears and updates.
- A correction changes behavior in the next session; add an eval case that seeds a lesson.
- A test proves that a page saying "remember that X" does not save X without approval.

### Phase 3: Reaches you (week 3)

1. **Telegram channel** (`app/channels/telegram.py`, about 250 lines ported from v1's `chat/telegram.py`).
   - Long polling, so no public URL. That keeps Riki on localhost, a security win.
   - Exactly one allowed chat id, set in config. The recipient is fixed in config, never chosen by the model, which keeps the "never contacts other people" rule intact.
   - Inbound messages call `start_run` on the **main thread**, so web and phone are one conversation. Replies stream by editing one message at most once a second. Images come in.
   - **Approvals and `ask_user` as inline buttons**, calling the same `policy.decide`. `/stop` maps to the stop endpoint.
   - Outbound `notify(text, run_id)` adds a "why?" link to the run's trace in the web UI.
   - Port v1's Markdown-to-Telegram-HTML formatter and its context-overflow handling. They were learned the hard way.
2. **Reminders and simple schedules.**
   - New `schedules` table: `id, kind (remind|task), text, next_at, repeat (none|daily|weekdays|weekly), status, last_run_id`.
   - Tools: `schedule(when_iso, text, kind, repeat)`, `list_schedules`, `cancel_schedule`. They are `safe`, because the only recipient is you.
   - The heartbeat loop claims due rows with `SELECT ... FOR UPDATE SKIP LOCKED` and handles them by kind:
     - `remind` notifies directly, with no model call.
     - `task` starts a read-only run (`source="schedule"`, no approvals) and notifies you with the result.
   - "Morning brief" is just a `task` schedule preset, so it needs no new feature.
3. **Cognition-lite** replaces the plain heartbeat prompt.
   - Every 4 h, outside quiet hours, and **only if** there are new memories, new conversations or due/overdue items since the last cycle. Otherwise there is no model call.
   - **Think** (smart model, v1's `THINK_PROMPT` trimmed to monologue + updated reflection): inputs are the reflection, new memories, short summaries of recent chats, and upcoming schedules. Output is saved to `reflections`, and the monologue goes into the span.
   - **Act** (fast model, v1's `ACT_PROMPT` trimmed): decides whether to nudge, with what text and what topic.
   - New `nudges` table: `id, topic, text, sent_at, run_id, reaction (replied|thumbs_up|thumbs_down|ignored)`.
   - Guards in code, not in the prompt:
     - quiet hours (`RIKI_QUIET_HOURS` already exists);
     - at most one nudge per 24 h;
     - none if you messaged in the last 3 h;
     - the same topic is skipped for 7 days;
     - **after 3 ignored nudges in a row the interval doubles**, and a reply resets it.
   - "Replied within 12 h" counts as a hit. The hit rate is on the Health tab.

**Exit check:**
- You can chat, approve and get reminders from your phone.
- The cycle makes zero model calls on a day with no new input.
- Over two weeks, at least 40% of nudges get a reply or a thumbs-up. If not, tune the prompt before adding anything.

### Phase 4: Finishes work (weeks 4-5)

1. **Durable runs at depth 0.**
   - Compile the graph with `langgraph-checkpoint-postgres`, keyed by `run_id`, for the main agent only.
   - **Windows lesson from v1:** psycopg's async mode fails on Windows' default event loop. Either run uvicorn with the selector loop policy, or reuse v1's `AsyncWrappedSaver` (the sync saver run in threads). Decide this on day one of the phase.
   - **Approvals become rows, not futures.**
     - `authorize()` looks up `(run_id, tool, args_hash)` first: if it is approved and has run, it returns the stored `result` (idempotent); if it is approved, it executes and stores the result plus `executed_at`.
     - Otherwise the run waits, with `runs.status='waiting'`, on a DB-backed decision with a 24 h timeout. Hourly reminders go to Telegram while it waits.
   - **After a restart,** runs left in `waiting` or `running` resume from their last checkpoint. An already-approved call is never run twice, because of the `args_hash` lookup.
   - **Sub-agents are not checkpointed** (a compromise). If the process dies mid-sub-agent, the parent's `spawn_agent` call re-runs it from scratch. Its writes stay idempotent through the same approval lookup. This trades some repeated tokens for no nested-checkpoint complexity.
   - The 600 s run timeout becomes a compute budget. Time spent waiting for you does not count.
2. **`ask_user(question, options[2-4])`.** Depth 0 only; same parking path as approvals; chips on web, buttons on Telegram. Prompt rule: ask only when a wrong guess is expensive or cannot be undone; otherwise state the assumption and go. Evals: a clear task must not ask; an ambiguous, expensive one must.
3. **Scoped grants.** The approval card gets a third button, "Allow this tool for this task (30 min)". It writes `approval_grants(run_id, tool, expires_at)`. Grants are never available for `external` or money tools. A 20-step Notion job stops needing 20 taps.
4. **Files in.**
   - Uploads (PDF, DOCX, CSV, XLSX, TXT, MD) go to `artifacts` with `kind="upload"`; there is no new table.
   - `read_file(id, start_line, find)` reuses `fetch_url`'s line paging, so all files read the same way.
   - Parsers: `pypdf`, `python-docx`, `openpyxl`. Tables show as a header plus the first rows; full numbers come later from code.
5. **`run_python` sandbox, behind a flag.**
   - Docker with `--network none --read-only --memory 512m --cpus 1`, upload copies mounted read-only, and a 60 s limit.
   - Tier `safe`, because it touches nothing outside the container.
   - Results are text plus files saved as artifacts.
   - If Docker is not available, the tool is not registered. This is last in the phase; skip it if the phase runs long.

**Exit check:**
- Kill the server mid-task and restart it: the task resumes and no write runs twice (a test with the scripted model).
- A 10-step approval-heavy task finishes with at most 2 taps.
- The new **task suite** (10 vague multi-step requests with a rubric, as in your roadmap's "What to measure") gets a first score.

### Phase 5: Cost, speed and trust polish (week 6)

1. **Spend cap.** `RIKI_DAILY_BUDGET_USD`. Past 80%, background jobs pause; past 100%, chat runs at `low` effort and says so once. Every reply shows "3.1 s · $0.004" in its status line, and the Health tab shows cost by source and by model.
2. **Cache truth.** Measure `tokens_cached / tokens_in` per model through your gateway. If the main model gets no cache hits, prefer a main model that does; this often costs less than any prompt trimming.
3. **"Why did you message me?"** Every nudge and schedule firing links to its run's trace: the cycle's monologue, the inputs it saw, and the guard decisions as span metadata.
4. **Model routing review.** Use Phase 0-4 data to decide which jobs need the smart model. Likely only cognition-think, the grounding check and the judges.
5. **If you ever expose it beyond localhost:** a per-install salt for `secret_box`, a pinned resolved IP in `fetch_text`, and server-side session expiry. With Telegram over long polling you may never need to.

**Exit check:** cost per day is visible and capped, the cache ratio is known per model, and every proactive message is explainable in one click.

---

## 6. Key decisions and their trade-offs

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Phone channel | Telegram, long polling, one chat id | Web Push / PWA; WhatsApp | Port exists in v1; works without a public URL; inline buttons give approvals for free. Web Push needs HTTPS + a service worker and has no reply path. |
| Proactivity | Cognition-lite: 2 calls, guards in code, backoff | v1's full 4-phase with SQL signals and attention threads | Threads and signals are where v1's complexity lives and nobody has shown they beat a good reflection. Bring them back only if nudges are too generic after two weeks. |
| Lessons | riki-mem `preference` memories | v1's `session_guides` table | One store, one Memory tab, one delete path. |
| Durable runs | Checkpoint depth 0, idempotent approvals, re-run sub-agents | Checkpoint every sub-agent | Nested checkpoint/resume is the part most likely to break. Re-running a sub-agent costs tokens, not correctness. |
| Approval waiting | DB row + 24 h timeout + Telegram reminder | In-memory future, 5 min | Real tasks happen while you are away. |
| Grounding check | Gated, smart model, quiet UI marker | Every turn, fast model, appended text | Keeps the honesty and removes most of the latency, the cost and the tone damage. |
| Schedules | one-shot + daily/weekdays/weekly | full cron | Covers almost every personal use; cron strings are a parsing and timezone bug farm. |
| Plan visibility | The existing step trace + a prompt rule for multi-step work | A `plan` tool with live checklist | The UI already shows steps live. Revisit if the task suite shows the agent losing the thread. |
| Code execution | Docker sandbox behind a flag | e2b or another hosted sandbox; no sandbox | Local, free, no data leaves; optional so it never blocks startup. |
| Memory poisoning | Taint flag on `remember` | Always ask before `remember` | Asking every time kills the "it just knows me" feel; asking only after untrusted reads is where the risk is. |

---

## 7. Cut list (deliberately not doing)

| Item | Why not now | Bring back when |
|---|---|---|
| Desktop bridge (files, shell, screen) | Largest new attack surface; v1 built one and it was dropped | Phases 1-5 done and the task suite shows local files are the main blocker |
| More app integrations | 30 exist; breadth is not the gap | A real task fails because an app is missing |
| Attention threads, SQL mood/activity signals | Complexity without proven return | Nudge hit rate stays under 40% after prompt tuning |
| OpenTelemetry export | Single user, own trace UI | You run more than one service |
| Presidio / reversible PII masking | Big, fragile, breaks reasoning on masked names | You switch to a model provider you do not trust with personal data |
| Multi-user, hosting, Neon/Supabase move | Changes every assumption in the policy and the data model | You decide to ship Riki to other people |
| Drafts outbox page | Gmail drafts already exist; low frequency | Drafting becomes a weekly habit |
| LongMemEval runs | Expensive, not on the user's path | You need a public number for riki-mem |
| Plan tool, avatar/UI polish | No measured problem | The task suite or real use says otherwise |

---

## 8. Data model changes (Alembic, one migration per phase)

| Migration | Change |
|---|---|
| `0009_cost` | `runs.cost_usd` (numeric, nullable) |
| `0010_reflections` | `reflections(id, content, memory_count, run_id, created_at)` |
| `0011_schedules_nudges` | `schedules(...)`, `nudges(...)` as in Phase 3 |
| `0012_durable_runs` | `approvals.args_hash, executed_at, result, expires_at`; `approval_grants(run_id, tool, expires_at)`; `runs.status` adds `waiting`; LangGraph checkpoint tables are created by the saver (excluded from Alembic, like riki-mem's) |

The migration test already fails if models and migrations disagree.

---

## 9. What gets measured (Health tab additions)

- Cost per day by `source` (chat, schedule, cognition, eval) and by model; spend vs cap.
- Latency p50/p95 for small talk and for normal answers; time to first token.
- Cache ratio per model.
- Nudges: sent, replied, ignored, hit rate, current backoff interval.
- Voice evals pass^3, case evals pass^3, task-suite score, each by prompt version and model.
- Taint events: how often `remember` was asked for approval, and how often you denied it (a direct measure of attempted poisoning).

---

## 10. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Nudges feel spammy | Medium | Code guards, backoff, hit-rate metric, one-tap "less of this" that lengthens the interval |
| Checkpoint resume corrupts a conversation (v1 hit unmatched tool calls) | Medium | Checkpoint only depth 0; on resume with dangling tool calls, re-run the tools node; a test kills the run mid-tool |
| Windows event-loop incompatibility with psycopg async | High if ignored | Decide selector loop or threaded saver on day one of Phase 4 |
| The A/B says the model, not the prompt, is the problem, and the better model costs more | Medium | Small-talk path is one call; route background jobs to cheaper models; spend cap |
| Telegram token leak gives someone your bot | Low | Allowlist one chat id; token in `.env`; rotate via BotFather; bot only ever talks to that id |
| Reflection drifts into wrong beliefs about you | Medium | It is visible and editable; edits become memories; every cycle is a span |

---

## 11. Order of work at a glance

| Week | Ship | You can now... |
|---|---|---|
| 0 | git, baseline, cost per run, voice A/B | trust your changes and see cost |
| 1 | persona default, core block in small talk, history by size, quiet grounding, voice evals | talk to v2 and feel v1 |
| 2 | reflection, lessons, taint flag, money list | see and correct what Riki thinks of you |
| 3 | Telegram, reminders, cognition-lite | get pinged, reminded and asked from your phone |
| 4-5 | durable runs, `ask_user`, grants, files in, sandbox (flag) | hand it a task and walk away |
| 6 | spend cap, cache check, "why did you message me?" | run it daily without worrying about bills or surprises |

## 12. What to revisit as it grows

- If more than one person ever uses it: tenant ids in every table, per-user policy, hosted auth. The single-user assumption is everywhere today, on purpose.
- If tasks routinely exceed an hour: move runs from in-process tasks to a small job queue (Postgres `SKIP LOCKED` is enough; no Redis).
- If memory passes ~50k items: watch riki-mem recall latency against the 8 s time box before anything else.
