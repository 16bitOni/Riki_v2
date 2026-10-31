# Working on Riki

Read `docs/RULES.md` first; it is the contract. Short version:

- Smallest complete change. Reuse LangChain/LangGraph/riki-mem and existing helpers before writing new code.
  Known limits get `# shortcut: <limit>, <when to upgrade>`.
- Safety lives in code, not prompts: new tools need an honest risk tier (`safe` / `write` / `external`).
- Every unit of model work goes through `tracing.span()`.
- Prompt edits change `PROMPT_VERSION`: run `uv run python -m evals.run --repeat 3` before and after.
- Model changes in `app/db.py` need `uv run alembic revision --autogenerate -m "..."`.
- Tests: `cd backend && uv run pytest` (needs `docker compose up -d`). Frontend: `cd frontend && npm run build` (type-checks).
- New agent behaviour -> a test in `backend/tests`; new user-facing behaviour or a bug seen in real use -> a case in `backend/evals/cases.json`.
