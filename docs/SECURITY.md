# Security

Riki has your memories, your files and a browser. The model is treated as untrusted: it can be misled
by anything it reads, so it is never the thing that decides what is allowed.

## Threats and controls

| Threat | Control | Where |
|---|---|---|
| Someone else uses your Riki | Password login (min 12 chars), httpOnly + SameSite=Strict cookie, 1 s delay on wrong password, binds to 127.0.0.1 | `main.py`, `config.py` |
| Someone else signs in (email mode) | A 6-digit code mailed only to an address that has an account (the owner's `RIKI_OWNER_EMAIL`, or a person the owner added): single use, 10 minutes, dies after 5 wrong tries, at most 1 per 30 s and 5 per hour per person; the same answer for any address; the code is stored as an HMAC. A remembered browser is a random token (only its hash is stored, tied to one person), revoked on sign-out, by "Sign out everywhere", when the person is paused or removed, or after 90 days unused. Password login is switched off while email mode is on | `main.login_code`, `login_verify`, `_signed_in`, `mailer.py`, tests `test_email_login.py` |
| One person reads or changes another's chats, runs, files or approvals | Every table holding a person's data has a `user_id` the database fills in, and Postgres row-level security shows a session only its own person's rows. Sessions run as the restricted role `riki_app`; a query that forgets a filter still sees nothing of anyone else's, and a forged `user_id` is refused. Only sign-in and user management use the unrestricted ("system") view | alembic `0014`, `db.Database.session`, `tenancy.py`, tests `test_members.py` |
| A member reaches the owner's apps, files, settings or settings-level data | Default-deny: an endpoint is owner-only (`Auth`) unless it is deliberately opened (`AnyUser`); the owner's connected apps, MCP servers (files), the code sandbox and scheduled tasks are never offered to a member's run and are refused if the model names them anyway; a member's Riki is neutral (no owner persona, no owner apps in its prompt) | `main.require_owner`, `tools.blocked_for_member`, `graph.offerable`, `runtime.system_for` |
| A member's memory mixes with the owner's | Each person's memory is its own riki-mem namespace (their id); a removed person's namespace is deleted with them | `turn.start_run`, `main._uid`, `main.remove_user` |
| One member runs up the model bill | A daily cap per member (`RIKI_MEMBER_DAILY_BUDGET_USD`, default $1); it only counts runs with a price, so set `RIKI_PRICES` | `spend.budget_state` |
| Prompt injection (web page, file, email, stored memory tells the model to act) | Risk tiers in code: writes need your approval, external actions never run; prompt marks tool output as data | `agent/policy.py`, `prompts.py` |
| Riki messages people on your behalf | `external` tier is never offered and always refused | `policy.py`, `mcp.json` `external_tools` |
| Data exfiltration through a tool | Uploads are `external`; writes need approval; `fetch_url` only reaches public addresses | `mcp.json`, `tools.assert_public_url` |
| Data sent out through a "read" (an injected page or email asks Riki to open `evil.site/?d=<your mail>`) | `fetch_url` and each server's `url_tools` (the browser's navigate) run on their own only for an address shown word for word in your message, a search result or a page read this run; any other address needs your approval | `tools.unseen_url`, `ToolEntry.ask_if`, `mcp.json` `url_tools` |
| SSRF to localhost, LAN or cloud metadata | DNS-resolved address must be globally routable; every redirect hop re-checked | `tools.py` |
| Secrets in traces | Keys, tokens, bearer headers, private keys and `password=`-style pairs redacted before storing or streaming; values capped at 20k chars | `tracing.redact` |
| Secrets in memory | riki-mem extraction excludes passwords and card numbers; prompt forbids storing secrets | eval `no-secrets-in-memory` |
| Runaway cost / loops | Depth, agent, step, token and time budgets per run | `config.py`, `graph.py` |
| A check-in acting while you're away | Heartbeat runs have `source="heartbeat"`; only `chat` runs can ever be approved | `policy.authorize` |
| Persona text weakening the rules | The persona sits under the safety rules and is labelled voice and style only; code-level tiers don't read it | `prompts.main_prompt` |
| A malicious avatar file | Type comes from magic bytes (PNG, JPEG, GIF, WebP only; SVG refused), 5 MB cap while streaming, served with `nosniff` and behind login | `main.put_avatar` |
| Check-ins at night, or while you can't respond | Quiet hours (`RIKI_QUIET_HOURS`, default 22-8 local); check-ins never get approvals | `ops.in_quiet_hours`, `policy.authorize` |
| App tokens stolen from a database copy | Tokens, keys and OAuth client secrets are Fernet-encrypted with a scrypt-stretched key from `RIKI_PASSWORD` (older plain-hash secrets are re-encrypted at startup); never in exports, traces or API responses | `apps.secret_box`, `main.export_all` |
| A key pasted into the chat | Known key shapes (Tavily, OpenAI/Anthropic, GitHub, Stripe, Hugging Face, Google, AWS, Slack, bearer, private keys) are replaced with `[secret removed]` in the request itself, so the model, memory, the saved chat and the title never see them; the final reply is scrubbed the same way. Riki is told never to ask for a key in chat: `connect_app` shows a card whose key field posts straight to the server | `main.ChatIn`, `turn.run_turn`, `tracing.scrub`, `ConnectCard` |
| A key echoed back by an error | Validation errors (422) drop the rejected value; app views never include secrets; an address you add by hand is shown as its host only (a key in its path or query stays out of the UI) | `main.invalid_request`, `main._app_view` |
| Tavily key leaking through search | Sent only as a bearer header to `api.tavily.com`; errors carry the HTTP status only; the model never sees it | `search.py` |
| A connected app used to message people | Tool names whose verb reaches people are `external` for every app; hubs (Zapier, Rube) have their action slugs checked per call | `apps.reaches_people`, `apps.hub_args_reach_people` |
| An app you added calls a destructive tool "read-only" | Its read-only hints only count when the name also starts with a read verb; listed apps' hints are trusted | `apps.tool_risk` |
| A forged sign-in callback | `/api/apps/callback` only resolves a sign-in Riki started, matched by its one-time OAuth `state`; the SDK also checks `state` and PKCE | `apps.finish_sign_in` |
| Lock-in | One-click JSON export of chats, memories, approvals and persona | `GET /api/export` |
| Stale approval used later | Approvals live in memory; restart marks them expired | `policy.expire_stale` |
| Malformed input reaching the database | Pydantic models with length limits; ids validated as 32-hex; SQLAlchemy parameters only | `main.py` |
| MCP secrets in config | `${VAR}` from environment / `.env`; `.env` is git-ignored | `mcp.py`, `.gitignore` |

