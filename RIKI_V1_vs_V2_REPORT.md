# Riki v1 vs Riki v2: what each one really is

Written 2026-10-31. Direct analysis, no cheerleading. Everything below comes from reading the code and docs on your machine, plus a read of the riki-mem library on GitHub. I did not run either app, and I did not run the test suites (no database, no gateway in this environment). Where I am guessing, I say so and give a rough probability.

## 0. Which folder is which

You gave me two folders. They are not what you described, so this matters before anything else.

| Name you used | Where it actually is | What is in it |
|---|---|---|
| "Riki" (v1, humane, works well) | **Not in the `Riki` folder.** `Riki` holds only `environment.yml` and `experiments.ipynb` (a laya router test and a mempalace memory test). No agent code. | The real v1 is `Persoanal agent/personal-agent` (git repo, 11 commits, last edited Oct 23). I read that. |
| "Riki_v2" | `Riki_v2` | Full app: ~3.3k lines of backend Python, ~4.8k lines of React, 8 migrations, ~92 test cases, 21 live evals, 7 docs. **No git repo.** |
| (not mentioned) riki-mem | github.com/16bitOni/Riki-mem | Your memory library, which v2 uses. ~13.5k lines, 289 tests per its README, alpha 0.2.0. Its README says it was "extracted from Riki". |

One more correction to the story: v1 is **not "not advanced"**. It is about 10k lines of Python with a four-channel memory system, a background "thinking" loop, Telegram, reminders, habits, a trace dashboard and a LongMemEval harness. Its weakness is not sophistication. Its weakness is that it has no guardrails and no runtime discipline. That is the thing v2 added.

---

## 1. Verdict

1. v2 is a better **runtime**: safer, more observable, more testable, more extensible. v1 is a better **companion**: it has the layer that makes it feel like someone who knows you and thinks about you between chats.
2. v2 did not upgrade v1. It replaced the agent loop and **dropped the "alive" layer** (reflection, session lessons, cognition cycle, nudges, habits, reminders, Telegram). Those were not extras. They are most of why v1 feels human.
3. v2 also changed the model (Claude Haiku 4.5 at temperature 0.7 in v1; Kimi K3 at provider-default temperature in v2), shrank the conversation context (full checkpointed history vs last 8 text-only turns), and made persona an optional text box. So the "humane" regression has at least four suspects changed at once, which makes it impossible to blame any single one without a test.
4. v2's own eval for small talk (`small-talk-hi`) demands that the reply "does not recite memories". v1's persona is built on the opposite instinct ("wait, didn't you say last week..."). The test suite is enforcing a different personality than the one you want.
5. Biggest process risk: **v2 has no version control.** 146 files, and the "backup" is a copied folder with patch files.
6. My rough bet (judgment, not measured): about 75% that most of the "humane" gap is prompt + model + context, and only about 25% that it needs architectural work. About 50% that porting v1's voice prompt into v2's Persona field closes most of it, about 35% that you also need to put Haiku 4.5 (or similar) back as the main model. A one-hour A/B settles it (section 9).
7. Nothing was wasted. v1 + v2's runtime is the right product. The mistake was rewriting instead of porting.

---

## 2. The black box, opened: one message in v1

Files: `app/chat/service.py`, `app/agents/graph.py`, `app/memory/*`, `app/cognition/*`.

```
Telegram / HTTP  ->  run_turn()
   |
   v  LangGraph:  trim -> recall -> assistant -> END
   |
   |-- trim:    caps any tool output at 8,000 chars; trims history above 120,000 chars or 100 messages
   |
   |-- recall:  (1) ONE LLM call turns your message into a "retrieval plan"
   |                 (keywords, a semantic query, entities, relation words like "sister", a time window)
   |            (2) Four searches run in parallel:
   |                 keyword  = Postgres full-text
   |                 semantic = pgvector (only for memories marked preference/episode by default)
   |                 time     = memories inside the date window
   |                 graph    = walks entity relations up to 3 hops ("sister's husband's company")
   |            (3) Weighted Reciprocal Rank Fusion (weights 1.0 / 1.0 / 0.7 / 0.5)
   |                 + a small importance boost -> top 8 memories
   |            (4) Adds: current time, "last message N hours ago", your profile paragraph,
   |                 the last 5 "lessons from past sessions", and the latest "Riki's current read on Subhadip"
   |                 (the reflection document)
   |
   |-- assistant: ONE ReAct agent, ~25 tools in a flat list, system prompt = the RIKI persona + all of the above
   |              (Claude Haiku 4.5 at temperature 0.7, via your local gateway on :8787)
   v
reply  ->  saved to Postgres  +  immutable JSON copy in the archive (S3 in your .env)
```

