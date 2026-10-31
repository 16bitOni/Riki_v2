"""Alembic for Riki's own tables. riki-mem's tables live in the same database but are
migrated by riki-mem itself, so they are invisible here (never altered, never dropped)."""

import asyncio

from alembic import context
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import BACKEND_DIR, Settings
from app.db import Base

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    # A table in the database that our models don't define belongs to someone else (riki-mem).
    return not (type_ == "table" and reflected and compare_to is None)


def _url() -> str:
    """RIKI_DATABASE_URL from the environment or backend/.env (no password needed to migrate)."""

    class DbOnly(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="RIKI_", env_file=BACKEND_DIR / ".env", extra="ignore")
        database_url: str = Settings.model_fields["database_url"].default

    return DbOnly().database_url


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, include_object=include_object,
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def main() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline mode is not supported; run against a database")
asyncio.run(main())
