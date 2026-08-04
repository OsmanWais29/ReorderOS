"""Restricted-role (reorderos_app) certification for auth/tenant resolution + domains.

Runs the REAL application code paths (resolve_principal, GET /auth/me, GET /tenants,
register-tenant, invitation accept, and the inventory/receipts/recipes/POS/inbound-admin
HTTP surfaces) under the ACTUAL deployment role `reorderos_app` — a LOGIN member of
app_user, subject to RLS, NOT super/bypassrls. The session factory connects DIRECTLY as
reorderos_app (not `SET ROLE` on a superuser: that is lost on pooled-connection reuse, so
a 2nd session in the same request would silently run as the bypassrls superuser and make
the tests vacuous). These FAIL against pre-fix code (masked by doadmin's BYPASSRLS) and
PASS after the fixes + migration 0035.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote, urlparse

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.core.database as _db
from app.core.security import Identity, get_identity
from app.main import create_app
from tests.conftest import DB_URL_SYNC, seed_membership, seed_tenant, seed_user

pytestmark = pytest.mark.integration

_ASYNC_URL = DB_URL_SYNC.replace("postgresql://", "postgresql+asyncpg://")


def _is_local_db(url: str) -> bool:
    """True only for a localhost DB — the guard that keeps the role-mutating fixture
    from EVER touching staging/production if DATABASE_URL is mispointed."""
    host = (urlparse(url.replace("postgresql+asyncpg://", "postgresql://")).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", ""}


@pytest.fixture(scope="session")
async def _reorderos_app_login() -> AsyncIterator[str]:
    """Provision reorderos_app as a genuine non-super/non-bypass LOGIN member of app_user
    with a RANDOM, in-memory-only password (never logged/persisted), so tests connect
    DIRECTLY as reorderos_app rather than `SET ROLE` on a superuser — SET ROLE is lost on
    pooled-connection reuse and would silently run handlers as the bypassrls superuser,
    making the tests vacuous. Yields the password for building the connect DSN.

    SAFETY: refuses to run against any non-local DB (cannot alter staging/production).
    TEARDOWN: disables LOGIN and scrambles the password — no known-password login is left
    behind. The NOLOGIN role remains (harmless; also used by test_startup_role_gate)."""
    if not _is_local_db(DB_URL_SYNC):
        pytest.skip("restricted-role tests mutate roles; only run against a LOCAL database")
    pw = secrets.token_hex(24)  # in memory only
    conn = await asyncpg.connect(DB_URL_SYNC)
    try:
        await conn.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='reorderos_app') "
            "THEN CREATE ROLE reorderos_app NOSUPERUSER NOBYPASSRLS INHERIT; END IF; END $$;"
        )
        # Enforce the security-relevant attributes even if the role pre-existed.
        await conn.execute("ALTER ROLE reorderos_app NOSUPERUSER NOBYPASSRLS INHERIT")
        await conn.execute(f"ALTER ROLE reorderos_app LOGIN PASSWORD '{pw}'")  # pw is hex-safe
        await conn.execute("GRANT app_user TO reorderos_app")
    finally:
        await conn.close()
    try:
        yield pw
    finally:
        conn = await asyncpg.connect(DB_URL_SYNC)
        try:
            # Leave NO usable login: disable + scramble to a fresh throwaway secret.
            await conn.execute("ALTER ROLE reorderos_app NOLOGIN")
            await conn.execute(f"ALTER ROLE reorderos_app PASSWORD '{secrets.token_hex(24)}'")
        finally:
            await conn.close()


@pytest.fixture
async def restricted_sessionmaker(
    _reorderos_app_login: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Any]:
    # Build the reorderos_app DSN from the random password (URL-encoded). Never printed.
    url = _ASYNC_URL.replace(
        "reorderos:reorderos@", f"reorderos_app:{quote(_reorderos_app_login, safe='')}@"
    )
    engine = create_async_engine(url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(_db, "_sessionmaker", sm, raising=False)
    monkeypatch.setattr(_db, "get_sessionmaker", lambda: sm)
    try:
        yield sm
    finally:
        await engine.dispose()


async def _client(app_identity: Identity) -> AsyncClient:
    app = create_app()
    app.dependency_overrides[get_identity] = lambda: app_identity
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _ident(user: Any) -> Identity:
    return Identity(
        workos_id=user["workos_id"],
        email=user["email"],
        email_verified=True,
        first_name=None,
        last_name=None,
    )


async def test_test_connection_is_reorderos_app(restricted_sessionmaker: Any) -> None:
    """Prove the test really runs as reorderos_app: member of app_user, not super,
    not bypassrls — otherwise every 'restricted' assertion below would be vacuous."""
    async with restricted_sessionmaker() as s:
        cu, is_super, bypass, member = (
            await s.execute(
                text(
                    "SELECT current_user,"
                    " (SELECT rolsuper FROM pg_roles WHERE rolname=current_user),"
                    " (SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user),"
                    " pg_has_role(current_user,'app_user','MEMBER')"
                )
            )
        ).one()
    assert cu == "reorderos_app"
    assert member is True
    assert is_super is False
    assert bypass is False


async def test_security_context_less_request_cannot_read_memberships(
    admin_conn: Any, app_conn: Any
) -> None:
    user = await seed_user(admin_conn)
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]))
    try:
        async with app_conn.transaction():
            await app_conn.execute("SET LOCAL app.user_id = ''")
            await app_conn.execute("SET LOCAL app.tenant_id = ''")
            await app_conn.execute("SET LOCAL app.rls_mode = ''")
            n = await app_conn.fetchval("SELECT count(*) FROM user_tenants")
        assert n == 0
    finally:
        await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id=$1", tenant["id"])
        await admin_conn.execute("DELETE FROM tenants WHERE id=$1", tenant["id"])
        await admin_conn.execute("DELETE FROM users WHERE id=$1", user["id"])


async def test_resolve_principal_succeeds_for_own_membership(
    admin_conn: Any, restricted_sessionmaker: Any
) -> None:
    from app.modules.tenants.repo import resolve_principal

    user = await seed_user(admin_conn)
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]), role="owner")
    try:
        p = await resolve_principal(_ident(user), str(tenant["id"]))
        assert p.tenant_id == str(tenant["id"]) and p.role == "owner"
        assert p.user_id == str(user["id"])
    finally:
        await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id=$1", tenant["id"])
        await admin_conn.execute("DELETE FROM tenants WHERE id=$1", tenant["id"])
        await admin_conn.execute("DELETE FROM users WHERE id=$1", user["id"])


async def test_resolve_principal_rejects_foreign_and_inactive(
    admin_conn: Any, restricted_sessionmaker: Any
) -> None:
    from fastapi import HTTPException

    from app.modules.tenants.repo import resolve_principal

    user = await seed_user(admin_conn)
    foreign = await seed_tenant(admin_conn)  # no membership
    inactive_t = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(inactive_t["id"]))
    await admin_conn.execute(
        "UPDATE user_tenants SET active=false WHERE user_id=$1 AND tenant_id=$2",
        user["id"],
        inactive_t["id"],
    )
    try:
        for tid in (foreign["id"], inactive_t["id"]):
            with pytest.raises(HTTPException) as ei:
                await resolve_principal(_ident(user), str(tid))
            assert ei.value.status_code == 403
    finally:
        await admin_conn.execute("DELETE FROM user_tenants WHERE user_id=$1", user["id"])
        await admin_conn.execute(
            "DELETE FROM tenants WHERE id IN ($1,$2)", foreign["id"], inactive_t["id"]
        )
        await admin_conn.execute("DELETE FROM users WHERE id=$1", user["id"])


async def test_auth_me_and_tenants_list_only_callers_active_tenants(
    admin_conn: Any, restricted_sessionmaker: Any
) -> None:
    a = await seed_user(admin_conn)
    b = await seed_user(admin_conn)  # a different user; A must NOT see B's tenants
    t1 = await seed_tenant(admin_conn)
    t2 = await seed_tenant(admin_conn)
    t_b = await seed_tenant(admin_conn)
    t_inactive = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(a["id"]), str(t1["id"]), role="owner")
    await seed_membership(admin_conn, str(a["id"]), str(t2["id"]), role="staff")
    await seed_membership(admin_conn, str(b["id"]), str(t_b["id"]))
    await seed_membership(admin_conn, str(a["id"]), str(t_inactive["id"]))
    await admin_conn.execute(
        "UPDATE user_tenants SET active=false WHERE user_id=$1 AND tenant_id=$2",
        a["id"],
        t_inactive["id"],
    )
    try:
        client = await _client(_ident(a))
        async with client:
            me = await client.get("/api/v1/auth/me")
            tl = await client.get("/api/v1/tenants")
        assert me.status_code == 200, me.text
        want = {str(t1["id"]), str(t2["id"])}
        assert {t["id"] for t in me.json()["tenants"]} == want  # only A's ACTIVE tenants
        assert {t["id"] for t in tl.json()} == want
        assert str(t_b["id"]) not in {t["id"] for t in tl.json()}  # never B's
    finally:
        ids = [t1["id"], t2["id"], t_b["id"], t_inactive["id"]]
        await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = ANY($1::uuid[])", ids)
        await admin_conn.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", ids)
        await admin_conn.execute("DELETE FROM users WHERE id = ANY($1::uuid[])", [a["id"], b["id"]])


async def test_register_tenant_works_under_reorderos_app(
    admin_conn: Any, restricted_sessionmaker: Any
) -> None:
    user = await seed_user(admin_conn)
    slug = f"reg-{uuid.uuid4().hex[:8]}"
    try:
        client = await _client(_ident(user))
        async with client:
            r = await client.post(
                "/api/v1/auth/register-tenant", json={"slug": slug, "name": "Reg Co"}
            )
        assert r.status_code == 201, r.text
        assert r.json()["membership"]["role"] == "owner"
    finally:
        await admin_conn.execute("DELETE FROM user_tenants WHERE user_id=$1", user["id"])
        await admin_conn.execute("DELETE FROM tenants WHERE slug=$1", slug)
        await admin_conn.execute("DELETE FROM users WHERE id=$1", user["id"])


async def test_invitation_accept_works_under_reorderos_app(
    admin_conn: Any, restricted_sessionmaker: Any
) -> None:
    owner = await seed_user(admin_conn)
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(owner["id"]), str(tenant["id"]), role="owner")
    invitee = await seed_user(admin_conn)
    tok = uuid.uuid4().hex
    await admin_conn.execute(
        "INSERT INTO invitations (id,tenant_id,email,role,token,expires_at) "
        "VALUES ($1,$2,$3,'staff',$4, now() + interval '7 days')",
        uuid.uuid4(),
        tenant["id"],
        invitee["email"],
        tok,
    )
    try:
        client = await _client(_ident(invitee))
        async with client:
            r = await client.post("/api/v1/invitations/accept", json={"token": tok})
        assert r.status_code in (200, 201), r.text
    finally:
        await admin_conn.execute("DELETE FROM invitations WHERE tenant_id=$1", tenant["id"])
        await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id=$1", tenant["id"])
        await admin_conn.execute("DELETE FROM tenants WHERE id=$1", tenant["id"])
        await admin_conn.execute(
            "DELETE FROM users WHERE id = ANY($1::uuid[])", [owner["id"], invitee["id"]]
        )


async def test_register_mode_stays_row_scoped_no_foreign_leak(
    admin_conn: Any, restricted_sessionmaker: Any
) -> None:
    """SECURITY: while register mode is LIVE, a broad SELECT on tenants/user_tenants
    must return ONLY the caller's own rows — never every tenant. This is the property
    a `rls_mode IN (...)` carve-out would violate (unconditionally true → whole table).
    The new tenant's INSERT...RETURNING must still be visible (bound app.tenant_id)."""
    from app.modules.tenants.repo import register_tenant

    foreign = await seed_tenant(admin_conn)  # unrelated tenant, different owner
    other = await seed_user(admin_conn)
    await seed_membership(admin_conn, str(other["id"]), str(foreign["id"]))
    user = await seed_user(admin_conn)
    slug = f"reg-{uuid.uuid4().hex[:8]}"
    try:
        async with restricted_sessionmaker() as s:
            tenant, _u, _m = await register_tenant(s, _ident(user), slug=slug, name="Reg Co")
            # register mode is STILL LIVE (router commits later). Probe broad reads:
            tids = {r[0] for r in (await s.execute(text("SELECT id::text FROM tenants"))).all()}
            assert str(tenant.id) in tids  # own new tenant visible → RETURNING works
            assert str(foreign["id"]) not in tids  # foreign tenant NOT exposed
            mids = {
                r[0]
                for r in (await s.execute(text("SELECT tenant_id::text FROM user_tenants"))).all()
            }
            assert str(foreign["id"]) not in mids  # foreign membership NOT exposed
            await s.rollback()
    finally:
        await admin_conn.execute(
            "DELETE FROM user_tenants WHERE user_id = ANY($1::uuid[])", [other["id"]]
        )
        await admin_conn.execute("DELETE FROM tenants WHERE id=$1 OR slug=$2", foreign["id"], slug)
        await admin_conn.execute(
            "DELETE FROM users WHERE id = ANY($1::uuid[])", [other["id"], user["id"]]
        )


