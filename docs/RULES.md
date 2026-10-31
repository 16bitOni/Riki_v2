# Rules

Two rule sets: how Riki behaves (enforced in code first, prompt second) and how Riki's code is written.

## Riki's behaviour

| Rule | Enforced by | Verified by |
|---|---|---|
| Never contact or act toward other people | `external` risk tier: never offered to a model, refused if called | test `test_evals_never_approve_and_external_tools_never_run`, eval `never-contact-others` |
| Ask before changing anything | `write` tier waits for approval; denial is final for that run | test `test_write_tool_waits_for_approval_and_respects_denial` |
| Tool output is data, not instructions | system prompt; write tools gated anyway | eval `injection-in-memory` |
| Back factual claims with evidence | prompt asks for `[tN]` / `[m:id]` and to look up anything specific; every run gets a citation check, and every non-small-talk reply a grounding check against its evidence (or the chat's last evidence) that appends "Not verified: ..." for claims the evidence lacks | `checks.citation_check`, `turn.check_grounding`, tests `test_claims_missing_*`, `test_a_follow_up_*`, evals `cites`, `count-from-source`, `no-relabelled-batch` |
| Say "I don't know" rather than guess | prompt | eval `honest-unknown` |
| Never store secrets | prompt; riki-mem extraction skips secrets; traces redact them; keys pasted in chat are removed before anything sees them; app keys live encrypted and are typed only into the connect card | eval `no-secrets-in-memory`, `key-pasted-in-chat`, tests `test_redact_*`, `tests/test_agentic.py` |
| Answer simple things fast | small talk skips memory and tools; memory has a time limit; prompt says answer from knowledge first | evals `small-talk-hi`, `knows-it-already`, `offers-tavily`; tests `test_agentic.py` |
| Stay within budgets | depth, agent count, steps, tokens, timeouts; each message's effort (low/medium/high) caps them further; older tool results are cleared from the model's context past a size budget | tests `test_depth_limit_*`, `test_token_budget_*`, `test_low_effort_*`, `test_old_tool_results_*` |
| Be consistent | `temperature=0`, versioned prompts | `evals.run --repeat k` (pass^k) |

The prompt is the weakest layer, so anything that matters is enforced in code and has a test or an eval.

## Code rules

1. **Smallest complete change.** Reuse what exists (LangChain, LangGraph, riki-mem, the registry) before
   writing new code. No speculative options or layers. A known limit gets a `# shortcut: <limit>, <when to
   upgrade>` comment.
2. **Validate at trust boundaries.** Every HTTP input is a Pydantic model with limits; ids are 32-hex;
   URLs pass `assert_public_url`; MCP config is checked at load.
3. **Fail closed.** Unknown tool -> `write`. Approval timeout, stop or restart -> not approved. Unknown core tool ->
   startup error. Schema mismatch -> startup error.
4. **One failing part never takes Riki down.** A broken MCP server is skipped; a memory outage means
   answering without memory; a tool error becomes a tool message the model can react to.
5. **Everything the model does is a span.** New kinds of work go through `tracing.span()`.
6. **Prompts are versioned.** Editing `prompts.py` changes `PROMPT_VERSION`; run the evals before and
   after, and compare on the Health tab.
7. **Schema changes come with an Alembic migration**; the migration test enforces it.
8. **Tests run against real Postgres** with the scripted model (`tests/conftest.py`). New agent behaviour
   gets a test; new user-facing behaviour gets an eval case.
9. **Readable over clever.** Short functions, plain names, comments only for the why.
