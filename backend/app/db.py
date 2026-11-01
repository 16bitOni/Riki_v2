"""App tables (Alembic-managed). riki-mem owns its own tables in the same database."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, event, text
from sqlalchemy import false as sa_false
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, declared_attr, mapped_column

from .tenancy import who


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    """Someone who can sign in. The owner is created at start from RIKI_OWNER_EMAIL; members are invited by the owner."""

    __tablename__ = "people"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True)  # lower case
    name: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(8), default="member", server_default="member")  # owner | member
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())
    login: Mapped[dict | None] = mapped_column(JSONB)  # the sign-in code in flight: {hash, expires, tries, sent}
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Owned:
    """Whose row this is. The database fills it in from the session's user, and Postgres row-level security only ever
    shows a session its own rows (see alembic 0014), so a query that forgets a filter still cannot see someone else's."""

    @declared_attr
    def user_id(cls) -> Mapped[str]:
        return mapped_column(String(32), ForeignKey("people.id", ondelete="CASCADE"),
                             server_default=text("current_setting('riki.user_id', true)"))


class Thread(Owned, Base):
    __tablename__ = "threads"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200), default="New chat")
    kind: Mapped[str] = mapped_column(String(16), default="side", server_default="side")  # main|side|heartbeat
    lessons_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # when lessons were last drawn from this chat
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Owned, Base):
    """Only user text and final answers. Tool chatter lives in spans, so it never re-enters context."""

    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    images: Mapped[list[str] | None] = mapped_column(JSONB)  # data: URLs the user attached (user messages only)
    run_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Artifact(Owned, Base):
    """A file Riki made to keep: a Markdown document (md) or a designed HTML page (html, saved as PDF)."""

    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    thread_id: Mapped[str | None] = mapped_column(ForeignKey("threads.id", ondelete="SET NULL"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(32))  # the run that last wrote it
    title: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16))  # md | html
    format: Mapped[str] = mapped_column(String(8), default="", server_default="")  # md | pdf
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Owned, Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    thread_id: Mapped[str | None] = mapped_column(ForeignKey("threads.id", ondelete="SET NULL"), index=True)
    source: Mapped[str] = mapped_column(String(16), default="chat")  # chat | eval | heartbeat | schedule | cognition | lessons
    effort: Mapped[str | None] = mapped_column(String(8))  # low | medium | high, kept so a resumed run gets the same limits
    resume_of: Mapped[str | None] = mapped_column(String(32))  # the run that was waiting for you when Riki restarted
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | waiting (for you) | ok | error | stopped
    input: Mapped[str] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(32))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    tokens_cached: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)  # model calls only, see tracing.run_cost; None = unpriced
    checks: Mapped[dict] = mapped_column(JSONB, default=dict)  # e.g. invalid citations
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Span(Owned, Base):
    """One node of the run tree: agent, llm call, tool call or memory lookup."""

    __tablename__ = "spans"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))  # agent | llm | tool | memory
    name: Mapped[str] = mapped_column(String(200))
    ref: Mapped[str | None] = mapped_column(String(16))  # citation handle shown to the model, e.g. t3
    status: Mapped[str] = mapped_column(String(16))  # ok | error | denied
    input: Mapped[dict | list | str | None] = mapped_column(JSONB)
    output: Mapped[dict | list | str | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    tokens_cached: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Approval(Owned, Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    span_id: Mapped[str] = mapped_column(String(32))
    tool: Mapped[str] = mapped_column(String(200))
    args: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|denied|expired
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # What makes an approval outlive a run: the same request after a restart finds this row instead of asking again.
    thread_id: Mapped[str | None] = mapped_column(String(32), index=True)
    args_hash: Mapped[str | None] = mapped_column(String(64))
    money: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())  # moves money: never granted
    via: Mapped[str] = mapped_column(String(8), default="user", server_default="user")  # user | grant
    result: Mapped[str | None] = mapped_column(Text)  # what the action returned (so it never runs twice), or ask_user's answer
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalGrant(Owned, Base):
    """"Allow this tool for 30 minutes in this chat": saves tapping through a 20-step job. Never for money or people."""

    __tablename__ = "approval_grants"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    thread_id: Mapped[str] = mapped_column(String(32), index=True)
    tool: Mapped[str] = mapped_column(String(200))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Reflection(Owned, Base):
    """Riki's living read on you. Each version is kept; the newest is injected into prompts and shown in Memory."""

    __tablename__ = "reflections"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    content: Mapped[str] = mapped_column(Text)
    monologue: Mapped[str] = mapped_column(Text, default="", server_default="")  # private thoughts of that thinking cycle
    memory_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(8), default="auto")  # auto | cycle | edit (an edit is ground truth)
    run_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Schedule(Owned, Base):
    """A reminder (just tells you) or a task (a read-only run whose result tells you), once or repeating."""

    __tablename__ = "schedules"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(8))  # remind | task
    text: Mapped[str] = mapped_column(Text)
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    repeat: Mapped[str] = mapped_column(String(10), default="none")  # none | daily | weekdays | weekly
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | done | cancelled
    last_run_id: Mapped[str | None] = mapped_column(String(32))
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppSetting(Owned, Base):
    """Settings changed in the app (they win over the environment), and the Telegram connection (token encrypted)."""

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Device(Base):
    """A browser that signed in with an emailed code and is remembered. Only the hash of its cookie is stored."""

    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the cookie token
    user_id: Mapped[str] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Nudge(Owned, Base):
    """A message Riki sent on its own initiative, with how you reacted (this is what teaches it when to stay quiet)."""

    __tablename__ = "nudges"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    topic: Mapped[str] = mapped_column(String(120))
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(12), default="open_loop")
    run_id: Mapped[str | None] = mapped_column(String(32))  # the thinking cycle behind it: "why did you message me?"
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    reaction: Mapped[str | None] = mapped_column(String(8))  # replied | ignored (set later)


