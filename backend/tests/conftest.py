"""Tests run against a real Postgres (a fresh `riki_test` database per session) with a scripted
model, so the whole graph, tracing, policy and API run for real with no provider or network."""

import asyncio
import os
from types import SimpleNamespace
from typing import Callable

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agent.prompts import VERIFY
from app.config import BACKEND_DIR, Settings
from app.runtime import Runtime

ADMIN_URL = os.environ.get("RIKI_TEST_ADMIN_URL", "postgresql://riki:riki@localhost:5433/riki")
TEST_DB = "riki_test"
TEST_URL = ADMIN_URL.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://") + f"/{TEST_DB}"


class ScriptedModel(BaseChatModel):
    """A chat model whose reply is computed by ``respond(messages)``; good enough for bind_tools/astream."""

    respond: Callable
    verify: Callable = lambda messages: AIMessage("NONE")  # the grounding check after each answer

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        msg: AIMessage = (self.verify if messages[0].content == VERIFY else self.respond)(messages)
        msg.usage_metadata = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
        return ChatResult(generations=[ChatGeneration(message=msg)])


class FakeMemory:
    def __init__(self) -> None:
        self.remembered: list[tuple] = []
        self.items: list[SimpleNamespace] = []

    async def context(self, user, message, recent_chat="", name=""):
        return "## Memories that may be relevant to this message\n- [fact, 2026-01-01] Likes chai  " \
               "(id: abcdef0123456789abcdef0123456789)", []

    blocks: dict = {}

    async def get_blocks(self, user):
        return self.blocks

    async def recall(self, user, query):
        return [], {}

    async def remember(self, user, message, reply, **kw):
        self.remembered.append((user, message, reply))

    async def save(self, data):
        from datetime import datetime, timezone

        m = SimpleNamespace(id=f"{len(self.items):032x}", content=data.content, kind=data.kind,
                            importance=data.importance, tags=[], event_at=None,
                            created_at=datetime.now(timezone.utc), source_key=data.source_key)
        self.items.append(m)
        return m

    async def list_memories(self, user, *, limit=100, offset=0, **kw):
        return list(reversed(self.items))[offset:offset + limit]

    async def edit(self, user, memory_id, *, content):
        m = next((m for m in self.items if m.id == memory_id), None)
        if m:
            m.content = content
        return m

    async def delete_user(self, user):
        return True


def tool_call(name: str, args: dict, i: int = 0) -> dict:
    return {"name": name, "args": args, "id": f"call_{name}_{i}", "type": "tool_call"}


@pytest.fixture(scope="session")
async def database():
    conn = await asyncpg.connect(ADMIN_URL)
    await conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    await conn.execute(f"CREATE DATABASE {TEST_DB}")
    await conn.close()
    os.environ["RIKI_DATABASE_URL"] = TEST_URL
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    await asyncio.to_thread(command.upgrade, cfg, "head")  # env.py runs its own event loop
    return TEST_URL


@pytest.fixture
async def make_runtime(database):
    """Factory: await make_runtime(respond, **settings) -> started Runtime, stopped after the test."""
    started: list[Runtime] = []

    async def factory(respond: Callable, extra_tools=(), verify: Callable | None = None, **overrides) -> Runtime:
        # a developer's own .env (email sign-in, SMTP) must not leak into tests: they opt in with overrides
        settings = Settings(**{"password": "correct horse battery", "database_url": database, "owner_email": None,
                               "smtp_host": None, "smtp_user": None, "smtp_password": None, "smtp_from": None,
                               "mcp_config": BACKEND_DIR / "does-not-exist.json", **overrides})
        model = ScriptedModel(respond=respond, **({"verify": verify} if verify else {}))
        rt = Runtime(settings, models={"fast": model, "smart": model}, memory=FakeMemory(), extra_tools=extra_tools)
        await rt.start()
        started.append(rt)
        return rt

    yield factory
    for rt in started:
        await rt.stop()
