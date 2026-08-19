"""Direct tests of the fail-closed runtime-role gates (app.core.rls_assert).

These EXECUTE the assertion functions (not just route behavior) under sessions bound
to specific DB roles via SET ROLE, covering the expected-pass and every fail-closed
path: wrong name, non-member, superuser, bypassrls, wrong service role, and a query
failure. Local roles available: reorderos (superuser), app_user, service_worker,
reorderos_app; plus a purpose-built superuser-member role.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.rls_assert import (
    assert_migration_capability_sync,
    assert_request_role_session,
    assert_service_role_session,
)
from tests.conftest import DB_URL_SYNC

pytestmark = pytest.mark.integration

_ASYNC_URL = DB_URL_SYNC.replace("postgresql://", "postgresql+asyncpg://")


def _is_local_db(url: str) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(url.replace("postgresql+asyncpg://", "postgresql://")).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", ""}


@pytest.fixture(scope="module", autouse=True)
async def _roles() -> AsyncIterator[None]:
    # SAFETY: this fixture creates a SUPERUSER test role — never run it against a
    # non-local DB (cannot alter staging/production).
    if not _is_local_db(DB_URL_SYNC):
        pytest.skip("startup-role-gate tests mutate roles; only run against a LOCAL database")
    conn = await asyncpg.connect(DB_URL_SYNC)
    await conn.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='reorderos_app') THEN "
        "  CREATE ROLE reorderos_app NOLOGIN NOSUPERUSER NOBYPASSRLS INHERIT; END IF; "
        "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='char_super_member') THEN "
        "  CREATE ROLE char_super_member NOLOGIN SUPERUSER NOBYPASSRLS INHERIT; END IF; "
        "END $$;"
    )
    await conn.execute("GRANT app_user TO reorderos_app")
    await conn.execute("GRANT app_user TO char_super_member")
    await conn.close()
    yield
    conn = await asyncpg.connect(DB_URL_SYNC)
    await conn.execute("DROP ROLE IF EXISTS char_super_member")
    await conn.close()


@asynccontextmanager
async def _as_role(role: str) -> AsyncIterator[Any]:
    engine = create_async_engine(_ASYNC_URL)
    try:
        async with engine.connect() as conn:
            await conn.begin()
            await conn.execute(text(f"SET ROLE {role}"))
            from app.core.database import make_bound_session

            yield make_bound_session(conn)
    finally:
        await engine.dispose()


# ── request pool ──────────────────────────────────────────────────────────────
async def test_request_gate_passes_for_reorderos_app() -> None:
    async with _as_role("reorderos_app") as s:
        assert await assert_request_role_session(s, expected="reorderos_app") == "reorderos_app"


async def test_request_gate_rejects_superuser() -> None:
    # local superuser stands in for doadmin (unavailable locally); rolsuper=true → fail.
    async with _as_role("reorderos") as s:
        with pytest.raises(RuntimeError):
            await assert_request_role_session(s, expected="reorderos_app")


async def test_request_gate_rejects_superuser_member_even_if_not_bypassrls() -> None:
    async with _as_role("char_super_member") as s:
        with pytest.raises(RuntimeError):
            # member of app_user, bypassrls=false, but SUPERUSER → must still fail.
            await assert_request_role_session(s, expected="char_super_member")


async def test_request_gate_rejects_non_member() -> None:
    async with _as_role("service_worker") as s:
        with pytest.raises(RuntimeError):
            await assert_request_role_session(s, expected="service_worker")


async def test_request_gate_rejects_wrong_name_even_if_member() -> None:
    # app_user IS a member of itself but is not the expected login role.
    async with _as_role("app_user") as s:
        with pytest.raises(RuntimeError):
            await assert_request_role_session(s, expected="reorderos_app")


# ── service pool ──────────────────────────────────────────────────────────────
async def test_service_gate_passes_for_service_worker() -> None:
    async with _as_role("service_worker") as s:
        assert await assert_service_role_session(s) == "service_worker"


async def test_service_gate_rejects_wrong_role() -> None:
    async with _as_role("reorderos_app") as s:
        with pytest.raises(RuntimeError):
            await assert_service_role_session(s)


async def test_service_gate_rejects_superuser() -> None:
    async with _as_role("reorderos") as s:
        with pytest.raises(RuntimeError):
            await assert_service_role_session(s)


# ── fail-closed on query failure ──────────────────────────────────────────────
async def test_request_gate_fails_closed_on_query_error() -> None:
    class _Boom:
        async def execute(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        await assert_request_role_session(_Boom())


# ── migration capability ──────────────────────────────────────────────────────
async def test_migration_capability_passes_for_admin_and_fails_for_app_user() -> None:
    engine = create_async_engine(_ASYNC_URL)
    try:
        async with engine.connect() as conn:
            # superuser has CREATE
            cu = await conn.run_sync(assert_migration_capability_sync)
            assert cu
            # app_user lacks CREATE on db/schema
            await conn.execute(text("SET ROLE app_user"))
            with pytest.raises(RuntimeError):
                await conn.run_sync(assert_migration_capability_sync)
            await conn.execute(text("RESET ROLE"))
    finally:
        await engine.dispose()


# ── SERVICE_ROLE_NAME-driven expectation (round 4: versioned-role rotation) ───
async def test_service_gate_default_follows_service_role_name_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default expected service role comes from Settings.service_role_name, so the
    versioned-role rotation (Phase B-alt) changes the assertion IN THE SAME deployment
    as the DSN: with SERVICE_ROLE_NAME=service_worker_v2, plain service_worker must
    FAIL the default assertion (and explicit expected= still works)."""
    from app.core.config import get_settings

    monkeypatch.setenv("SERVICE_ROLE_NAME", "service_worker_v2")
    get_settings.cache_clear()
    try:
        async with _as_role("service_worker") as s:
            with pytest.raises(RuntimeError, match="expected=service_worker_v2"):
                await assert_service_role_session(s)
            # explicit override remains available and passes:
            assert await assert_service_role_session(s, expected="service_worker") == (
                "service_worker"
            )
    finally:
        monkeypatch.delenv("SERVICE_ROLE_NAME", raising=False)
        get_settings.cache_clear()
