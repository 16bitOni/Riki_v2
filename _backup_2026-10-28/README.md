# Riki v2

A single-user personal agent that behaves like a capable friend: it remembers you, delegates work to
sub-agents (which can delegate further), asks before it changes anything, never contacts other people,
and shows its working for every answer.

- **Graph of agents.** One LangGraph loop runs every agent. `spawn_agent` starts a sub-agent with a clean
  context and only the tools it needs; sub-agents can spawn their own, up to `RIKI_MAX_DEPTH`. Parallel
  spawns run concurrently.
- **Traceable.** Every agent, model call, tool call and memory lookup is a span in a tree you can open from
  any answer. Answers cite evidence (`[t3]` = tool output 3, `[m:1fdab01f]` = a memory). Citations that
  point at nothing are flagged on the run.
- **Memory** by [riki-mem](https://github.com/16bitOni/Riki-mem): recall before every answer, learning after
  every turn, full control in the Memory tab.
- **Safe by construction.** Tools are `safe` (run), `write` (wait for your approval) or `external` (never
  run). Secrets are redacted from traces. Login required.
- **Measured.** An eval suite with pass^k consistency and regression detection, plus daily drift signals
  from real chats.
- **Lean.** Built-in + MCP tools are listed in a one-line catalog and loaded on demand; prompts are
  byte-stable for prompt caching; sub-agent chatter never enters the main context.

## The app

Dark, quiet, built around one conversation. No sidebar:

- **Top bar**: Riki and its status on the left, a floating tab bar in the middle (Home, Chat, Apps, Memory,
  Activity), approvals and settings on the right.
- **The Island**: a black pill under the tab bar that only appears when something is happening. It shows
  what Riki is doing ("Searching the web for …"), and when Riki wants to change something it asks right
  there: Allow or Deny, with "Show exactly what it will do". It follows the server, so it is the same on every
  tab and after a refresh. Replies keep coming if you switch tabs or close the page, and Stop ends one.
- **Home**: the day at a glance. Ask anything, continue recent chats, today's check-in note, what Riki
  learned today; suggestions come from your own history.
- **Chat**: one centered column and a floating message capsule (what-can-Riki-do menu, dictation in
  Chrome and Edge). Each reply has one quiet line ("Worked for 3.5 s") that opens its steps, and the
  "how this was made" panel shows the full trace. The chat name above the conversation opens the chat
  switcher: main chat, separate topic chats with previews, rename and delete. The bin next to the name
  deletes the chat (clears it, for the main chat).
- **Apps**: connect Gmail, Calendar, Notion, Zerodha Kite, Swiggy, GitHub and ~25 more (sign in with the app,
  or paste a key), thousands more through Zapier or Rube, any app in the MCP Registry, or any MCP link. Per app:
  "Read only" or "Ask before changes", what it can and can't do, how often it was used, disconnect.
- **Memory**: everything Riki knows, where it learned it and when; correct or forget anything.
- **Activity**: a searchable, live log of what Riki worked on (including what is still running, waiting
  for you, stopped or failed), what it changed, and what it learned.
- **Settings** (gear): Persona (name, avatar image or GIF, personality), Check-ins (never during quiet
  hours, 22:00 to 8:00 by default), Privacy & data (what Riki can do in plain English, one-click export),
  Tools, Diagnostics (health, logs, eval results, daily signals).

On phones the tabs move to a bottom bar and panels rise as bottom sheets.

Read next: [Architecture](docs/ARCHITECTURE.md) · [Rules](docs/RULES.md) · [Security](docs/SECURITY.md) ·
[Evals & operations](docs/EVALS.md) · [riki-mem improvements](docs/RIKI_MEM_IMPROVEMENTS.md)

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/) (uv installs the right Python for you)
- Node 18+ (for the UI and for npx-based MCP servers)
- Docker Desktop (for Postgres + pgvector), or any Postgres 13+ with pgvector
- An `OPENAI_API_KEY` for the default models, or AWS credentials with Bedrock access if you switch providers (below)

## Setup (Windows PowerShell; the same commands work in bash)

```powershell
# 1. Database
docker compose up -d

# 2. Backend
cd backend
copy .env.example .env        # then set RIKI_PASSWORD (12+ chars) in .env
uv sync --group dev
uv run alembic upgrade head
uv run pytest                 # needs the database from step 1
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3. UI (second terminal)
cd frontend
npm install
npm run dev                   # http://localhost:5173
```

For a single process, `npm run build` once; the backend then serves the UI at http://127.0.0.1:8000.

If you use Bedrock, AWS credentials are read the standard way (`aws configure`, `AWS_PROFILE`, or `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` in the environment). Bedrock model access must be enabled in the AWS console
for the models you configure.

