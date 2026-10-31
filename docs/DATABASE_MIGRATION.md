# Moving Riki's database to Neon or Supabase (proposal, not built)

Status: **idea only. Nothing here is implemented.** The database holds chats, memories, connected-app tokens and
every trace, so this wants a deliberate design and a review before any code.

## The idea

Today a user runs Postgres in Docker on their own machine. Offer "Move to the cloud": the user connects a Neon or
Supabase account, Riki creates the database there, copies the data over, applies the schema, and starts using it.
The user never touches `pg_dump`, `alembic` or `.env`.

It is a good idea. The hard part of self-hosting Riki is the database, and this removes it. The way to get there
is not the one that looks obvious, so the rest of this note covers what to do and what to avoid.

## What exists today (checked in the code)

- **Export** (`GET /api/export`, Settings, Privacy & data) is **one JSON file**, not JSONL. It has chats, memories,
  approvals, the persona and the list of apps without their tokens.
- **There is no import.** Nothing reads that file back.
- The export leaves out runs, spans (the traces), feedback, eval runs, app tokens and message images. It is a
  "take your data with you" file for a person. **It is not a backup and it cannot be a migration format.**
- Supabase is in the app catalog (`apps.json`: OAuth, MCP at `mcp.supabase.com`, read-only by default). Neon is not in
  the catalog; it has its own MCP server and could be added as a custom app.
- Two things migrate the schema: Riki's Alembic (`backend/alembic`, head `0005`) and riki-mem, which creates its own
  tables on `init()` and its own extensions (`vector`, `pg_trgm`). The app refuses to start if Alembic is not at head.
- `RIKI_DATABASE_URL` is read from the environment or `backend/.env`.

## Recommendation

**Move the database, not the export.** Use Postgres's own dump and restore for the data, and let the existing
migrations own the schema.

1. **Connect** (Settings, "Move to the cloud"): the user picks Neon or Supabase and signs in or pastes a connection
   string. Pasting is the safe v1; creating the project through the provider's API or MCP is a later step.
2. **Preflight, changing nothing:** can we connect with SSL; is the server version at least as new as the source; can we
   create `vector` and `pg_trgm`; is there enough storage on the plan for `pg_database_size` of the source.
3. **Freeze the app:** refuse new chats with a clear message (the chat endpoint already returns 409 when busy). Wait
   for active runs to finish. Pending approvals expire, as they do on restart.
4. **Copy:** `pg_dump` from the source, `pg_restore` into the empty target. This carries every table including
   riki-mem's, embeddings and traces. Then run `alembic upgrade head` against the target. On a dump taken at head
   this changes nothing; on an older dump it brings it up to date.
5. **Verify before switching:** per-table row counts match, a memory search returns results, Alembic reports head.
6. **Switch:** write the new URL to the config the app reads on start, restart. Keep the old database untouched.
7. **Rollback:** one button restores the old URL. Offer to delete the old data only after the user has used the new
   database for a while.

Alembic alone is not enough, because it creates empty tables and does not copy data. A JSON import is not enough
either, because it would drop the traces and the embeddings and would have to re-embed every memory.

## Things that will bite

| Risk | Why it matters | Mitigation |
|---|---|---|
| App tokens are encrypted with a key derived from the password | A migrated database opens only with the same password. A new password makes the connected apps unreadable | Keep the password, or re-encrypt during the move, or tell the user to reconnect apps |
| Supabase's pooled connection (transaction mode) breaks asyncpg prepared statements | Confusing errors after the move | Use the direct or session connection string, or set asyncpg `statement_cache_size=0` |
| Neon scales to zero | The first request after idle is slow, and a connection can drop | Pool pre-ping and a retry on first connect; say so in the UI |
| SSL is required on both | asyncpg needs `ssl=require` in the URL or the connect arguments | The preflight tests it |
| `pg_dump` is a binary and its version must be at least the server's | Not every machine has it | Run it in the Docker image Riki already uses, or in a one-shot container |
| Free-plan limits (storage, compute, connections) | Traces and embeddings grow | Show the size and the plan limit before copying |
| The database URL is a credential | It can read everything | Store it like app tokens (encrypted), scrub it from logs, never show it to the model |
| Both providers support pgvector | True when this was written | The preflight checks it instead of assuming |

## Safety rules for this feature

- It is a `write`-tier action with a typed confirmation, never something the agent does on its own. It must not be
  reachable from a chat message, because a prompt-injected page could ask for it.
- No automatic deletion of the source database, ever.
- Everything before the switch is reversible; the switch itself is the only step that changes what the app uses.
- The model never sees the connection string.
- A test covers the preflight failures and the rollback. An eval is not needed because it is not model behaviour.

## Two ways to build it

- **Small:** a backend endpoint plus one Settings screen that takes a connection string and runs steps 2 to 7 in a
  background task with a progress line. No provider API.
- **Larger:** provider sign-in that creates the project for the user (Neon and Supabase both have management APIs;
  check their current terms and scopes first). Sign-in already exists for Supabase through the apps framework. Do this
  only after the small version has been used on a real database.

## Separate question: "without setting up the backend"

Moving the database removes one burden. The user still runs the backend and the model keys. Removing that means a
hosted Riki, which is a product and cost decision (who pays for model calls, who holds the data), not a database one.
Treat it separately.

## Do first, whichever way this goes

1. **Import.** A button that restores a Riki export, so "Export everything" has a way back. It touches the database, so
   it follows the same care.
2. **Say what the export is.** The Settings text says "Open format, readable by anything". Add that it is not a backup
   and leaves out traces, feedback and app tokens.
3. **A real backup:** "Download a full backup" using `pg_dump`. This is also step 4 above, built first, so it gets
   tested before it is used for migration.