class Feedback(Owned, Base):
    __tablename__ = "feedback"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    score: Mapped[int] = mapped_column(Integer)  # +1 / -1
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvalRun(Base):
    __tablename__ = "eval_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    prompt_version: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(200))
    repeat: Mapped[int] = mapped_column(Integer)
    passed: Mapped[int] = mapped_column(Integer)  # cases that passed all `repeat` tries (pass^k)
    total: Mapped[int] = mapped_column(Integer)
    results: Mapped[list] = mapped_column(JSONB)
    regressions: Mapped[list] = mapped_column(JSONB, default=list)
    judge: Mapped[str | None] = mapped_column(Text)  # the grading prompt: not part of Riki's prompt_version
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class PromptVersion(Base):
    """The prompt text behind a run's prompt_version (PROMPT_VERSION.persona_version), so an old one can be read back."""

    __tablename__ = "prompt_versions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    templates: Mapped[dict] = mapped_column(JSONB)
    persona: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Persona(Owned, Base):
    """Single row (id=1): who Riki is, written by the user. Part of the system prompt."""

    __tablename__ = "persona"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    name: Mapped[str] = mapped_column(String(40), default="Riki")
    soul: Mapped[str] = mapped_column(Text, default="")  # personality, tone, things to always/never do
    hue: Mapped[int] = mapped_column(Integer, default=265)  # avatar colour, 0-359
    heartbeat_minutes: Mapped[int] = mapped_column(Integer, default=0)  # 0 = off
    heartbeat_prompt: Mapped[str] = mapped_column(Text, default="")
    avatar: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)  # your image or GIF
    avatar_type: Mapped[str | None] = mapped_column(String(32))  # set from the file's bytes, never the client
    role: Mapped[str] = mapped_column(String(24), default="", server_default="")  # apps.json roles: what you use Riki for
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())  # setup finished or skipped
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Connection(Owned, Base):
    """An app you connected (Notion, Gmail, Zerodha...). Listed apps take name/url/auth from apps.json."""

    __tablename__ = "connections"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # also the prefix of its tool names
    name: Mapped[str] = mapped_column(String(80))
    url: Mapped[str] = mapped_column(String(500))
    auth: Mapped[str] = mapped_column(String(16))  # oauth | google | token | none
    mode: Mapped[str] = mapped_column(String(8), default="ask")  # read | ask
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    secret: Mapped[bytes | None] = mapped_column(LargeBinary)  # Fernet-encrypted JSON: tokens, client, key
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class _Scoped(Session):
    """A session that, when it has a person, runs as the restricted role with that person's id (row-level security)."""


@event.listens_for(_Scoped, "after_begin")
def _scope(session, _transaction, connection) -> None:
    uid = session.info.get("uid")
    if uid is not None:  # SET LOCAL: gone at commit or rollback, so a pooled connection never keeps it
        # Pin the schema first: the default search_path starts with "$user", which stops meaning the app's schema
        # the moment the role changes.
        connection.execute(text("SELECT set_config('search_path', current_schema() || ',public', true)"))
        connection.execute(text("SET LOCAL ROLE riki_app"))
        connection.execute(text("SELECT set_config('riki.user_id', :u, true)"), {"u": uid})


class Database:
    def __init__(self, url: str, owner_id: str = "me") -> None:
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.owner_id = owner_id
        self._factory: async_sessionmaker[AsyncSession] = async_sessionmaker(self.engine, expire_on_commit=False,
                                                                           sync_session_class=_Scoped)

    def session(self, *, system: bool = False) -> AsyncSession:
        """A session for the person making this request. With nobody signed in (a background job, Telegram, the heartbeat)
        it is the owner's. ``system=True`` sees every person's rows: only for sign-in, user management and start-up."""
        s = self._factory()
        person = who()
        s.sync_session.info["uid"] = None if system else (person.id if person else self.owner_id)
        return s

    async def close(self) -> None:
        await self.engine.dispose()
