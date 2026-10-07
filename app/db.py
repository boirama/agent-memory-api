"""Async SQLAlchemy engine and session factory.

The non-obvious part is pgbouncer handling. Supabase's transaction pooler
(port 6543) multiplexes connections, which breaks asyncpg's prepared-statement
cache. Detected, we turn the cache off and use NullPool — otherwise you get
intermittent "prepared statement does not exist" errors that only appear
under real traffic, which is the worst time to find them.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from .config import settings

logger = logging.getLogger(__name__)

_connect_args: dict = {
    "timeout": settings.db_command_timeout,
    "server_settings": {"application_name": "agent-memory-api"},
}

_engine_kwargs: dict = {"echo": False, "pool_pre_ping": True, "future": True}

if settings.uses_pgbouncer:
    logger.info("Supabase pooler detected - disabling prepared statements")
    _connect_args["statement_cache_size"] = 0
    _connect_args["prepared_statement_cache_size"] = 0
    _engine_kwargs["poolclass"] = NullPool
else:
    _engine_kwargs.update(
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=1800,
    )

engine: AsyncEngine = create_async_engine(
    settings.database_url, connect_args=_connect_args, **_engine_kwargs
)

SessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def ping() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("database ping failed: %s", exc)
        return False


async def verify_schema() -> list[str]:
    """Returns missing tables. Empty list means migrations ran."""
    expected = {
        "tenants", "memories", "agents", "mesh_grants",
        "mesh_shares", "usage_events", "api_keys",
    }
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            )
            present = {row[0] for row in result}
        return sorted(expected - present)
    except Exception as exc:  # noqa: BLE001
        logger.warning("schema verification failed: %s", exc)
        return sorted(expected)


async def dispose() -> None:
    await engine.dispose()
