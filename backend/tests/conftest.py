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

# Path to alembic.ini — one directory above tests/.
_ALEMBIC_INI = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")


# ── Schema governance ─────────────────────────────────────────────────────────


@pytest.fixture(scope="session", autouse=True)
def validate_schema() -> None:
    """Assert the database is at the alembic head revision before any test runs.

    Fails loudly so developers know to run `alembic upgrade head` first.
    Never auto-migrates — schema mutations are always explicit and deliberate.

    Uses asyncio.run() so the check runs in its own event loop before
    pytest-asyncio's session runner starts, avoiding loop-scope conflicts.
    """
    import asyncio

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(_ALEMBIC_INI)
    script = ScriptDirectory.from_config(cfg)
    head_rev = script.get_current_head()

    async def _read_current_rev() -> str | None:
        conn = await asyncpg.connect(DB_URL_SYNC)
        try:
            row = await conn.fetchrow(
                "SELECT version_num FROM alembic_version LIMIT 1"
            )
            return row["version_num"] if row else None
        finally:
            await conn.close()

    current_rev = asyncio.run(_read_current_rev())

    assert current_rev == head_rev, (
        f"\n\nDatabase schema is at revision {current_rev!r} "
        f"but alembic head is {head_rev!r}.\n"
        "Run:  alembic upgrade head\n"
        "then re-run the tests.\n"
    )


# ── TH-1: test-data leak detector ─────────────────────────────────────────────
# Proves test data self-cleans: snapshot every base table's row count at session
# start, assert return-to-baseline at session end. A non-zero delta = a test that
# committed rows and did not clean them up (the TH-1 accumulation defect). This is
# what keeps "tests self-clean" from being an atomic-by-construction, untested claim.


@pytest.fixture(scope="session", autouse=True)
def assert_no_test_data_leak(validate_schema: None) -> Any:
    import asyncio

    async def _counts() -> dict[str, int]:
        conn = await asyncpg.connect(DB_URL_SYNC)
        try:
            tables = [
                r["relname"]
                for r in await conn.fetch(
                    "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
                    " WHERE n.nspname='public' AND c.relkind='r' AND relname <> 'alembic_version'"
                )
            ]
            return {t: await conn.fetchval(f'SELECT count(*) FROM "{t}"') for t in tables}
        finally:
            await conn.close()

    before = asyncio.run(_counts())
    yield
    after = asyncio.run(_counts())
    deltas = {t: after[t] - before.get(t, 0) for t in after if after[t] != before.get(t, 0)}
    if deltas:
        print("\n\n===== TH-1 LEAK DETECTOR: tables not returned to pre-suite counts =====")
        for t, d in sorted(deltas.items(), key=lambda kv: -kv[1]):
            print(f"  {t:32s} {d:+d}")
        print("=" * 70)
    assert not deltas, f"test data leaked (did not self-clean): {deltas}"


# ── Tenant-isolation lint guard (revoke-IDOR class backstop) ──────────────────
# Attaches a SQL listener to the APP engine only and flags any SELECT/UPDATE/DELETE
# on a tenant-scoped table that lacks a tenant_id predicate (and isn't ALLOWLISTed).
# IDOR_GUARD_MEASURE=1 => collect+report, never fail (sizing the audit).
# See tests/idor_guard.py for the why (the read is the primary case for this codebase).


@pytest.fixture(scope="session", autouse=True)
def idor_guard() -> Any:
    """Session-scoped collector. The per-test `_idor_guard_attach` fixture binds a
    listener to each test's app engine (the engine is disposed+recreated per test by
    `reset_sa_engine`, so a one-time session listener would be stranded) and funnels
    violations here. At session end: report + (unless IDOR_GUARD_MEASURE=1) fail."""
    from tests import idor_guard as guard

    state = guard.GuardState(measure_only=os.environ.get("IDOR_GUARD_MEASURE") == "1")
    yield state

    print(f"\n[idor_guard] statements inspected: {state.seen}")
    if state.violations:
        print("\n\n===== IDOR GUARD: tenant-scoped queries with no tenant_id filter =====")
        print("(runtime guard — only test-EXERCISED paths are visible; green != IDOR-impossible)")
        for v in state.violations.values():
            print(f"\n  [{v.verb.upper()}] tables={','.join(v.tables)}")
            print(f"  {v.sql}")
        print("=" * 70)
        try:
            import json as _json

            with open("/tmp/idor_violations.txt", "w") as fh:
                for v in state.violations.values():
                    fh.write(f"[{v.verb.upper()}] {','.join(v.tables)}\n{v.sql}\n\n")
            with open("/tmp/idor_fingerprints.json", "w") as fh:
                _json.dump(
                    {v.fingerprint: f"{v.verb}:{','.join(v.tables)}" for v in state.violations.values()},
                    fh,
                    indent=2,
                )
        except OSError:
            pass
    if not state.measure_only:
        assert not state.violations, (
            f"{len(state.violations)} tenant-scoped query/queries lack a tenant_id filter "
            "(potential cross-tenant IDOR). See output above / tests/idor_guard.py ALLOWLIST."
        )


