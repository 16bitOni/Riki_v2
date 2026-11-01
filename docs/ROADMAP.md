# Roadmap: from a chat that answers to an agent that finishes the work

Where Riki v2 is (October 2026), what stops it from doing real work end to end, and the order to fix that in.
Each item names the code it touches, so it can start as a small change.

## Status (2026-11-01)

Built since this page was written: approvals as database rows that survive a restart (a waiting request is resumed and an
action that already ran is not run twice), `ask_user`, "allow for 30 minutes" grants, a money rule, the taint gate on
`remember`, reminders and scheduled tasks, Telegram, file uploads with `read_file`, an optional Docker sandbox, a living
read on you with lessons and a nudge cycle, cost per answer and a daily budget. Still open: a visible plan checklist,
a desktop bridge, and a task suite (10 vague multi-step requests with a rubric) to measure whether Riki finishes work.

## Where it stood

Strong already: risk tiers enforced in code, nested sub-agents with budgets, full traces, citation and grounding
checks, evals with pass^k, ~30 apps through MCP, artifacts (Markdown, designed pages, PDF), check-ins, and now
setup (role, apps, read-only scan, facts you tick).

What it can't do yet, and why:

| Gap | Why, in the code |
|---|---|
| Work that takes longer than 10 minutes, or survives a restart | One run = one asyncio task, `RIKI_RUN_TIMEOUT_S=600`; no checkpoint, so a restart ends it (`ops.interrupt_stale_runs`) |
| Finish while you're away | Approvals are in-memory futures that time out after 5 minutes (`policy._PENDING`); check-ins can never be approved |
| Turn a vague ask into a plan you can see and steer | No plan object: the model plans inside its context; the UI shows steps only after they happen |
| Ask one good question instead of guessing | The main agent has no way to ask you something mid-run except ending the turn |
| Work with your files and numbers | Only images can be attached; no code execution, so no CSV, PDF or XLSX analysis and no spreadsheet output |
| Do things on your computer | No local bridge yet (the filesystem MCP server reads one folder, and nothing more) |
| Remind you, or run something at 9:00 | Check-ins are a fixed interval; no scheduler, no notification channel |

## Now (each is days, not weeks)

### 1. Tasks that outlive a turn
The single change that makes "end to end" possible.
- Add the LangGraph Postgres checkpointer (`langgraph-checkpoint-postgres`) to `Agent.graph`. A run then resumes
  after a restart, from its last step, instead of being marked interrupted.
- Persist approvals: keep the `approvals` row as the source of truth and let a waiting run *park* (checkpoint, end
  the task) instead of awaiting a future. Deciding in the Approvals sheet resumes it. This removes the 5-minute
  timeout and lets you approve from your phone hours later.
- A `jobs` view = runs with `status in (running, waiting)`; Home already shows "needs your OK".
- Keep the budgets (tokens, agents, depth). Add a per-job wall-clock budget instead of the 600 s run timeout.

### 2. A visible plan for vague requests
- New tool `plan(steps: list[str])` (safe, depth 0): the agent writes a 3-7 step checklist before multi-step work and
  ticks items with `plan_update(i, status)`. Both are spans, so the Island and the chat show a live checklist the
  way the trace shows tools now. Prompt rule: plan only when the work has more than two steps.
- Assume-and-say over ask: for a vague request, take the most reasonable reading, state the assumptions in the plan,
  and start. Ask only when a wrong guess is expensive or can't be undone (next item).

### 3. `ask_user`: one question, with choices
- A tool that renders 2-4 choice chips in the chat and parks the run like an approval (same mechanism as item 1).
  It is how "plan my trip" becomes "which weekend: 8th or 15th?" instead of a wrong itinerary.
- Eval cases: a clear ask must not trigger a question; an ambiguous, expensive one must.

### 4. Files in, files out
- Accept PDF, CSV, XLSX and DOCX uploads (store in `artifacts` with `kind="upload"`), readable by a `read_file` tool
  that returns text or a table preview.
- `run_python` in a sandbox (a throwaway Docker container with no network and a 60 s CPU limit; e2b if you'd rather
  not run Docker). Tier: `safe`, because it can only touch the upload copies. This is what lets the finance and
  coding cases below work on real numbers instead of estimates.
- `save_sheet` for CSV/XLSX output, next to `save_markdown` and `save_artifact`.

## Next

### 5. Reminders, schedules and a notification channel
- `schedule(when, what)` tool and a `schedules` table; the heartbeat loop already ticks every minute, so it becomes
  the scheduler. "Every weekday 9:00" and "in 2 hours" both fit a cron string plus a next-run time.
