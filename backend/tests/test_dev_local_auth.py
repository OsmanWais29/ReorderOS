"""LOCAL-ONLY dev sign-in — the gate tests the founder required.

The dev auth mode must be provably fail-closed:
  * OFF by default — the endpoint answers 404 with no env flag set;
  * OFF in production/staging EVEN IF the flag is accidentally set — both the
    endpoint (404) and token verification (a dev token never authenticates);
  * ON only when LOCAL_DEV_AUTH=true AND app_env in {local, ci}: creates/reuses
    the dev tenant with an OWNER membership and mints a token the normal
    request path (get_identity → /auth/me) accepts;
  * flipping the flag off invalidates existing dev tokens immediately (the gate
    is per-request, not boot-time);
  * the WorkOS path is untouched when the gate is closed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import Settings, get_settings
from app.core.database import engine, get_session, make_bound_session
from app.main import create_app
from app.modules.auth.dev_local import (
    DEV_TENANT_SLUG,
    DEV_WORKOS_ID,
    dev_local_auth_enabled,
    mint_dev_token,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def app_instance() -> Any:
    return create_app()


@pytest.fixture
async def conn(app_instance: Any) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as connection:
        await connection.begin()
        bound = make_bound_session(connection)
        app_instance.dependency_overrides[get_session] = lambda: bound
        try:
            yield connection
        finally:
            app_instance.dependency_overrides.clear()
            await connection.rollback()


@pytest.fixture
async def client(app_instance: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app_instance), base_url="http://test") as c:
        yield c


@pytest.fixture
def settings_env(monkeypatch: Any) -> Any:
    """Mutate auth env for the duration of one test; the lru_cache is cleared on
    entry and exit so both this test and the NEXT one read fresh env."""

    def apply(**env: str) -> None:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


def _prod_settings_with_flag() -> Settings:
    """A production Settings object with the dev flag set — construction requires
    the F1.3 secrets, mirroring test_phase0_config."""
    return Settings(
        _env_file=None,
        APP_ENV="production",
        LOCAL_DEV_AUTH="true",
        DATABASE_URL="postgresql://u:p@h:5432/d",
        TOKEN_ENCRYPTION_KEY="k",
        SERVICE_DATABASE_URL="postgresql://u:p@h:5432/d",
        WORKOS_CLIENT_ID="client",
        WORKOS_JWKS_URL="https://jwks",
        CLOVER_APP_ID="app",
        CLOVER_APP_SECRET="secret",
        CLOVER_WEBHOOK_AUTH_CODE="whc",
    )


# ── gate: OFF by default ──────────────────────────────────────────────────────


async def test_dev_sign_in_is_404_when_flag_unset(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient, settings_env: Any
) -> None:
    settings_env(LOCAL_DEV_AUTH="false")  # explicit default
    r = await client.post("/api/v1/auth/dev-sign-in")
    assert r.status_code == 404


# ── gate: production NEVER qualifies, even with the flag set ──────────────────


def test_gate_rejects_production_and_staging_even_with_flag() -> None:
    prod = _prod_settings_with_flag()
    assert prod.local_dev_auth is True  # the flag really is set…
    assert dev_local_auth_enabled(prod) is False  # …and the gate still refuses

    staging = prod.model_copy(update={"app_env": "staging"})
    assert dev_local_auth_enabled(staging) is False

    local = prod.model_copy(update={"app_env": "local"})
    assert dev_local_auth_enabled(local) is True  # sanity: the gate CAN open


async def test_dev_sign_in_404_in_production_even_with_flag(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient, settings_env: Any
) -> None:
    settings_env(
        LOCAL_DEV_AUTH="true",
        APP_ENV="production",
        TOKEN_ENCRYPTION_KEY="k",
        SERVICE_DATABASE_URL="postgresql://u:p@h:5432/d",
        WORKOS_CLIENT_ID="client",
        WORKOS_JWKS_URL="https://jwks.invalid/keys",
        CLOVER_APP_ID="app",
        CLOVER_APP_SECRET="secret",
        CLOVER_WEBHOOK_AUTH_CODE="whc",
    )
    r = await client.post("/api/v1/auth/dev-sign-in")
    assert r.status_code == 404


async def test_dev_token_never_authenticates_in_production(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient, settings_env: Any
) -> None:
    """A token minted in dev mode must be worthless against a production app,
    flag or no flag: get_identity's gate closes and the token falls through to
    the WorkOS verifier, which cannot accept it (wrong alg/issuer)."""
    # Mint under LOCAL settings (the only mode that can mint)…
    local = Settings(
        _env_file=None, APP_ENV="local", LOCAL_DEV_AUTH="true", TOKEN_ENCRYPTION_KEY="k"
    )
    token = mint_dev_token(local)

    # …then run the app in production mode with the flag accidentally set.
    settings_env(
        LOCAL_DEV_AUTH="true",
        APP_ENV="production",
        TOKEN_ENCRYPTION_KEY="k",
        SERVICE_DATABASE_URL="postgresql://u:p@h:5432/d",
        WORKOS_CLIENT_ID="client",
        WORKOS_JWKS_URL="https://jwks.invalid/keys",
        CLOVER_APP_ID="app",
        CLOVER_APP_SECRET="secret",
        CLOVER_WEBHOOK_AUTH_CODE="whc",
    )
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    # 401 (rejected) or 503 (JWKS unreachable) — NEVER an authenticated 200.
    assert r.status_code in (401, 503)


# ── happy path: local + flag → owner user, working token ─────────────────────


async def test_dev_sign_in_creates_owner_and_token_authenticates(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient, settings_env: Any
) -> None:
    settings_env(LOCAL_DEV_AUTH="true", APP_ENV="local")

    r = await client.post("/api/v1/auth/dev-sign-in")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "owner"
    token = body["access_token"]

    # The token works through the NORMAL request path.
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    assert me.json()["user"]["email"] == "dev@local.test"
    assert any(t["slug"] == DEV_TENANT_SLUG for t in me.json()["tenants"])

    # DB truth: owner membership, exactly one dev tenant.
    role = (
        await conn.execute(
            text("""
                SELECT ut.role FROM user_tenants ut
                  JOIN users u ON u.id = ut.user_id
                  JOIN tenants t ON t.id = ut.tenant_id
                 WHERE u.workos_id = :wid AND t.slug = :slug
            """),
            {"wid": DEV_WORKOS_ID, "slug": DEV_TENANT_SLUG},
        )
    ).scalar_one()
    assert role == "owner"

    # Second call REUSES (idempotent): same tenant, no duplicate.
    r2 = await client.post("/api/v1/auth/dev-sign-in")
    assert r2.status_code == 200
    assert r2.json()["tenant_id"] == body["tenant_id"]
    n_tenants = (
        await conn.execute(
            text("SELECT count(*) FROM tenants WHERE slug = :slug"), {"slug": DEV_TENANT_SLUG}
        )
    ).scalar_one()
    assert n_tenants == 1


async def test_dev_token_dies_the_moment_the_flag_is_disabled(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient, settings_env: Any
) -> None:
    """The gate is PER-REQUEST: an existing dev token stops working instantly
    when the flag is turned off — no restart required, nothing lingers."""
    settings_env(LOCAL_DEV_AUTH="true", APP_ENV="local")
    r = await client.post("/api/v1/auth/dev-sign-in")
    assert r.status_code == 200
    token = r.json()["access_token"]

    settings_env(LOCAL_DEV_AUTH="false")
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code in (401, 503)  # falls through to WorkOS, never accepted


async def test_workos_path_unchanged_when_gate_closed(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient, settings_env: Any
) -> None:
    """With the gate closed, a non-dev bearer token takes exactly the
    pre-existing WorkOS path (still rejected — regression sanity)."""
    settings_env(LOCAL_DEV_AUTH="false")
    r = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code in (401, 503)
