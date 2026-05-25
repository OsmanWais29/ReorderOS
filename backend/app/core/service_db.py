"""Async SQLAlchemy engine for the service_worker database role.

service_worker has cross-tenant SELECT/INSERT/UPDATE on pos_event_inbox
and tenant_pos_connections (USING(true) RLS), but must SET LOCAL
app.tenant_id before writing to orders or sale_line_items.

This engine is used by:
  - The webhook handler (INSERT into pos_event_inbox without tenant ctx)
  - The inbox worker process (claim loop, Clover API fetch, order insert)
  - The reconciliation service
  - The token refresh job

It is intentionally separate from the app_user engine in database.py
so that the two pools never share connections or security contexts.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_service_engine: AsyncEngine | None = None
_service_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_service_engine() -> AsyncEngine:
    global _service_engine
    if _service_engine is None:
        settings = get_settings()
        url = settings.service_database_url
        if not url:
            raise RuntimeError("SERVICE_DATABASE_URL is not set")

        # Normalize protocol same as main engine
        if url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://") :]
        elif url.startswith("postgres://"):
            url = "postgresql+asyncpg://" + url[len("postgres://") :]

        if "sslmode" in url:
            from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

            parsed = urlparse(url)
            params = {k: v for k, v in parse_qs(parsed.query).items() if k != "sslmode"}
            url = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))

        connect_args: dict[str, Any] = {}
        if settings.is_production:
            import ssl as _ssl

            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            connect_args = {"ssl": ctx}

        # Worker uses a small pool: only 1 worker process runs at a time (Sprint 4).
        _service_engine = create_async_engine(
            url,
            connect_args=connect_args,
            pool_pre_ping=True,
            pool_size=3,
            max_overflow=2,
            pool_recycle=1800,
            future=True,
        )
    return _service_engine


def get_service_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _service_sessionmaker
    if _service_sessionmaker is None:
        _service_sessionmaker = async_sessionmaker(
            bind=get_service_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _service_sessionmaker


def dispose_service_engine_sync() -> None:
    global _service_engine, _service_sessionmaker
    if _service_engine is not None:
        _service_engine.sync_engine.dispose(close=False)
        _service_engine = None
        _service_sessionmaker = None


async def ping_service_database() -> dict[str, Any]:
    engine = get_service_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        value = result.scalar_one()
    return {"service_db": "ok", "select_1": value}


class _ServiceEngineProxy:
    """Lazy proxy for the service_worker engine.

    Mirrors the _EngineProxy pattern in database.py so tests can write
    `service_worker_engine.connect()` without caring about lazy init.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(get_service_engine(), name)


service_worker_engine = _ServiceEngineProxy()
