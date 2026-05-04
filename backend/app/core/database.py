"""Async SQLAlchemy 2 engine + session.

The engine is created lazily so tests can override settings before connecting.
Sprint 2 will add per-request session middleware that sets ``app.tenant_id``
and ``app.user_role`` for RLS.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    """Project-wide ORM base. Modules subclass this for their tables."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        # DigitalOcean managed DB uses a self-signed CA not in Python's trust
        # store. We still encrypt the connection; we just skip chain verification
        # since this is internal DO-to-DO traffic.
        if settings.is_production:
            import ssl as _ssl

            _ctx = _ssl.create_default_context()
            _ctx.check_hostname = False
            _ctx.verify_mode = _ssl.CERT_NONE
            connect_args: dict[str, Any] = {"ssl": _ctx}
        else:
            connect_args = {}
        # Per-instance budget: 5 + 5 = 10 connections. With the dev-tier
        # cluster's 47-connection cap, we can safely run 4 app instances
        # (40 conns) while leaving headroom for migrations, psql, and
        # background workers.
        _engine = create_async_engine(
            str(settings.database_url),
            connect_args=connect_args,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            pool_recycle=1800,
            future=True,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields a session, commits on success, rolls back on exception."""
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Called on shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None


def dispose_engine_sync() -> None:
    """Synchronous pool reset — used between tests to avoid cross-loop connections.

    Uses close=False so we don't attempt to close connections that may be
    tied to an already-closed event loop.
    """
    global _engine, _sessionmaker
    if _engine is not None:
        _engine.sync_engine.dispose(close=False)
        _engine = None
        _sessionmaker = None


async def ping_database() -> dict[str, Any]:
    """Cheap readiness check: SELECT 1."""
    from sqlalchemy import text

    engine = get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        value = result.scalar_one()
    return {"db": "ok", "select_1": value}