- Notifications to you only: Web Push from the PWA (the UI is already mobile-shaped), or a Telegram bot that talks
  only to your chat id. Both fit "never contacts other people" because the recipient is fixed in config, not chosen
  by the model.

### 6. Drafts outbox
Everything that would reach another person (mail, messages, comments, invites) becomes a draft in one place with
"Open in Gmail" / "Copy". It makes the hard rule pleasant instead of a refusal. Gmail already has `create_draft`
(write tier); the outbox is a view over drafts Riki made plus a `draft_message` tool for apps without drafts.

### 7. Knowing you, continuously
- A **Profile** page generated from memory: work, people, routines, preferences, with "why do you think this?"
  linking to the source run (`source_key` is already stored).
- Learn from corrections: a thumbs-down with a comment becomes a style preference memory ("shorter answers about
  code"), confirmed in one tap.
- Opt-in weekly re-scan (the setup scan as a scheduled job) that proposes new facts and flags stale ones; nothing
  is saved without a tick, as in setup.

### 8. The desktop bridge
A small local companion (one Python process, MCP over localhost with a token) instead of a cloud-to-PC tunnel:
- **Files**: folders you grant one by one; reads are safe, edits are write, deletes are write with a list of files.
  Credential folders (`.ssh`, browser profiles, password managers) are never grantable.
- **Shell**: an allowlist of commands (`git status`, `npm test`, `uv run pytest`...) is safe; anything else is
  write, shown as the exact command line.
- **Screen**: screenshot + click/type for apps without an API, always write, with the screenshot on the approval
  card. This is the "Desktop Operator" loop (vision, plan, act, observe) on top of the same tiers.
- A visible "Riki is using your computer" pill and a global stop hotkey.

## By the kind of person using it

Each role in `apps.json` should map to a pack: apps to connect, starter requests, a check-in list (all done in
setup), plus the flows below. They need items 1-4 first.

| Who | End-to-end flows | Needs | Guardrails |
|---|---|---|---|
| Developer | "Why is CI red?" (read the logs, find the commit, explain); "fix the failing test" (clone into the sandbox, patch, run the tests, open a *draft* PR); morning digest of PRs, deploys and Sentry | GitHub, Sentry, Vercel; sandbox; desktop bridge for local repos | Draft PRs on your own repos could move from external to write: they reach nobody until you mark them ready. Comments and reviews stay external |
| Finance / investing | Upload statements, then categorise spending, monthly report and XLSX; portfolio move explained with sources; bills due from mail; price alerts | Kite (read), Morningstar, uploads, `run_python`, schedules | Add a **money tier** next to external: order, transfer, pay and withdraw verbs are never run, in any mode. Numbers come from code, never the model's arithmetic. Says "not financial advice" in code, like the secret notice |
| Founder / operator | Inbox triage into drafts; meeting prep brief (calendar + mail + Notion) 15 minutes before each meeting; weekly update as a doc | Gmail, Calendar, Notion, Linear, Stripe | Drafts only; calendar events with attendees stay write with the attendee list shown |
| Student | Study plan from the syllabus PDF and calendar; quizzes and flashcards as artifacts; deadline reminders | Drive/Docs, Calendar, uploads, schedules | None beyond the defaults |
| Everyday | Food and grocery orders built for you to confirm; trip plans; renewals and bills from mail | Swiggy, Instamart, Gmail, Calendar | Checkout is write with the total on the card; a daily spend cap in code |

## Security, next steps

Already strong. In order of value:
1. **Durable, scoped approvals** (with item 1): "allow `notion_update_page` for this task" for 30 minutes, so a
   20-step job doesn't need 20 taps; never for external or money tools.
2. **Money tier**, as above: a tool-name verb list like `apps.reaches_people`, checked for every app and hub.
3. **Taint beyond URLs**: the new URL guard stops made-up addresses; apply the same "has this run read untrusted
   content?" flag to write tools, so after reading a web page or an email the approval card says so in red.
4. Close the documented gaps when Riki leaves localhost: pin the resolved IP in `fetch_text` (DNS rebinding), a
   random per-install salt for `secret_box`, server-side session expiry and logout.
5. An injection eval set: 20 hostile pages, emails and files (instructions, fake tool output, exfil links), run with
   `--repeat 3` on every prompt change.

## What to measure

The evals already give pass^k. Add a small **task suite**: 10 vague, multi-step requests with a rubric for the
finished result ("a report I could send as is"), the number of questions asked, approvals needed, tokens and wall
time. Track it next to the case suite on the Health tab; that number is the one that says whether Riki finishes
work.