What happens when you are NOT talking (this is the part v2 lacks):

- **Session close** (24 h of silence, `/new`, shutdown, or a 20 h Telegram gap): one model reads the whole conversation and writes memories; another writes a "self-guide" of lessons about how to work with you (corrections you made, what you liked). Those lessons are injected into later prompts.
- **Reflection**: a living ~450-word "current read" on you (right now / open loops / questions I'm sitting with / patterns), regenerated when new memories exist, injected into every prompt.
- **Cognition cycle** (default every 4 h; README says 8 h, the config says 4): four phases. SQL computes signals (mention rates vs baseline, mood, activity). An LLM interprets them, continues a private **monologue** that reads its last 3 entries, tracks "attention threads" (hypotheses it is watching), and rewrites the reflection. A separate, deliberately conservative call decides whether to message you. Dedup against recent nudges, quiet hours 23:00-07:00, minimum 48 h between nudges, and it tracks its own reach-out accuracy.
- **Nudges, reminders, habits** go out through Telegram.

Two facts that surprised me while reading:

- `extract_and_save` (per-turn memory extraction, documented in the README as the way memory gets saved "in the background") **is never called anywhere**. Memory is written only at session close (or when the model calls the `remember` tool). So something you tell it at 10:00 is not in long-term memory until the session closes, possibly 24 h later. Within the session it is fine, because the full conversation is checkpointed. If the process dies before close, crash recovery is supposed to pick the session up, but I did not test that.
- The recall planner is an LLM call on **every** message, including "hey". It uses the main model (`fast=False`). That is real latency and cost for no benefit on greetings.

## 3. The black box, opened: one message in v2

Files: `backend/app/turn.py`, `agent/graph.py`, `agent/policy.py`, `agent/tools.py`, `memory.py`, `tracing.py`, `checks.py`.

```
POST /api/chat  ->  start_run(): an asyncio task owned by the SERVER, not the browser tab
   |
   v  run_turn():
   |-- small talk? (regex: hi / thanks / ok ...)  -> NO memory lookup, NO tools, one model call
   |
   |-- otherwise: memory span.  riki-mem context() with an 8 second time box:
   |       regex-planned search (no planner LLM call), rerank off
   |       -> profile + relevant memories + computed counts
   |
   |-- builds ONE user message:  <context> Now: ... + memories </context> + your text
   |       (time and memories are in the USER message so the system prompt stays byte-stable for caching)
   |       history = last 8 turns, TEXT ONLY (old tool output never comes back)
   |
   |-- Agent.run(): LangGraph with two hand-written nodes, "model" and "tools"
   |       model: Kimi K3 via your gateway, provider-default temperature
   |       tools: all tool calls of a step run in parallel; each passes through authorize():
   |             safe     -> runs
   |             write    -> parks on an approval card in the UI (5 min), denied if you do not answer
   |             external -> never offered to the model, always refused
   |       guards: repeated identical call refused, old tool results replaced by stubs past a size budget,
   |               outputs cut at 6,000 chars, last step gets no tools and must answer
   |       spawn_agent: runs the SAME graph one level down with a clean context
   |
   |-- final text  ->  grounding check (one MORE model call that lists unsupported claims)
   |                   + citation check (are [t3] / [m:abcd1234] real?)
   |                   + secret scrub
   |                   "Not verified: ..." is appended to the reply if the checker flags anything
   |-- saves run + every span + messages in one write
   |-- background: riki-mem remember() learns from the turn (fire-and-forget)
   v
SSE stream to the UI; the Island, the trace tree, and "worked for 3.5 s" all read from the same span list
```

Default limits you actually get: effort defaults to **medium**, which caps depth at 1, sub-agents at 3, steps at 8, tokens at 120k. The headline "nested to depth 3, 12 agents, 300k tokens" only applies when a message asks for **high** effort.

---

## 4. Side by side

| Dimension | v1 (`personal-agent`) | v2 (`Riki_v2` + riki-mem) | Edge |
|---|---|---|---|
| Agent shape | One ReAct agent, ~25 flat tools. You deliberately removed supervisor + specialists (your own analysis showed 5-8 model calls per simple question). | One graph, two nodes, nested `spawn_agent` sub-agents with clean contexts and budgets. | v2 on capability, v1 on simplicity. Removing the supervisor in v1 was correct. |
| Personality | A long, specific persona prompt with a register table ("i'm tired -> yeah you've been running on fumes, what happened today"). | Generic default voice ("warm, direct and honest... short by default"); real personality only if you fill the Persona text box (stored in the DB, which I cannot see). | **v1, clearly** |
| Model | Claude Haiku 4.5, temperature 0.7. Fast model GLM-4.7-flash. | Kimi K3, temperature unset. Smart model GLM-5. | Unknown. Model quality for voice is a top suspect. |
| Conversation context | Full LangGraph state checkpointed in Postgres (tool calls and images too), up to 100 messages / 120k chars. | Last 8 turns, text only. | v1 for continuity; v2 for cost. |
| Memory write | At session close, by a model that sees the whole conversation. Plus the `remember` tool. | After every turn, in the background, via riki-mem (with `turn_key` idempotency). | v2 is fresher; v1 sees more context per extraction. |
| Memory read | LLM planner on every message -> 4 channels -> RRF -> top 8. | Regex planner, 8 s time box, small talk skips it. riki-mem adds deterministic counts/sums/date math from stored rows. | v2 on speed and on "how many" questions; v1 on planning quality. |
| Memory lifecycle | Soft delete, supersede, trigram + vector dedup, embedding-model tag with a CHECK constraint, S3 archive that can rebuild everything. | Newer fact retires older, hard delete, JSONL export/import, per-model vectors, user control in a Memory tab. | Roughly even. riki-mem is the evolved version of v1's memory. |
| "Thinking between chats" | Reflection + 4-phase cognition cycle + monologue + attention threads + nudges. | A heartbeat that runs a plain checklist prompt, never gets approvals, and has no way to reach your phone. | **v1, by a mile** |
| Channels | Telegram (long polling, chat-ID allowlist), web chat page, dashboard. | Web app only (very polished). No push channel (your roadmap lists it as missing). | v1 for daily-driver feel; v2 for UI. |
| Tools | Fixed list: calendar, Gmail (read + draft only), Drive, web, images, weather, tasks/notes, reminders, habits, memory. No MCP. | Built-ins + MCP (filesystem, Playwright, GitHub) + ~30-app catalog + MCP registry + custom MCP link. Loaded on demand from a one-line catalog. | v2 |
| Safety model | Prompt rules + "EXTERNAL CONTENT" text markers + a regex PII scrubber + Telegram allowlist. Writes run on the model's say-so. Email has no send tool. | Risk tiers enforced in code (safe / write / external), fail-closed, approvals, SSRF guard, URL-exfil guard, secret scrub on input and in traces, encrypted tokens, sandboxed artifact iframe. | **v2, clearly** |
| Budgets | `recursion_limit=30` only. No token or cost cap. | Depth, sub-agent count, steps, tokens, per-tool timeout, whole-run timeout, per-message effort. | v2 |
| Observability | Trace/span store + a static-JS dashboard: latency percentiles, retrieval ranking by channel, cost estimates, alerts. | Trace tree per answer, citations, daily drift signals, Health tab. | Even. v1's retrieval explainer is excellent. |
| Evals | Harnesses for retrieval, extraction, end-to-end, cognition-act, plus LongMemEval (247 MB dataset downloaded; I found no stored results). | 21 live cases with pass^k, regression detection (exit code 1), run on every prompt change by rule. | v2 for discipline; v1 for ambition. |
| Tests | ~140 test functions (17 under `tests/eval` appear to call a real model). Pytest cache from Oct 21 records 6 failures; many edits since. | ~92 cases against real Postgres with a scripted model. Cache from Oct 28 07:38 suggests a clean last run, 1-3 minutes after the last edits to `turn.py` and `test_agent.py`. | v2 (known-good state) |
| Reliability of runs | Standard request/response; checkpoint corruption on a crash mid-tool-call is detected and handled in the Telegram path. | Runs belong to the server; refresh, tab switch, disconnect do not lose a reply; Stop works; restart closes dangling runs honestly. | v2 |
| Ops / deploy | Fly.io config + GitHub Action, Dockerfile, S3 archive, Postgres keepalives. | Docker Compose for local Postgres; localhost-only by design. | v1 (it is actually deployed-shaped) |
| Version control | Git, 11 commits, some uncommitted work. | **None.** | v1 |
| Docs | Large, partly stale (section 8). `docs/` is in `.gitignore`, so none of it is versioned. | Clean and mostly accurate, with a written rules file the code follows. | v2 |

---

## 5. Why v1 feels human and v2 probably does not

Ranked by how much I think each matters. The ranking is my judgment; none of it is measured.

1. **Persona prompt (high).** v1's prompt is a character: dry humor, opinions, "say it once then help anyway", banned openers ("Great!", "Sure!"), a register table with examples. v2's default is three bullet points. If the Persona box in v2 is empty or short, you are comparing a written character with a generic assistant.
2. **Small talk gets no memory (high for the "friend" feel).** In v2, "hey" / "thanks" skip the memory lookup entirely, so the model has no profile, no reflection, and cannot say "didn't you say last week...". v1 injects the profile and the reflection on every turn, greetings included. v2's own `small-talk-hi` eval requires "does not recite memories", so the suite rewards the cold behavior.
3. **The reflection block (high).** v1 hands the model a fresh private note on you every turn: what is on your mind, open loops, questions it is sitting with. v2 has no equivalent. This is what lets v1 sound like it was paying attention.
4. **Model and temperature (medium-high, unknown).** Haiku 4.5 at 0.7 vs Kimi K3 at its default. I cannot judge Kimi K3's voice from here. This is the cheapest thing to test.
5. **Context length (medium).** v1 keeps the entire session; v2 keeps 8 text-only turns plus retrieved memories. Long banter threads lose texture in v2.
6. **The "Not verified" footer (medium).** After most answers v2 runs a second model call and, if it flags anything, appends a warning block. Great for trust, bad for warmth. It also uses the same fast model to grade its own answer.
7. **No proactivity (high, but it is a feature gap, not a tone gap).** v1 messages you first. v2 cannot: no phone channel, and its heartbeat runs with approvals disabled and nobody to notify.

What does **not** explain it: v2's architecture (graph, sub-agents, tiers). None of that touches voice.

Test before you build anything more (30-60 minutes, uses only what exists):

- Take 20 real messages from your v1 archive (`data/archive`). When you paste the persona in (B), drop the two lines that mention the "current read" block and "thinking between conversations", since v2 has neither yet.
- Run them through v2 under three configs: (A) as is; (B) paste v1's persona (everything before `Rules:`) into the Persona box; (C) B plus `RIKI_MODEL=openai:global.anthropic.claude-haiku-4-5-20251001-v1:0` and a temperature around 0.7.
- Judge blind against v1's replies. If B closes it, it was the prompt. If only C does, it was the model. If neither, it is the context/reflection layer.

---

## 6. Riki v1: where it aces, where it fails

### Aces

- **The voice.** The persona prompt is the most valuable single asset in either project. It is specific, testable, and it is why you like the answers.
- **The "alive" layer.** Reflection, session lessons, cognition cycle, nudges. The design is thoughtful: SQL counts and the LLM only interprets; thinking and acting are separate calls; most cycles correctly end in "do not reach out"; dedup, quiet hours and an accuracy track record keep it from being annoying. Few personal-agent projects have this.
- **Memory design.** Four retrieval channels, a graph walk with a fan-out cap so it cannot explode, supersede-not-delete, trigram + vector dedup that never merges different dates, vectors tagged with their embedding model (a CHECK constraint rejects untagged ones), embeddings only where they pay off, and an immutable archive that can rebuild the whole index.
- **Learning from your corrections.** Session guides turn "stop doing that" into a lesson injected into future prompts.
- **It lives on your phone.** Telegram, reminders, habits, calendar, Gmail drafts. It is a daily driver, not a demo.
- **Retrieval explainability.** The dashboard shows which channel found which memory and how it ranked. That is better than v2's trace for debugging memory.
- **Removing the supervisor.** Your own written analysis found 5-8 model calls per simple question and fixed it. Right call.

### Fails

| # | Problem | Why it matters |
|---|---|---|
| 1 | **No approval layer.** `create_calendar_event`, `forget_memory`, `remember`, `set_reminder`, task and habit tools all run when the model says so. The only defense against prompt injection is text markers and prompt wording. Your own `SECURITY.md` says it is "best-effort". | One malicious email read by the agent could delete memories or create events. Email has no send tool, which limits the worst case. |
| 2 | **No budgets.** `recursion_limit=30` is the only brake. No token cap, no cost cap, no per-turn deadline. | A loop or a huge tool result costs real money before anyone notices. |
| 3 | **Memory is written only at session close.** `extract_and_save` is dead code. | Up to 24 h before something you said is searchable in a new chat; if the close job fails, the session's facts can be lost. The README says the opposite. |
| 4 | **LLM planner on every message.** Even for "hey". | Added latency and cost on exactly the messages that should be instant. |
| 5 | **Docs describe code that is gone.** README says AWS Bedrock; the code is an OpenAI-compatible client (pointed at your gateway). `SECURITY.md` describes Bedrock timeouts in `llm.py` that no longer exist. `scripts/desktop_operator.py` imports `app.desktop`, which is not in the repo. `docs/` is git-ignored. | You cannot trust your own documentation, and nobody (including you in a month) can tell what is real. |
| 6 | **Aadhaar check uses Luhn.** Aadhaar numbers use the Verhoeff checksum, so most real Aadhaar numbers will fail a Luhn test and the scanner will skip them. (Based on how Aadhaar numbers are specified; I did not run it.) | The PII scrubber gives false confidence for one of the identifiers it names first. |
| 7 | **Checkpointer hardcodes `sslmode=require`** and ignores your own `db_ssl=False` setting. | A local Postgres without SSL (your own docker-compose) will not work with the checkpointer. |
| 8 | **Test state unknown.** The pytest cache is from Oct 21 and records 6 failures, including a test for a desktop operator that is no longer in the repo. 5 files modified and not committed. `tests/test_chat_flow.py` patches `routes.extract_and_save`, which `routes.py` no longer has, so those tests very likely fail. | You do not know whether v1 is green, and probably it is not. |
| 9 | **API is open if `API_KEY` is empty.** Documented, not fixed (your current `.env` does set one). | Anyone who can reach the port can chat, read and delete memories. |
| 10 | **Traces store full message text by default,** and chat archives are plaintext JSON. | Fine for one laptop; weak once on Fly.io + S3 (which is what your `.env` points at). |
| 11 | **Single process.** Telegram polling, four background loops and the API share one event loop; the checkpointer is a sync driver run in threads. | One crash takes down everything; cannot scale out. Acceptable for one user. |

---

## 7. Riki v2: where it aces, where it fails

### Aces

- **Runtime engineering.** Runs owned by the server (survive refresh, tab switch, dropped connection), event replay from any index, Stop that also cancels a run waiting on approval, honest "interrupted" messages after a restart. Loop breaker for repeated identical calls, stub-out of old tool results so cost does not grow quadratically, truncation of huge outputs, a forced "answer now" on the last step, retry on provider empty-stream errors.
- **Safety by construction.** Unknown tool means `write`; approval timeout, stop or restart means "not approved"; `external` tools are never shown to a model; hubs like Zapier get their action arguments inspected; `fetch_url` and browser navigation only go to a URL you typed or that a search/page showed (blocks "open evil.site/?d=<your mail>"); SSRF check re-validates every redirect hop; keys pasted into chat are stripped before the model, memory, database or title ever see them; app tokens are encrypted; the avatar upload checks magic bytes; agent-written HTML pages are served in a CSP sandbox with no same-origin access. This is real engineering.
- **Traceability.** Every agent, model call, tool call and memory lookup is a span in a tree. Citations like `[t3]` and `[m:1fdab01f]` are checked against that tree, plus a grounding check on claims. "Why did you say that?" has an answer.
- **Discipline.** `RULES.md` and `CLAUDE.md` are short, specific and actually followed. Prompts are hashed into `PROMPT_VERSION`. Evals use pass^k and exit non-zero on regression. Migrations are enforced by a test, and the app refuses to start on a schema mismatch.
- **Extensibility.** OAuth with PKCE via the MCP SDK, ~30 catalogued apps, the MCP registry, custom links, and tools loaded on demand from a one-line-per-app catalog (ten apps cost ten lines, not 150 tool schemas).
- **Latency thinking.** Small-talk bypass, regex-planned memory, an 8 s memory time box, byte-stable system prompt for caching, parallel tools and sub-agents.
- **Honest roadmap.** `ROADMAP.md` lists exactly what it cannot do. That page is accurate.

### Fails

| # | Problem | Why it matters |
|---|---|---|
| 1 | **No git.** 146 files, `_backup_2026-10-28/` plus `changes.patch` as the history. | One bad edit or disk problem and there is no way back. Fix first. |
| 2 | **It lost the human layer.** No reflection, session lessons, cognition, nudges, habits, reminders or Telegram. The heartbeat is a plain prompt with approvals disabled and no way to notify you. | The thing you said you value most is the thing it cannot do. |
| 3 | **Personality is optional and small talk is amnesiac.** See section 5. | Likely the main cause of "does not feel like Riki". |
| 4 | **The test suite enforces cold behavior.** `small-talk-hi` requires "does not recite memories". The only voice test (`greeting`) is a loose "warm and brief" rubric graded by the same fast model. Going by the 21 case names and the ones I read, nothing grades register, opinions or teasing. | You are measuring safety and speed carefully and voice not at all. |
| 5 | **Grounding check on every non-trivial answer.** One extra model call and possibly a "Not verified" block. Self-graded by the same model. | Adds latency and cost, can make answers feel clinical, and a model grading its own output is a weak check. |
| 6 | **The headline depth is not the default.** Medium effort = depth 1, 3 sub-agents. | The "graph of agents" you built is mostly off unless a message asks for high effort. |
| 7 | **It cannot finish long work.** One run = one task with a 600 s limit; approvals are in-memory and expire after 5 minutes; no checkpoint, so a restart ends the run; no scheduler; no file upload or code execution; no `ask_user`. Your roadmap says all of this. | "Complete vague tasks end to end" (your stated goal) is blocked by missing basics, not by missing apps. |
| 8 | **Memory poisoning path.** `remember` is a `safe` core tool, so an injected web page can get a "fact" saved and replayed into later prompts. Only the prompt stops it. riki-mem's own notes list the same gap (item 7). | A prompt-only defense in a project whose rule is "safety lives in code". |
| 9 | **Artifact pages can leak through images.** The sandbox CSP blocks scripts from calling out, but allows `img-src https:`. An injected agent could write a page whose image URL carries data, and opening the page sends it. | Needs injection plus you opening the page. Low-medium. Drop `https:` from `img-src` or proxy images. |
| 10 | **`main.py` is 1,001 lines** holding every endpoint. | Hard to review, and it is where new features pile up. |
| 11 | **Clutter and drift.** `backend/.playwright-mcp/` (logs and page dumps), `rfs_snapshot.yml` (a stray browser snapshot of a Y Combinator page), `_backup_*`, `.pytest_cache`. `DATABASE_MIGRATION.md` says Alembic head is `0005`; the real head is `0008`. | Small, but it is how docs rot. |
| 12 | **Depends on an alpha library.** riki-mem is 0.2.0; its README says no benchmark score is published yet. | The memory layer is promising but unproven; v1's memory had a LongMemEval harness started and no result either. |
| 13 | **Breadth before depth.** 30 app integrations, onboarding scans, PDF/HTML artifacts, GIF avatars exist before reminders or a push channel do. | Effort went to what demos well, not to what makes it a friend. |
| 14 | **Known, documented security limits.** Fixed-salt key derivation, DNS-rebinding gap, pattern-based redaction. | Fine on localhost; fix before exposing it. |

---

## 8. Where you went wrong

Said plainly, because you asked for the black box and not encouragement.

1. **You rewrote instead of porting.** v1's agent loop was the weak part; its persona, reflection, cognition and Telegram were the strong parts. v2 rebuilt the weak part beautifully and left the strong parts behind. A port (keep v1's "alive" layer, replace its loop and safety model with v2's) would have kept both.
2. **You changed everything at once.** New loop, new model, new memory library, new persona handling, new context policy. When it feels less human you cannot say which change did it. That is why the A/B in section 5 is the next step, not more code.
3. **You measured what is easy to measure.** Latency, tokens, citations, injection, budgets all have evals. Voice, warmth and "answers like I want" have one weak test. Whatever you do not measure drifts, and in v2 it drifted away from the goal.
4. **You spent the first rewrite on the wrapper.** The UI, the app catalog and the artifact generator exist; reminders, a notification channel and long-running tasks (your own roadmap's #1 and #5) do not. A friend that cannot message you first and cannot remember to remind you is not yet a friend.
5. **You let docs and code diverge, in both projects.** v1's README and `SECURITY.md` describe Bedrock code that is gone and a per-turn extractor that is never called. v2's migration note and "any depth" headline do not match the code or the defaults. Your own `CLAUDE.md` rules fix this for v2, but v1 never had them.
6. **No version control in v2.** For a project with this many files and this rule set, that is the one genuinely careless thing.
7. **Measurement infrastructure without measurements.** v1 downloaded a 247 MB LongMemEval set and built a runner; riki-mem built a harness. I found no stored result from either. Until there is a number, "better memory" is a belief.
8. **"I grinded too much" is half right.** The grind in v1 produced the part worth keeping. What v1 lacked was not more features but guardrails and ops, which is what v2 delivered. The effort was not wasted; it was just not combined.

Things you got right: removing the supervisor in v1; keeping memory as its own library; fail-closed policy in v2; evals-before-prompt-changes as a rule; writing a roadmap that admits the gaps; putting time and memories in the user message to keep the prompt cacheable.

---

## 9. What to do next, in order

1. **Today: put v2 under git.** `git init`, delete `backend/.playwright-mcp/`, `backend/rfs_snapshot.yml`, `backend/.pytest_cache/`, commit, then retire `_backup_2026-10-28/`. In v1, commit the five modified files and remove `docs/` from `.gitignore`.
2. **This week: run the A/B in section 5.** One hour, three configs, 20 real messages, blind comparison. It tells you whether the gap is prompt, model, or context, before you write more code. If B (v1 persona in v2's Persona box) or C (plus Haiku 4.5) closes the gap, most of your problem is solved without new code.
3. **Port the "alive" layer into v2, in this order.** Each step is small and each fits your roadmap and your hard rule that Riki never contacts other people:
   1. A Telegram notify channel locked to your own chat ID (recipient fixed in config, not chosen by the model). Your roadmap item 5 already says this.
   2. Let small talk see the profile and a reflection block (no tools, no extra model call).
   3. Reflection generation from riki-mem memories, stored and injected into `<context>`.
   4. v1's four-phase cognition cycle replaces the plain heartbeat prompt; v1's nudge dedup, quiet hours and accuracy tracking come with it. v1's `cases_cognition_act.json` can become v2 eval cases.
   5. Session-end "lessons" from corrections, stored as preference memories (v1's `generate_self_learning` prompt works as is).
4. **Add voice to the evals.** Eight to ten cases with a rubric built from v1's register table (teasing when light, drop the bit when upset, opinion once then help, no stranger openers). Change `small-talk-hi` so using a remembered fact is allowed. Add a "nudge is specific, not generic" case.
5. **Small v2 fixes.** Make `remember` a `write`-tier action or pass its input through the same extraction filter riki-mem uses; remove `https:` from the artifact CSP `img-src` (or proxy images); state the real effort defaults in the README; fix the migration-head note; split `main.py` by router.
6. **Decide v1's status.** If v1 is your daily driver today, keep it running and freeze features. Optional cheap fixes: use Verhoeff for Aadhaar, honor `db_ssl` in the checkpointer, delete `extract_and_save` and `scripts/desktop_operator.py`, make `API_KEY` mandatory. Retire v1 only when v2 has steps 3.1 to 3.4.
7. **Do not add more apps or UI until steps 2 to 4 are done.** That is the path to "friend", not the 31st integration.

What would prove me wrong: if v2 with v1's persona, Haiku 4.5 and a reflection block is still clearly less human, the cause is something I could not see from code (most likely the Persona text currently stored in v2's database, or the way small talk is handled in the UI), and I would want to see sample conversations from both.

---

## 10. What I could not verify

- **I did not run anything.** No tests, no apps, no model calls. "Test state" is inferred from pytest cache timestamps. "v1 works well for you" is taken from you.
- **v2's Persona text** lives in the database. I could not see it, so I do not know how close v2 already is to v1's voice.
- **Which paths call what in v1.** I traced `extract_and_save` and `close_session` with a code search; I did not execute the Telegram flow. `extract_and_save` has no callers; `close_session` is called by the session manager and the Telegram bot.
- **riki-mem version.** I read GitHub HEAD (commit `887524e`, 2026-10-27). Your v2 lockfile may pin an older commit (the docs mention `56f9814`), so details could differ.
- **The Aadhaar claim** comes from how Aadhaar checksums are specified (Verhoeff, not Luhn), not from running the scanner.
- **The ponytail plugin** would not load in this session (it reported a hook mismatch), so I applied its audit approach by hand: bugs, security, load behavior, risky untested code, what to delete or merge. The findings tables above are the equivalent of its report. If you want the official `ponytail-audit` output, run it once from your own Claude Code session on each folder.
- **Credentials.** Both `.env` files contain real-looking credentials. I read variable names, model IDs and the gateway host only, and never printed secret values. v1's `.env` and `secrets/` are git-ignored (verified). When you `git init` v2, its `.gitignore` already excludes `.env`.

## 11. Evidence read

- v1: `app/agents/graph.py`, `prompts.py`; `app/chat/service.py`, `session.py`, `session_manager.py`, `telegram.py` (first part); `app/memory/` (recall, retrieval, extraction, store, rerank, profile); `app/cognition/cycle.py`, `reflection.py`; `app/features/nudge.py`; `app/core/llm.py`, `checkpoint.py`, `privacy.py`; `app/config.py`, `app/main.py`; `app/tools/__init__.py`, `calendar.py`; `docs/CHANGELOG.md`, `PROPOSED.md`, `SECURITY.md`; `README.md`; git log and status.
- v2: `backend/app/turn.py`, `memory.py`, `checks.py`, `config.py`, `runtime.py`, `ops.py`, `tracing.py`, `agent/graph.py`, `policy.py`, `tools.py`, `mcp.py`, `prompts.py`; `main.py` (endpoint list and artifact serving); `mcp.json`, `pyproject.toml`, `tests/conftest.py`, `evals/cases.json`; `docs/ARCHITECTURE.md`, `RULES.md`, `SECURITY.md`, `EVALS.md`, `ROADMAP.md`, `RIKI_MEM_IMPROVEMENTS.md`, `DATABASE_MIGRATION.md`; `README.md`, `CLAUDE.md`.
- riki-mem: README and file inventory (shallow clone of GitHub HEAD).