async def test_accept_invite_mode_stays_row_scoped_no_foreign_leak(
    admin_conn: Any, restricted_sessionmaker: Any
) -> None:
    """SECURITY: while accept_invite mode is LIVE, a broad SELECT on user_tenants must
    return ONLY the invitee's own memberships — never every tenant's. The invitee's new
    membership INSERT...RETURNING must still be visible (bound app.user_id)."""
    from app.modules.invitations.repo import accept_invitation

    foreign = await seed_tenant(admin_conn)  # unrelated tenant
    other = await seed_user(admin_conn)
    await seed_membership(admin_conn, str(other["id"]), str(foreign["id"]))
    inviting = await seed_tenant(admin_conn)
    owner = await seed_user(admin_conn)
    await seed_membership(admin_conn, str(owner["id"]), str(inviting["id"]), role="owner")
    invitee = await seed_user(admin_conn)
    tok = uuid.uuid4().hex
    await admin_conn.execute(
        "INSERT INTO invitations (id,tenant_id,email,role,token,expires_at) "
        "VALUES ($1,$2,$3,'staff',$4, now() + interval '7 days')",
        uuid.uuid4(),
        inviting["id"],
        invitee["email"],
        tok,
    )
    try:
        async with restricted_sessionmaker() as s:
            m = await accept_invitation(
                s,
                token=tok,
                identity_email=invitee["email"],
                identity_workos_id=invitee["workos_id"],
            )
            # accept_invite mode is STILL LIVE. Probe broad read of user_tenants:
            mids = {
                r[0]
                for r in (await s.execute(text("SELECT tenant_id::text FROM user_tenants"))).all()
            }
            assert str(m.tenant_id) in mids  # own new membership visible → RETURNING works
            assert str(foreign["id"]) not in mids  # foreign membership NOT exposed
            await s.rollback()
    finally:
        await admin_conn.execute("DELETE FROM invitations WHERE tenant_id=$1", inviting["id"])
        await admin_conn.execute(
            "DELETE FROM user_tenants WHERE tenant_id = ANY($1::uuid[])",
            [foreign["id"], inviting["id"]],
        )
        await admin_conn.execute(
            "DELETE FROM tenants WHERE id = ANY($1::uuid[])", [foreign["id"], inviting["id"]]
        )
        await admin_conn.execute(
            "DELETE FROM users WHERE id = ANY($1::uuid[])",
            [other["id"], owner["id"], invitee["id"]],
        )


