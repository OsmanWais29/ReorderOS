"""Pytest fixtures for Sprint 1 + Sprint 2."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import Identity
from app.main import app

# Derive sync URL from DATABASE_URL env var so the same fixtures work in CI
# (port 5432) and local dev (port 5433). Strip the +asyncpg driver prefix since
# asyncpg.connect() doesn't use SQLAlchemy DSNs.
DB_URL_SYNC = (
    os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://reorderos:reorderos@localhost:5433/reorderos",
    )
    .replace("postgresql+asyncpg://", "postgresql://")
    .replace("postgres://", "postgresql://")
)


# ── Engine reset between tests ────────────────────────────────────────────────
# SQLAlchemy's async engine is a module-level singleton tied to the event loop
# that created it. pytest-asyncio 1.x gives each test its own event loop, so
# the pool from test N is invalid in test N+1. Resetting synchronously (before
# the next test's loop starts) avoids the "Future attached to a different loop"
# error at the cost of one reconnect per test.


@pytest.fixture(autouse=True)
def reset_sa_engine() -> Any:
    from app.core.database import dispose_engine_sync
    from app.core.service_db import dispose_service_engine_sync

    yield
    dispose_engine_sync()
    dispose_service_engine_sync()


# ── App client ────────────────────────────────────────────────────────────────


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ── JWT / RSA fixtures (session-scoped — key generation is slow) ──────────────


@pytest.fixture(scope="session")
def rsa_private_key() -> Any:
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def fake_jwks(rsa_private_key: Any) -> dict[str, Any]:
    """JWKS document containing the test public key."""
    import json

    import jwt.algorithms

    pub = rsa_private_key.public_key()
    jwk_str = jwt.algorithms.RSAAlgorithm.to_jwk(pub)
    jwk: dict[str, Any] = json.loads(jwk_str)
    jwk["kid"] = "test-key-1"
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return {"keys": [jwk]}


@pytest.fixture(scope="session")
def make_token(rsa_private_key: Any) -> Callable[..., str]:
    """Factory: returns a signed RS256 JWT with sane defaults."""
    import jwt as _jwt

    def _make(
        *,
        sub: str = "user_workos_test",
        email: str = "test@example.com",
        email_verified: bool | str = True,
        iss: str = "https://api.workos.com/user_management/client_ci_fake",
        exp_delta: int = 3600,
        kid: str = "test-key-1",
        alg: str = "RS256",
        extra_headers: dict[str, Any] | None = None,
        **extra: Any,
    ) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": sub,
            "email": email,
            "email_verified": email_verified,
            "iss": iss,
            "iat": now,
            "exp": now + exp_delta,
            **extra,
        }
        headers: dict[str, Any] = {"kid": kid}
        if extra_headers:
            headers.update(extra_headers)
        return _jwt.encode(
            payload,
            rsa_private_key,
            algorithm=alg,
            headers=headers,
        )

    return _make


# ── JWTVerifier with fake JWKS ────────────────────────────────────────────────


@pytest.fixture
def verifier(fake_jwks: dict[str, Any]) -> Any:
    """A JWTVerifier pre-seeded with the fake JWKS (no HTTP required)."""
    import json
    import time as _time

    import jwt.algorithms

    from app.core.security import JWTVerifier

    v = JWTVerifier(
        jwks_url="https://fake.jwks/",
        issuer="https://api.workos.com/user_management/client_ci_fake",
        client_id="client_test",
        verify_audience=False,
    )
    pub_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(fake_jwks["keys"][0]))
    v._keys = {"test-key-1": pub_key}
    v._fetched_at = _time.monotonic()
    return v


# ── Integration DB connections ────────────────────────────────────────────────


@pytest.fixture
async def admin_conn() -> AsyncIterator[Any]:
    """Direct asyncpg connection as superuser (bypasses FORCE RLS)."""
    conn = await asyncpg.connect(DB_URL_SYNC)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def app_conn() -> AsyncIterator[Any]:
    """Separate asyncpg connection running as app_user (subject to FORCE RLS).

    Uses a fresh connection independent of admin_conn so superuser operations
    (via admin_conn) are not affected by SET ROLE.
    """
    conn = await asyncpg.connect(DB_URL_SYNC)
    await conn.execute("SET ROLE app_user")
    try:
        yield conn
    finally:
        await conn.execute("RESET ROLE")
        await conn.close()


@pytest.fixture
async def service_conn() -> AsyncIterator[Any]:
    """Direct asyncpg connection as service_worker (subject to RLS, cross-tenant).

    service_worker has LOGIN so this connects directly via its own credentials.
    It has SELECT + UPDATE on tenant_pos_connections and pos_event_inbox, and
    USING(true) RLS on those tables — no SET LOCAL app.tenant_id required.
    """
    svc_url = DB_URL_SYNC.replace(
        "reorderos:reorderos@", "service_worker:service_worker@"
    )
    conn = await asyncpg.connect(svc_url)
    try:
        yield conn
    finally:
        await conn.close()


# ── Shared test identities ────────────────────────────────────────────────────


@pytest.fixture
def test_identity() -> Identity:
    return Identity(
        workos_id="user_test_fixed",
        email="owner@example.com",
        email_verified=True,
        first_name="Owner",
        last_name="Test",
    )


@pytest.fixture
def manager_identity() -> Identity:
    return Identity(
        workos_id="user_manager_fixed",
        email="manager@example.com",
        email_verified=True,
        first_name="Manager",
        last_name="Test",
    )


@pytest.fixture
def staff_identity() -> Identity:
    return Identity(
        workos_id="user_staff_fixed",
        email="staff@example.com",
        email_verified=True,
        first_name="Staff",
        last_name="Test",
    )


# ── Authenticated client helpers ──────────────────────────────────────────────


@pytest.fixture
async def owner_client(client: AsyncClient, test_identity: Identity) -> AsyncIterator[AsyncClient]:
    from app.core.security import get_identity

    app.dependency_overrides[get_identity] = lambda: test_identity
    yield client
    app.dependency_overrides.pop(get_identity, None)


@pytest.fixture
async def manager_client(
    client: AsyncClient, manager_identity: Identity
) -> AsyncIterator[AsyncClient]:
    from app.core.security import get_identity

    app.dependency_overrides[get_identity] = lambda: manager_identity
    yield client
    app.dependency_overrides.pop(get_identity, None)


@pytest.fixture
async def staff_client(client: AsyncClient, staff_identity: Identity) -> AsyncIterator[AsyncClient]:
    from app.core.security import get_identity

    app.dependency_overrides[get_identity] = lambda: staff_identity
    yield client
    app.dependency_overrides.pop(get_identity, None)


# ── DB seeding helpers (call from tests using admin_conn) ─────────────────────


async def seed_user(
    conn: Any,
    *,
    workos_id: str | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    wid = workos_id or f"user_{uuid.uuid4().hex[:8]}"
    em = email or f"{uuid.uuid4().hex[:8]}@test.com"
    row = await conn.fetchrow(
        "INSERT INTO users (workos_id, email, email_verified)"
        " VALUES ($1, $2, true) RETURNING id, workos_id, email",
        wid,
        em,
    )
    return dict(row)


async def seed_tenant(
    conn: Any,
    *,
    slug: str | None = None,
    name: str = "Test Tenant",
) -> dict[str, Any]:
    s = slug or f"tenant-{uuid.uuid4().hex[:8]}"
    row = await conn.fetchrow(
        "INSERT INTO tenants (slug, name) VALUES ($1, $2) RETURNING id, slug, name",
        s,
        name,
    )
    return dict(row)


async def seed_membership(
    conn: Any,
    user_id: str,
    tenant_id: str,
    role: str = "owner",
) -> dict[str, Any]:
    row = await conn.fetchrow(
        "INSERT INTO user_tenants (user_id, tenant_id, role)"
        " VALUES ($1, $2, $3) RETURNING id, user_id, tenant_id, role",
        user_id,
        tenant_id,
        role,
    )
    return dict(row)
