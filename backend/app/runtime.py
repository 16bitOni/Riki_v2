"""Everything a running Riki needs, built once: database, models, memory, tools, agent."""

import asyncio
import logging
import time
from contextlib import AsyncExitStack

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from cryptography.fernet import InvalidToken
from langchain.chat_models import init_chat_model
from langchain.embeddings import init_embeddings
from riki_memory import MemoryConfig, RikiMemory
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .agent.graph import SUB_AGENT_BASE_TOOLS, Agent
from .apps import CATALOG, Apps
from .agent.mcp import load_mcp
from .agent import policy
from .agent.policy import expire_stale
from .agent.prompts import PROMPT_VERSION, TEMPLATES, main_prompt, persona_version, sub_prompt
from .agent.tools import ToolRegistry, builtin_tools
from .config import BACKEND_DIR, Settings
from .db import Database, Persona, PromptVersion, utcnow
from .memory import LangChainProvider
from .files import file_tools
from .ops import heartbeat_loop, interrupt_stale_runs, resume_waiting_runs
from .sandbox import sandbox_tools
from .schedules import schedule_tools
from .settings_store import get_value, load_overrides, put_value, snapshot_defaults

log = logging.getLogger(__name__)


def _endpoint(base_url: str | None, api_key) -> dict:
    return {**({"base_url": base_url} if base_url else {}),
            **({"api_key": api_key.get_secret_value()} if api_key else {})}


def _model(spec: str, settings: Settings):
    kwargs = _endpoint(settings.base_url, settings.api_key)
    if settings.temperature is not None:
        kwargs["temperature"] = settings.temperature
    if spec.startswith("bedrock"):
        kwargs["region_name"] = settings.aws_region
    if spec.startswith("openai"):
        kwargs["stream_usage"] = True  # off by default for custom base URLs; the token budget needs it
        kwargs["max_retries"] = 4  # proxies answer 502 now and then; the default of 2 gives up too soon
        # a stalled stream is cut and retried (graph.ProviderHiccup) instead of eating 120s, the library default.
        # shortcut: a model that thinks silently for over 45s without streaming would be cut too; raise it then
        kwargs["stream_chunk_timeout"] = 45
    return init_chat_model(spec, **kwargs)


def _embeddings(settings: Settings):
    spec = settings.embedding_model
    kwargs = _endpoint(settings.embedding_base_url or settings.base_url, settings.embedding_api_key or settings.api_key)
    if spec.startswith("bedrock"):
        kwargs["region_name"] = settings.aws_region
    if spec.startswith("openai") and "base_url" in kwargs:
        kwargs["check_embedding_ctx_length"] = False  # non-OpenAI servers want text, not tiktoken ids
    return init_embeddings(spec, **kwargs)