@pytest.fixture(autouse=True)
def _idor_guard_attach(idor_guard: Any) -> Any:
    """Attach the IDOR listener to THIS test's app engine instance (fresh per test)."""
    from sqlalchemy import event

    from app.core.database import get_engine
    from tests import idor_guard as guard

    state = idor_guard

    def _on_exec(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        state.seen += 1
        v = guard.inspect(statement)
        if v is None:
            return
        if not guard.app_origin():  # police app-issued queries only, not test reads
            return
        state.violations.setdefault(v.fingerprint, v)

    engine = get_engine()  # creates/returns this test's engine; app reuses the cached one
    event.listen(engine.sync_engine, "before_cursor_execute", _on_exec)
    try:
        yield
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _on_exec)


_TENANT_TABLES_CACHE: list[str] | None = None


@pytest.fixture(autouse=True)
def autoclean_committed_test_data() -> Any:
    """TH-1: per-test, delete the committed `tenants`/`users`/`pos_waitlist` rows THIS test
    created — captured-id discipline (the exact ids that appeared, never "where it looks like
    residue"). Rollback-fixture tests commit nothing → the diff is empty → no-op. Committing
    tests (admin_conn-seeded e2e/worker) self-clean even on failure: the finalizer runs
    regardless of the assert outcome (unlike inline cleanup-after-assert, which skips on
    failure and was the accumulation source). Tenant deletion cascades to every `tenant_id`
    table under `session_replication_role=replica` (order-independent; superuser only).

    SYNC fixture (+ asyncio.run) so it runs for SYNC tests too — an async autouse fixture
    errors on a sync test (no loop). Same pattern as the leak detector."""
    import asyncio

    async def _snapshot() -> tuple[set, set, set, set, set]:
        conn = await asyncpg.connect(DB_URL_SYNC)
        try:
            return (
                {r["id"] for r in await conn.fetch("SELECT id FROM tenants")},
                {r["id"] for r in await conn.fetch("SELECT id FROM users")},
                {r["id"] for r in await conn.fetch("SELECT id FROM pos_waitlist")},
                # oauth_states / monitoring_alerts can carry a tenant_id with NO tenant row
                # (e.g. state_manager unit tests) so the tenant-cascade misses them — diff
                # them directly by their own PK.
                {r["state"] for r in await conn.fetch("SELECT state FROM oauth_states")},
                {r["id"] for r in await conn.fetch("SELECT id FROM monitoring_alerts")},
            )
        finally:
            await conn.close()

    t0, u0, w0, o0, m0 = asyncio.run(_snapshot())
    yield

    async def _clean() -> None:
        conn = await asyncpg.connect(DB_URL_SYNC)
        try:
            new_t = list({r["id"] for r in await conn.fetch("SELECT id FROM tenants")} - t0)
            new_u = list({r["id"] for r in await conn.fetch("SELECT id FROM users")} - u0)
            new_w = list({r["id"] for r in await conn.fetch("SELECT id FROM pos_waitlist")} - w0)
            new_o = list({r["state"] for r in await conn.fetch("SELECT state FROM oauth_states")} - o0)
            new_m = list({r["id"] for r in await conn.fetch("SELECT id FROM monitoring_alerts")} - m0)
            if not (new_t or new_u or new_w or new_o or new_m):
                return
            global _TENANT_TABLES_CACHE
            if _TENANT_TABLES_CACHE is None:
                _TENANT_TABLES_CACHE = [
                    r["relname"]
                    for r in await conn.fetch(
                        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
                        " JOIN pg_attribute a ON a.attrelid=c.oid WHERE n.nspname='public'"
                        " AND c.relkind='r' AND a.attname='tenant_id' AND a.attnum>0"
                    )
                ]
            async with conn.transaction():
                await conn.execute("SET LOCAL session_replication_role = replica")
                if new_t:
                    for tbl in _TENANT_TABLES_CACHE:
                        await conn.execute(
                            f'DELETE FROM "{tbl}" WHERE tenant_id = ANY($1::uuid[])', new_t
                        )
                    await conn.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", new_t)
                if new_u:
                    await conn.execute("DELETE FROM users WHERE id = ANY($1::uuid[])", new_u)
                if new_w:
                    await conn.execute("DELETE FROM pos_waitlist WHERE id = ANY($1::uuid[])", new_w)
                if new_o:
                    await conn.execute("DELETE FROM oauth_states WHERE state = ANY($1::text[])", new_o)
                if new_m:
                    await conn.execute("DELETE FROM monitoring_alerts WHERE id = ANY($1::uuid[])", new_m)
        finally:
            await conn.close()

    asyncio.run(_clean())


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
    svc_url = DB_URL_SYNC.replace("reorderos:reorderos@", "service_worker:service_worker@")
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