## Models

Models are LangChain `provider:model` strings, so any provider LangChain supports works:

| Setting | Default | Used for |
|---|---|---|
| `RIKI_MODEL` | `openai:gpt-4.1-mini` | main agent, sub-agents, memory extraction |
| `RIKI_SMART_MODEL` | `openai:gpt-4.1` | sub-agents spawned with `smart: true` |
| `RIKI_EMBEDDING_MODEL` | `openai:text-embedding-3-small` | memory search |

The fast model runs the main agent on purpose: most personal-agent turns are short, and latency is what
you feel. Hard reasoning is delegated to a smart sub-agent. The defaults need `OPENAI_API_KEY`. To use Bedrock instead, set the three variables to
`bedrock_converse:<model id>` / `bedrock:<embedding model id>` and see the AWS notes above.

Changing the embedding model is safe: riki-mem keeps vectors per model; run its `reembed_stale()` to
backfill.

## Tools

Built in: `current_time`, `web_search` (Tavily once you connect it in Apps, DuckDuckGo until then),
`fetch_url` (public pages only), `recall_memory`, `remember`, `spawn_agent`, `load_tools`, `connect_app`.

**Web search.** Open Apps, add **Tavily**, paste its key (free plan at app.tavily.com). Or just tell Riki "set up
Tavily": it puts a card in the chat with a hidden key field. The key is checked, stored encrypted, and used only for
the call to `api.tavily.com`. Never paste a key into the chat itself: it is stripped (`[secret removed]`) before
anything sees it.

**Speed.** Greetings and thanks skip memory and tools (one model call). Everything else looks up memory without an
extra planner model call (`RIKI_MEMORY_PLANNER=heuristic`; `escalate` or `llm` give the model-planned search back)
and never waits longer than `RIKI_MEMORY_TIMEOUT_S` (8) for it. Riki answers what it already knows without
searching, and does single lookups itself instead of spawning helpers.

MCP servers are configured in [`backend/mcp.json`](backend/mcp.json): filesystem (scoped to
`RIKI_FILES_DIR`) and a headless Playwright browser are enabled; GitHub is ready once `GITHUB_TOKEN` is set
and `enabled` is true. Each server lists which of its tools are `safe_tools` and `external_tools` (glob
patterns); everything else needs your approval. The Tools tab shows what is loaded and its risk.

Your own accounts are connected from the **Apps** tab rather than `mcp.json` (list in
[`backend/app/apps.py`](backend/app/apps.py) / `apps.json`; how it works in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#apps-appappspy-appappsjson)).
Notes per app:

- **Open Riki at `http://localhost:…`** when you connect apps. The sign-in returns to the address you're on,
  and some apps (Swiggy) only accept `localhost` for personal use.
- **Google** (Gmail, Calendar, Drive, Docs): Google requires your own OAuth client. In Google Cloud, create a
  Web application OAuth client, enable the API for each app, and add the redirect URI the Apps tab shows.
- **Zerodha Kite** starts read only; Kite's own login link appears in the chat when Riki first needs it.
- **Swiggy** lets anyone test on `localhost`; wider use needs their review (mcp.swiggy.com/builders).
- **Zapier** (each use costs 2 Zapier tasks) and **Rube** reach apps without their own MCP server, e.g. Spotify.

## Evals

```powershell
cd backend
uv run python -m evals.run --repeat 3
```

Cases live in [`backend/evals/cases.json`](backend/evals/cases.json). See [docs/EVALS.md](docs/EVALS.md).

## Layout

```text
backend/
  app/
    main.py          HTTP API: auth, chat (SSE), approvals, runs/traces, memory, evals, metrics
    turn.py          one message -> one run: recall, run main agent, persist trace, learn
    runtime.py       builds db, models, memory, tool registry, agent; checks schema version
    agent/graph.py   the agent loop, spawn_agent / load_tools, tool execution, budgets
    agent/policy.py  risk tiers and approvals
    agent/tools.py   built-in tools, registry, SSRF-safe fetch
    agent/mcp.py     MCP servers from mcp.json, persistent sessions
    agent/prompts.py system prompts and PROMPT_VERSION
    apps.py          apps you connect (OAuth sign-in, encrypted tokens, per-app risk); list in apps.json
    tracing.py       spans, run context, secret redaction
    checks.py        citation check run on every answer
    memory.py        riki-mem provider over LangChain models
    db.py            tables (Alembic in alembic/)
  evals/             eval runner and cases
  tests/             tests against real Postgres with a scripted model
frontend/            React UI: chat, live agent tree, approvals, memory, health, tools
docs/                architecture, rules, security, evals, riki-mem notes
```