class Runtime:
    def __init__(self, settings: Settings, *, models: dict | None = None, memory=None, extra_tools=()) -> None:
        """``models``/``memory`` are injectable so tests can run without a provider; ``extra_tools``
        adds your own ToolEntry objects next to the built-in and MCP ones."""
        self.settings = settings
        self._extra_tools = list(extra_tools)
        self._models, self._memory = models, memory
        self._stack = AsyncExitStack()
        self.background: set[asyncio.Task] = set()
        self.started_at = time.time()
        self.active: dict[str, dict] = {}  # run_id -> what is running right now (for the status page)
        self.runs: dict[str, tuple] = {}  # run_id -> (RunContext, task): followable, stoppable (see turn.start_run)
        self.mcp_status: dict[str, dict] = {}
        self._heartbeat: asyncio.Task | None = None
        self.telegram = None  # the phone channel, once a bot is connected (in the app, or by RIKI_TELEGRAM_* once)
        self.telegram_problem = ""  # why a saved connection could not be opened
        self.defaults: dict = {}  # what .env gave for the settings the app can change

    async def start(self) -> None:
        s = self.settings
        policy.SHUTTING_DOWN = False
        self.db = Database(s.database_url)
        await self._check_schema()
        self.defaults = snapshot_defaults(s)
        await load_overrides(self)  # what you changed in Settings wins over .env
        self.models = self._models or {"fast": _model(s.model, s), "smart": _model(s.smart_model, s)}
        self.model_names = {"fast": s.model, "smart": s.smart_model}
        self.call_kwargs = {tier: ({"cache_control": {"ttl": "5m"}}
                                   if s.prompt_cache and self.model_names[tier].startswith("bedrock_converse")
                                   else {}) for tier in ("fast", "smart")}
        if self._memory is not None:
            self.memory = self._memory
        else:
            provider = LangChainProvider(self.models["fast"], self.models["smart"], _embeddings(s), s.embedding_model)
            # rerank off: one fewer model call per message; the main model re-reads the shortlist anyway
            self.memory = RikiMemory(s.database_url, llm=provider,
                                     config=MemoryConfig(rerank="off", planner_mode=s.memory_planner,
                                                         default_timezone=s.timezone))
            await self.memory.init()

        self.registry = ToolRegistry()
        self.agent = Agent(self)
        for entry in [*builtin_tools(self), *schedule_tools(self), *file_tools(self), *sandbox_tools(self),
                      *self.agent.agent_tools(),
                      *self._extra_tools, *await load_mcp(s.mcp_config, self._stack, self.mcp_status)]:
            self.registry.add(entry)
        unknown = [n for n in s.core_tools if self.registry.get(n) is None]
        if unknown:
            raise ValueError(f"RIKI_CORE_TOOLS names unknown tools: {unknown}")
        await self.reload_persona()
        await expire_stale(self.db)
        await interrupt_stale_runs(self.db)
        self.apps = Apps(self)
        await self.apps.start()
        self._heartbeat = asyncio.create_task(heartbeat_loop(self))
        await self.telegram_load()
        self.spawn(resume_waiting_runs(self))  # requests that were waiting for you when Riki last stopped

    def sync_sandbox(self) -> None:
        """Switch run_python on or off to match the setting, without a restart."""
        want = self.settings.sandbox
        if not want:
            self.registry.remove("run_python")
        elif self.registry.get("run_python") is None:
            for entry in sandbox_tools(self):
                self.registry.add(entry)

    # -- the phone channel: connected from Settings, the token stored encrypted ----------------------------

    async def telegram_load(self) -> None:
        """Open the saved connection. A bot set in .env is taken over once (encrypted into the database), so after that
        Settings is the one place that controls it."""
        from .channels.telegram import TelegramChannel

        s, box = self.settings, self.apps.box
        rec = await get_value(self.db, "telegram")
        if rec is None and s.telegram_token and s.telegram_chat_id:
            rec = {"token": box.encrypt(s.telegram_token.get_secret_value().encode()).decode(),
                   "chat_id": s.telegram_chat_id, "bot": "", "who": ""}
            await put_value(self.db, "telegram", rec)
        if not rec or rec.get("off"):
            return
        try:
            token = box.decrypt(rec["token"].encode()).decode()
        except InvalidToken:
            self.telegram_problem = "The saved Telegram token cannot be opened (the password changed). Connect it again."
            log.warning(self.telegram_problem)
            return
        self.telegram_problem = ""
        self.telegram = TelegramChannel(self, token, rec.get("chat_id"), rec.get("bot", ""), rec.get("who", ""))
        await self.telegram.start()

    async def _telegram_record(self, **changes) -> None:
        rec = await get_value(self.db, "telegram") or {}
        await put_value(self.db, "telegram", {**rec, **changes})

    async def telegram_connect(self, token: str) -> dict:
        """Check the token with Telegram, keep it encrypted, and start waiting for the pairing code."""
        from .channels.telegram import TelegramChannel, bot_info

        bot = (await bot_info(token)).get("username", "")
        await self.telegram_stop()
        await put_value(self.db, "telegram", {"token": self.apps.box.encrypt(token.encode()).decode(),
                                              "chat_id": None, "bot": bot, "who": ""})
        self.telegram_problem = ""
        self.telegram = TelegramChannel(self, token, None, bot)
        await self.telegram.start()
        return self.telegram.status()

    async def telegram_stop(self) -> None:
        if self.telegram is not None:
            channel, self.telegram = self.telegram, None
            await channel.stop()

    async def telegram_disconnect(self) -> None:
        await self.telegram_stop()
        await put_value(self.db, "telegram", {"off": True})  # also keeps an old .env value from coming back

    async def telegram_repair(self) -> dict:
        """Pair a different chat (a new phone): keeps the bot, forgets the chat, shows a new code."""
        if self.telegram is None:
            raise ValueError("connect a bot first")
        self.telegram.unpair()
        await self._telegram_record(chat_id=None, who="")
        return self.telegram.status()

    async def telegram_paired(self, chat_id: int, who: str) -> None:
        await self._telegram_record(chat_id=chat_id, who=who)

    def telegram_status(self) -> dict:
        if self.telegram is None:
            return {"connected": False, "paired": False, "error": self.telegram_problem}
        return self.telegram.status()

    async def reload_persona(self) -> Persona:
        """(Re)build the system prompts. Only here and on persona edits, so the prompt stays byte-stable
        between edits and prompt caching keeps working."""
        s = self.settings
        async with self.db.session() as db:
            persona = await db.get(Persona, 1)
            if persona is None:
                persona = Persona(id=1)
                db.add(persona)
                await db.commit()
            self.prompt_version = f"{PROMPT_VERSION}.{persona_version(persona.soul, persona.name)}"
            # the text behind every version stays readable; apps connecting at once may race here, hence on-conflict
            await db.execute(pg_insert(PromptVersion).values(
                id=self.prompt_version, templates=TEMPLATES, persona={"name": persona.name, "soul": persona.soul},
                created_at=utcnow()).on_conflict_do_nothing())
            await db.commit()
        self.persona = persona
        # from the registry, not app state: an app reloads the prompt just before it reports "connected"
        live = {e.source.split(":", 1)[1] for e in self.registry.entries.values() if e.source.startswith("app:")}
        self.main_system = main_prompt(s.user_name, self.registry.catalog(exclude=set(s.core_tools)),
                                       persona.name, persona.soul, ", ".join(a for a in CATALOG if a not in live),
                                       ", ".join(sorted(live)))
        self.sub_system = sub_prompt(s.user_name, self.registry.catalog(exclude={*SUB_AGENT_BASE_TOOLS, "connect_app"}),
                                     persona.name)
        return persona

    async def _check_schema(self) -> None:
        """Refuse to run against a database the code doesn't match (fail fast, not mid-chat)."""
        head = ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini"))).get_current_head()
        async with self.db.engine.connect() as conn:
            current = await conn.run_sync(lambda c: MigrationContext.configure(c).get_current_revision())
        if current != head:
            raise RuntimeError(f"Database schema is at {current}, the code expects {head}. "
                               "Run: uv run alembic upgrade head")

    async def stop(self) -> None:
        policy.SHUTTING_DOWN = True  # runs waiting for you stay waiting, so the next start can continue them
        await self.telegram_stop()
        if self._heartbeat:
            self._heartbeat.cancel()
            await asyncio.gather(self._heartbeat, return_exceptions=True)
        for ctx, task in list(self.runs.values()):  # waiting for you: leave them waiting, now, not in 15 s
            if ctx.parked and not task.done():
                task.cancel()
        if self.background:  # let in-flight runs and memory writes finish their bookkeeping
            _, pending = await asyncio.wait(self.background, timeout=15)
            for task in pending:  # e.g. a run still waiting on an approval
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        await self.apps.stop()
        await self._stack.aclose()
        if self._memory is None:
            await self.memory.close()
        await self.db.close()

    def active_run(self, thread_id: str):
        """The run still going in this chat, if any (a chat runs one message at a time)."""
        return next((c for c, t in self.runs.values() if c.thread_id == thread_id and not t.done()), None)

    def spawn(self, coro) -> asyncio.Task:
        """Background task that outlives the request and is awaited on shutdown."""
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task
