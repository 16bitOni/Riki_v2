"""App tables (Alembic-managed). riki-mem owns its own tables in the same database."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Thread(Base):
    __tablename__ = "threads"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200), default="New chat")
    kind: Mapped[str] = mapped_column(String(16), default="side", server_default="side")  # main|side|heartbeat
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Base):
    """Only user text and final answers. Tool chatter lives in spans, so it never re-enters context."""

    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    images: Mapped[list[str] | None] = mapped_column(JSONB)  # data: URLs the user attached (user messages only)
    run_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Artifact(Base):
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


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    thread_id: Mapped[str | None] = mapped_column(ForeignKey("threads.id", ondelete="SET NULL"), index=True)
    source: Mapped[str] = mapped_column(String(16), default="chat")  # chat | eval
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | ok | error
    input: Mapped[str] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(32))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    tokens_cached: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    checks: Mapped[dict] = mapped_column(JSONB, default=dict)  # e.g. invalid citations
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Span(Base):
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


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    span_id: Mapped[str] = mapped_column(String(32))
    tool: Mapped[str] = mapped_column(String(200))
    args: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|denied|expired
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Feedback(Base):
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Persona(Base):
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
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Connection(Base):
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


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.session: async_sessionmaker[AsyncSession] = async_sessionmaker(self.engine, expire_on_commit=False)

    async def close(self) -> None:
        await self.engine.dispose()