async def test_schema_head_readable_under_reorderos_app(restricted_sessionmaker: Any) -> None:
    """The API startup schema-head check reads alembic_version on the request path."""
    async with restricted_sessionmaker() as s:
        v = (await s.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    assert v == "0035_restricted_runtime_role"


# ═════════════════════════════════════════════════════════════════════════════
# DOMAIN endpoint coverage under reorderos_app (real HTTP, real principal
# resolution, real RLS). Each domain proves: (1) own-tenant behavior works under
# the non-bypassrls role, and (2) foreign-tenant isolation — either principal-level
# (X-Tenant-Id of a non-member tenant → 403 in resolve_principal) or resource-level
# (a foreign row is invisible → 404, enforced by RLS, not an app WHERE clause).
# ═════════════════════════════════════════════════════════════════════════════


async def _two_owners(admin: Any) -> tuple[Any, Any, Any, Any]:
    """Two users, each OWNER of their own tenant. Returns (userA, userB, tenantA, tenantB)."""
    a = await seed_user(admin)
    b = await seed_user(admin)
    ta = await seed_tenant(admin)
    tb = await seed_tenant(admin)
    await seed_membership(admin, str(a["id"]), str(ta["id"]), role="owner")
    await seed_membership(admin, str(b["id"]), str(tb["id"]), role="owner")
    return a, b, ta, tb


async def _seed_unit(admin: Any, tid: Any) -> Any:
    return await admin.fetchval(
        "INSERT INTO units_of_measure (id,tenant_id,name,abbreviation,unit_type) "
        "VALUES (gen_random_uuid(),$1,'Each','ea','count') RETURNING id",
        tid,
    )


async def _seed_item(admin: Any, tid: Any, unit_id: Any, name: str = "Widget") -> Any:
    return await admin.fetchval(
        "INSERT INTO inventory_items (id,tenant_id,name,inventory_mode,storage_unit_id) "
        "VALUES (gen_random_uuid(),$1,$2,'recipe_deducted',$3) RETURNING id",
        tid,
        name,
        unit_id,
    )


async def _seed_menu_item(admin: Any, tid: Any, name: str = "Burger") -> Any:
    return await admin.fetchval(
        "INSERT INTO menu_items (id,tenant_id,name) VALUES (gen_random_uuid(),$1,$2) RETURNING id",
        tid,
        name,
    )


async def _seed_recipe(admin: Any, tid: Any, menu_item_id: Any) -> Any:
    return await admin.fetchval(
        "INSERT INTO recipes (id,tenant_id,menu_item_id) "
        "VALUES (gen_random_uuid(),$1,$2) RETURNING id",
        tid,
        menu_item_id,
    )


async def _hdr(tid: Any) -> dict[str, str]:
    return {"X-Tenant-Id": str(tid)}


async def test_inventory_read_scoped_and_denied(
    admin_conn: Any, restricted_sessionmaker: Any
) -> None:
    a, _b, ta, tb = await _two_owners(admin_conn)
    ua = await _seed_unit(admin_conn, ta["id"])
    ub = await _seed_unit(admin_conn, tb["id"])
    item_a = await _seed_item(admin_conn, ta["id"], ua, name="A-widget")
    item_b = await _seed_item(admin_conn, tb["id"], ub, name="B-widget")
    try:
        client = await _client(_ident(a))
        async with client:
            own = await client.get("/api/v1/inventory/items", headers=await _hdr(ta["id"]))
            cross = await client.get("/api/v1/inventory/items", headers=await _hdr(tb["id"]))
        assert own.status_code == 200, own.text
        ids = {i["id"] for i in own.json()["items"]}
        assert str(item_a) in ids  # own item visible
        assert str(item_b) not in ids  # foreign item hidden by RLS (load-bearing)
        assert cross.status_code == 403  # A is not a member of tenant B (principal denial)
    finally:
        await admin_conn.execute(
            "DELETE FROM inventory_items WHERE tenant_id = ANY($1::uuid[])", [ta["id"], tb["id"]]
        )
        await admin_conn.execute(
            "DELETE FROM units_of_measure WHERE tenant_id = ANY($1::uuid[])", [ta["id"], tb["id"]]
        )
        await _cleanup_tenants(admin_conn, [a], [ta, tb])


async def test_inventory_writes_work_under_restricted_role(
    admin_conn: Any, restricted_sessionmaker: Any
) -> None:
    """Flag #1 regression: these handlers do DB work AFTER their first commit
    (post-commit read-back / idempotency write). Under the non-bypassrls request role
    that work runs with empty RLS context unless re-established — 500 before the fix,
    201 after. Covers ALL THREE fixed handlers: create_receipt AND create_count_event
    (unconditional post-commit SELECT) + opening-balance (post-commit store_response)."""
    a, _b, ta, tb = await _two_owners(admin_conn)
    ua = await _seed_unit(admin_conn, ta["id"])
    item_a = await _seed_item(admin_conn, ta["id"], ua)
    try:
        client = await _client(_ident(a))
        async with client:
            # create_receipt_endpoint: commit → post-commit SELECT ... FROM receipts (line 264)
            r_receipt = await client.post(
                "/api/v1/inventory/receipts", json={}, headers=await _hdr(ta["id"])
            )
            # opening-balance FIRST (must be the item's first movement) with Idempotency-Key:
            # commit → post-commit store_response write.
            r_ob = await client.post(
                f"/api/v1/inventory/items/{item_a}/opening-balance",
                json={"quantity": "5"},
                headers={**await _hdr(ta["id"]), "Idempotency-Key": uuid.uuid4().hex},
            )
            # create_count_event: commit → UNCONDITIONAL post-commit SELECT created_at (line 107)
            r_count = await client.post(
                "/api/v1/inventory/count-events",
                json={"inventory_item_id": str(item_a), "counted_quantity": "3"},
                headers={**await _hdr(ta["id"]), "Idempotency-Key": uuid.uuid4().hex},
            )
            # cross-tenant write denied at principal resolution
            r_cross = await client.post(
                "/api/v1/inventory/receipts", json={}, headers=await _hdr(tb["id"])
            )
        assert r_receipt.status_code == 201, r_receipt.text  # post-commit read-back succeeded
        assert r_count.status_code == 201, r_count.text  # post-commit read + idem write succeeded
        assert r_ob.status_code == 201, r_ob.text  # post-commit idempotency write succeeded
        assert r_cross.status_code == 403  # A not a member of B
    finally:
        ids = [ta["id"], tb["id"]]
        for tbl in (
            "monitoring_alerts",
            "inventory_count_events",
            "inventory_movements",
            "idempotency_keys",
            "receipts",
            "inventory_items",
            "units_of_measure",
        ):
            await admin_conn.execute(f"DELETE FROM {tbl} WHERE tenant_id = ANY($1::uuid[])", ids)
        await _cleanup_tenants(admin_conn, [a], [ta, tb])


async def test_receipts_read_scoped_and_denied(
    admin_conn: Any, restricted_sessionmaker: Any
) -> None:
    a, _b, ta, tb = await _two_owners(admin_conn)
    # A creates a receipt via the real endpoint; B's receipt is seeded directly.
    foreign_receipt = await admin_conn.fetchval(
        "INSERT INTO receipts (id,tenant_id,source) VALUES (gen_random_uuid(),$1,'manual') RETURNING id",
        tb["id"],
    )
    try:
        client = await _client(_ident(a))
        async with client:
            created = await client.post(
                "/api/v1/inventory/receipts", json={}, headers=await _hdr(ta["id"])
            )
            assert created.status_code == 201, created.text
            own_id = created.json()["id"]
            lst = await client.get("/api/v1/receipts", headers=await _hdr(ta["id"]))
            own_detail = await client.get(
                f"/api/v1/receipts/{own_id}", headers=await _hdr(ta["id"])
            )
            foreign_detail = await client.get(
                f"/api/v1/receipts/{foreign_receipt}", headers=await _hdr(ta["id"])
            )
            cross = await client.get("/api/v1/receipts", headers=await _hdr(tb["id"]))
        assert lst.status_code == 200, lst.text
        assert own_detail.status_code == 200
        assert str(foreign_receipt) not in lst.text  # foreign receipt absent from own list (RLS)
        assert foreign_detail.status_code == 404  # foreign receipt invisible → RLS (load-bearing)
        assert cross.status_code == 403  # A not a member of B
    finally:
        await admin_conn.execute(
            "DELETE FROM receipts WHERE tenant_id = ANY($1::uuid[])", [ta["id"], tb["id"]]
        )
        await _cleanup_tenants(admin_conn, [a], [ta, tb])


async def test_recipes_read_write_scoped_and_denied(
    admin_conn: Any, restricted_sessionmaker: Any
) -> None:
    a, _b, ta, tb = await _two_owners(admin_conn)
    mi_a = await _seed_menu_item(admin_conn, ta["id"], name="A-dish")
    await _seed_recipe(admin_conn, ta["id"], mi_a)
    mi_b = await _seed_menu_item(admin_conn, tb["id"], name="B-dish")
    await _seed_recipe(admin_conn, tb["id"], mi_b)
    try:
        client = await _client(_ident(a))
        async with client:
            lst = await client.get("/api/v1/onboarding/recipes", headers=await _hdr(ta["id"]))
            own_detail = await client.get(
                f"/api/v1/onboarding/recipes/{mi_a}", headers=await _hdr(ta["id"])
            )
            foreign_detail = await client.get(
                f"/api/v1/onboarding/recipes/{mi_b}", headers=await _hdr(ta["id"])
            )
            # write (skip is a manager mutation): own succeeds, foreign 404 under RLS
            own_write = await client.post(
                f"/api/v1/onboarding/recipes/{mi_a}/skip", headers=await _hdr(ta["id"])
            )
            foreign_write = await client.post(
                f"/api/v1/onboarding/recipes/{mi_b}/skip", headers=await _hdr(ta["id"])
            )
            cross = await client.get("/api/v1/onboarding/recipes", headers=await _hdr(tb["id"]))
        assert lst.status_code == 200, lst.text
        assert str(mi_b) not in lst.text  # foreign menu item/recipe absent from own list (RLS)
        assert own_detail.status_code == 200
        assert foreign_detail.status_code == 404  # RLS (load-bearing)
        assert own_write.status_code == 200, own_write.text  # own write works under reorderos_app
        assert foreign_write.status_code == 404  # foreign write blocked (menu item invisible)
        assert cross.status_code == 403  # A not a member of B
    finally:
        await admin_conn.execute(
            "DELETE FROM recipes WHERE tenant_id = ANY($1::uuid[])", [ta["id"], tb["id"]]
        )
        await admin_conn.execute(
            "DELETE FROM menu_items WHERE tenant_id = ANY($1::uuid[])", [ta["id"], tb["id"]]
        )
        await _cleanup_tenants(admin_conn, [a], [ta, tb])


async def test_pos_status_under_restricted_role(
    admin_conn: Any, restricted_sessionmaker: Any
) -> None:
    """POS status uses a raw session + SET app.tenant_id only (Flag #2 partial context).
    Prove it still works under reorderos_app for the own tenant, and that cross-tenant
    access is denied at principal resolution (403), matching the mechanism."""
    a, _b, ta, tb = await _two_owners(admin_conn)
    try:
        client = await _client(_ident(a))
        async with client:
            own = await client.get("/api/v1/pos/clover/status", headers=await _hdr(ta["id"]))
            cross = await client.get("/api/v1/pos/clover/status", headers=await _hdr(tb["id"]))
        assert own.status_code == 200, own.text  # raw-session + SET tenant_id path works
        assert own.json().get("connected") is False  # no connection for A
        assert cross.status_code == 403  # A not a member of B
    finally:
        await _cleanup_tenants(admin_conn, [a], [ta, tb])


async def test_inbound_rotate_writes_and_isolates_under_restricted_role(
    admin_conn: Any, restricted_sessionmaker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REAL inbound WRITE under reorderos_app — Postmark ENABLED (safe test config).
    POST /inbound-address/rotate must return exactly 200 (NOT the 409 feature-off path),
    revoke the caller's existing token, leave exactly ONE active token, and leave a foreign
    tenant's token untouched, inaccessible, and absent from the response. Cross-tenant
    rotate is denied. No assertion accepts 409 as success."""
    from app.core.config import get_settings

    # Enable Postmark inbound with safe, test-only config; refresh the cached Settings so
    # the in-process ASGITransport app sees it. APP_ENV is local in tests, so enabling it
    # does not trip config's production webhook-cred requirement.
    monkeypatch.setenv("POSTMARK_INBOUND_ENABLED", "true")
    monkeypatch.setenv("POSTMARK_INBOUND_ADDRESS", "invoices@inbound.test")
    get_settings.cache_clear()

    a, _b, ta, tb = await _two_owners(admin_conn)
    old_a = "olda" + secrets.token_hex(8)
    tok_b = "bbbb" + secrets.token_hex(8)
    await admin_conn.execute(
        "INSERT INTO tenant_inbound_email_tokens (tenant_id, token) VALUES ($1,$2)",
        ta["id"],
        old_a,
    )
    await admin_conn.execute(
        "INSERT INTO tenant_inbound_email_tokens (tenant_id, token) VALUES ($1,$2)",
        tb["id"],
        tok_b,
    )
    try:
        client = await _client(_ident(a))
        async with client:
            r = await client.post(
                "/api/v1/receipts/inbound-address/rotate", headers=await _hdr(ta["id"])
            )
            # cross-tenant rotate: A is not a member of B → denied at principal resolution
            cross = await client.post(
                "/api/v1/receipts/inbound-address/rotate", headers=await _hdr(tb["id"])
            )
        assert r.status_code != 409, r.text  # NOT the feature-disabled path
        assert r.status_code == 200, r.text  # real write succeeded under reorderos_app
        body = r.json()
        assert body.get("rotated") is True

        # A: exactly one active token, and it is NOT the revoked old one
        active_a = await admin_conn.fetch(
            "SELECT token FROM tenant_inbound_email_tokens "
            "WHERE tenant_id=$1 AND revoked_at IS NULL",
            ta["id"],
        )
        assert len(active_a) == 1, active_a
        new_a = active_a[0]["token"]
        assert new_a != old_a
        old_revoked = await admin_conn.fetchval(
            "SELECT revoked_at IS NOT NULL FROM tenant_inbound_email_tokens "
            "WHERE tenant_id=$1 AND token=$2",
            ta["id"],
            old_a,
        )
        assert old_revoked is True  # the caller's prior token was revoked

        # B: untouched — still exactly one active token, unchanged value
        active_b = await admin_conn.fetch(
            "SELECT token FROM tenant_inbound_email_tokens "
            "WHERE tenant_id=$1 AND revoked_at IS NULL",
            tb["id"],
        )
        assert len(active_b) == 1 and active_b[0]["token"] == tok_b

        # Response exposes only A's NEW token; never B's token
        assert new_a in body["address"]
        assert tok_b not in r.text

        assert cross.status_code == 403  # cross-tenant rotate denied
    finally:
        get_settings.cache_clear()  # restore cached Settings for later tests
        await admin_conn.execute(
            "DELETE FROM tenant_inbound_email_tokens WHERE tenant_id = ANY($1::uuid[])",
            [ta["id"], tb["id"]],
        )
        await _cleanup_tenants(admin_conn, [a], [ta, tb])


async def _cleanup_tenants(admin: Any, users: list[Any], tenants: list[Any]) -> None:
    tids = [t["id"] for t in tenants]
    members = await admin.fetch(
        "SELECT DISTINCT user_id FROM user_tenants WHERE tenant_id = ANY($1::uuid[])", tids
    )
    await admin.execute("DELETE FROM user_tenants WHERE tenant_id = ANY($1::uuid[])", tids)
    await admin.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", tids)
    all_uids = list({*(r["user_id"] for r in members), *(u["id"] for u in users)})
    await admin.execute("DELETE FROM users WHERE id = ANY($1::uuid[])", all_uids)
