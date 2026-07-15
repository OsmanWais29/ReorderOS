"""CLOVER_ENABLED — Clover is optional; receipts must run without it.

Production rollout was paused with prod pointed at a Clover sandbox it may
never use. These tests pin both modes:

  OFF (default): production Settings boot WITHOUT Clover secrets, every
  Clover route 503s with a stable code, the inbox worker exits cleanly,
  env_check stops requiring Clover keys — and receipts still commit.

  ON: the original fail-closed behavior is fully restored.

The suite-wide default is CLOVER_ENABLED=true (tests/conftest.py) so the
existing POS suites keep exercising Clover as before; these tests flip the
switch explicitly and clear the Settings cache around themselves.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.core.database import engine, get_db_session, make_bound_session
from app.core.security import Principal, get_principal
from app.main import create_app
from app.ops.env_check import check_env

pytestmark = pytest.mark.integration


@pytest.fixture
def clover_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CLOVER_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def clover_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CLOVER_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ── Settings: fail-closed only when enabled ───────────────────────────────────


def test_production_boots_without_clover_secrets_when_disabled(
    clover_off: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CLOVER_APP_ID", "")
    monkeypatch.setenv("CLOVER_APP_SECRET", "")
    monkeypatch.setenv("CLOVER_WEBHOOK_AUTH_CODE", "")
    get_settings.cache_clear()
    s = get_settings()  # would raise fail-closed before this change
    assert s.clover_enabled is False


def test_production_fail_closed_restored_when_enabled(
    clover_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CLOVER_APP_SECRET", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="CLOVER_APP_SECRET"):
        get_settings()


# ── Routes: 503 with stable code when disabled ────────────────────────────────


@pytest.fixture
async def client_off(clover_off: None) -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_clover_routes_503_when_disabled(client_off: AsyncClient) -> None:
    r = await client_off.get("/api/v1/pos/clover/status")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "CLOVER_DISABLED"

    r = await client_off.post("/api/v1/webhooks/pos/clover", json={})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "CLOVER_DISABLED"


# ── Receipts still work with Clover off ───────────────────────────────────────


async def test_receipts_flow_unaffected_when_disabled(clover_off: None) -> None:
    app = create_app()
    tid, uid = uuid.uuid4(), uuid.uuid4()
    connection: AsyncConnection
    async with engine.connect() as connection:
        await connection.begin()
        bound = make_bound_session(connection)
        app.dependency_overrides[get_db_session] = lambda: bound
        app.dependency_overrides[get_principal] = lambda: Principal(
            user_id=str(uid),
            workos_id=f"w_{uid.hex[:8]}",
            email="x@test.com",
            tenant_id=str(tid),
            role="manager",  # type: ignore[arg-type]
        )
        try:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'CvOff', :slug)"),
                {"id": tid, "slug": f"cv-{tid.hex[:8]}"},
            )
            await connection.execute(
                text("INSERT INTO users (id, workos_id, email) VALUES (:id, :w, :e)"),
                {"id": uid, "w": f"w_{uid.hex[:8]}", "e": f"{uid.hex[:8]}@test.com"},
            )
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                r = await c.post("/api/v1/receipts", json={"supplier_name": "NoClover Inc"})
                assert r.status_code == 201, r.text
                rid = r.json()["receipt_id"]
                detail = await c.get(f"/api/v1/receipts/{rid}")
                assert detail.status_code == 200
        finally:
            app.dependency_overrides.clear()
            await connection.rollback()


# ── env_check: Clover keys conditional ────────────────────────────────────────


def _base_env(**overrides: str) -> dict[str, str]:
    env = {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql+asyncpg://u:p@h/db",
        "SERVICE_DATABASE_URL": "postgresql+asyncpg://svc:p@h/db",
        "TOKEN_ENCRYPTION_KEY": "real-fernet-key-here",
    }
    env.update(overrides)
    return env


def test_env_check_skips_clover_when_disabled() -> None:
    r = check_env("migrate_job", _base_env(CLOVER_ENABLED="false"))
    assert r.ready, r.failures  # no Clover keys demanded
    r2 = check_env("migrate_job", _base_env())  # unset gate == disabled
    assert r2.ready


def test_env_check_requires_clover_when_enabled() -> None:
    r = check_env("migrate_job", _base_env(CLOVER_ENABLED="true"))
    assert not r.ready
    assert r.failures["CLOVER_APP_SECRET"] == "missing"
    assert r.failures["CLOVER_WEBHOOK_AUTH_CODE"] == "missing"

    ok = check_env(
        "migrate_job",
        _base_env(
            CLOVER_ENABLED="true",
            CLOVER_APP_SECRET="real-clover-secret-value",
            CLOVER_WEBHOOK_AUTH_CODE="real-webhook-auth-value",
        ),
    )
    assert ok.ready


# ── Clover workers: clean disabled exit ───────────────────────────────────────


async def test_inbox_worker_exits_cleanly_when_disabled(clover_off: None) -> None:
    from app.workers.inbox_worker import _main

    # Returns immediately (no queue, no DB, no SystemExit) when disabled.
    await _main()


async def test_reconciliation_worker_exits_cleanly_when_disabled(clover_off: None) -> None:
    from app.workers.reconciliation_worker import _main

    # Returns immediately (no tick loop, no token refresh, no DB) when disabled.
    await _main()