## Added with persistent approvals, the phone and files

| Threat | Control | Where |
|---|---|---|
| A page, file or app tells Riki to "remember" something (memory poisoning) | A run that has read content nobody here wrote (web, uploaded file, any app or MCP server) is *tainted*: `remember` then needs your approval, and the background learning from that run uses what you said, not the reply built on that content | `RunContext.tainted`, `ToolEntry.ask_if`, `turn.run_turn` |
| A tool that spends or moves money | `policy.moves_money` (name-based) forces `write`, shows a "moves money" note, and never allows an "Allow for 30 minutes" grant | `agent/policy.py`, `apps.py`, `mcp.py` |
| A grant used beyond its purpose | A grant is one tool in one chat for 30 minutes, never for external tools (they never run) or money | `policy.decide`, `approval_grants` |
| An old approval used again | An approval is matched by chat, tool and a hash of the arguments, only within the run it belongs to and the run it continues after a restart; it expires after 24 h; an action already done is never repeated | `policy._obtain` |
| Someone else uses the phone channel | Exactly one Telegram chat id is answered or messaged (config, not model-chosen); other chats are ignored and logged | `channels/telegram.py` |
| A malicious file | Uploads become plain text (the original bytes are not kept or opened by anything); text is treated as data and taints the run | `files.py` |
| Code run for you | `run_python` is off by default; in Docker with no network, a read-only disk, dropped capabilities, 512 MB, one CPU, 60 s | `sandbox.py` |

## Known limits

- The URL guard checks addresses, not search queries: a `web_search` query could still carry a fragment of private
  text to the search provider (not to an attacker). It also cannot stop an address that is already complete inside
  a page or email (no new data in it, but the sender learns it was opened, like a tracking pixel).

- `fetch_url` checks DNS then connects; a DNS rebinding attack could swap the address in between.
  Fine for a localhost personal agent; pin the resolved IP if Riki is ever exposed publicly.
- Redaction is pattern-based. It catches common key formats, not every secret; a key in an unusual shape pasted
  into chat would still get through.
- An MCP address you add by hand is stored as typed. If its path or query holds a key, that key sits in the
  database in plain text (the UI hides it). Prefer apps that take the key as a header.
- The password-derived key uses a fixed salt: stealing the database alone gets nothing, but a copy of the database
  plus a guessable password is brute-forceable. Keep `RIKI_PASSWORD` long.
- Single user, single process. If you expose Riki beyond localhost: put it behind HTTPS, set
  `RIKI_COOKIE_SECURE=true`, and keep the password strong.
- The Playwright MCP server runs `--isolated` (no saved browser profile). Logging into sites through it
  means typing credentials into a tool call; prefer not to.
- Apps are third-party services: what you connect can see what its tools return to Riki, and Riki's model
  provider sees those results while answering. "Read only" limits what Riki can change, not what the app's
  own servers do. Hubs (Zapier, Rube) hold your app sign-ins on their side.
- Calendar events with attendees send invitations. Creating an event is `write` (asks first); check the
  attendees on the approval card.
- MCP servers are third-party code running on your machine with your permissions. Only enable ones you
  trust, and keep `safe_tools` to genuine reads.

## Checklist when adding a tool

1. Decide its tier honestly: does it read, change something, or reach another person?
2. Put reads in `safe_tools`, anything that reaches people in `external_tools`; leave the rest as write.
3. Add an eval case if the tool creates a new way to go wrong.

## Several people on one Riki: what is and is not covered

Built: invited people (email codes), private chats, runs, files, approvals and memory, a spending cap, owner-only settings,
apps, files, sandbox, Telegram, check-ins and schedules.

Limits to know:
- Isolation of the app's own tables is enforced by the database. riki-mem's tables are not row-level secured: its API is
  namespaced per person, but anything with raw database access can read all memory.
- `riki_app` is created by migration 0014 and needs `CREATEROLE` (or a superuser) on the connecting database user.
- The owner's persona, models, quiet hours, time zone and the one Telegram phone belong to the owner; members share the server
  time zone and a neutral Riki. Per-person versions of these are not built.
- Members cannot connect their own apps yet: connected apps are one set of tools per server, so they stay the owner's.
- Open sign-up is not built. It needs a per-IP rate limit and abuse protection first (see the shortcut note in `main.py`).
- Costs are shared: one model key. Without `RIKI_PRICES` nothing is priced, so no cap can trip.
