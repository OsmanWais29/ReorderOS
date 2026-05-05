"""End-to-end auth pipeline integration tests.

Covers the three gaps left by dependency_overrides-based tests:

  Gap 1 — Bearer token travels the full JWTVerifier → get_identity pipeline.
  Gap 2 — get_principal hits a real DB membership lookup, not a stub.
  Gap 3 — RBAC role guard fires after real get_principal resolution.

The singleton JWTVerifier is replaced via dependency_overrides[get_jwt_verifier]
so we use a fake RSA key instead of real WorkOS creds — but every other seam
in the pipeline is real: HTTP bearer extraction, JWT decode, DB lookup,
set_rls_context call, and route handler logic.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest
from httpx import AsyncClient

from tests.conftest import seed_membership, seed_tenant, seed_user

_WORKOS_ID = "user_workos_test"
_EMAIL = "test@example.com"


def _headers(token: str, tenant_id: str | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {token}"}
    if tenant_id:
        h["X-Tenant-Id"] = tenant_id
    return h


@pytest.fixture(autouse=True)
def _inject_fake_verifier(verifier: Any) -> Any:
    """Swap real WorkOS JWTVerifier for the test RSA-key verifier for this file."""
    from app.core.security import get_jwt_verifier
    from app.main import app

    app.dependency_overrides[get_jwt_verifier] = lambda: verifier
    yield
    app.dependency_overrides.pop(get_jwt_verifier, None)


# ── Gap 1: Bearer token travels the real JWTVerifier pipeline ────────────────


@pytest.mark.integration
async def test_valid_bearer_reaches_route_handler(
    client: AsyncClient, make_token: Any, admin_conn: Any
) -> None:
    """RS256 token → HTTPBearer → JWTVerifier.verify → get_identity → /auth/me 200."""
    user = await seed_user(admin_conn, workos_id=_WORKOS_ID, email=_EMAIL)

    token = make_token()
    r = await client.get("/api/v1/auth/me", headers=_headers(token))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["workos_id"] == _WORKOS_ID
    assert body["user"]["email"] == _EMAIL

    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest.mark.integration
async def test_expired_bearer_is_rejected_by_verifier(
    client: AsyncClient, make_token: Any, admin_conn: Any
) -> None:
    """Expired JWT is caught by JWTVerifier before reaching the route handler."""
    user = await seed_user(admin_conn, workos_id=_WORKOS_ID, email=_EMAIL)

    token = make_token(exp_delta=-1)
    r = await client.get("/api/v1/auth/me", headers=_headers(token))

    assert r.status_code == 401
    assert "expired" in r.json()["detail"].lower()

    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest.mark.integration
async def test_missing_authorization_header_returns_401(
    client: AsyncClient,
) -> None:
    """No Authorization header → 401 before touching the DB."""
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401


# ── Gap 2: get_principal performs a real DB membership lookup ─────────────────


@pytest.mark.integration
async def test_wrong_tenant_returns_403(
    client: AsyncClient, make_token: Any, admin_conn: Any
) -> None:
    """Valid JWT + X-Tenant-Id for a tenant the user isn't in → 403 from get_principal."""
    user = await seed_user(admin_conn, workos_id=_WORKOS_ID, email=_EMAIL)
    tenant_a = await seed_tenant(admin_conn)
    tenant_b = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant_a["id"]))
    # No membership for tenant_b

    token = make_token()
    r = await client.get("/api/v1/invitations", headers=_headers(token, str(tenant_b["id"])))

    assert r.status_code == 403, r.text

    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant_a["id"])
    await admin_conn.execute(
        "DELETE FROM tenants WHERE id IN ($1, $2)", tenant_a["id"], tenant_b["id"]
    )
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest.mark.integration
async def test_missing_tenant_header_returns_400(
    client: AsyncClient, make_token: Any, admin_conn: Any
) -> None:
    """Valid JWT but no X-Tenant-Id header on a principal-required route → 400."""
    user = await seed_user(admin_conn, workos_id=_WORKOS_ID, email=_EMAIL)
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]))

    token = make_token()
    r = await client.get("/api/v1/invitations", headers=_headers(token))

    assert r.status_code == 400

    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


# ── Gap 3: RBAC role guard fires through the real JWT → get_principal path ────


@pytest.mark.integration
async def test_owner_jwt_gets_200_on_owner_only_endpoint(
    client: AsyncClient, make_token: Any, admin_conn: Any
) -> None:
    """Owner role resolved from DB → invitation list returns 200."""
    user = await seed_user(admin_conn, workos_id=_WORKOS_ID, email=_EMAIL)
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]), role="owner")

    token = make_token()
    r = await client.get("/api/v1/invitations", headers=_headers(token, str(tenant["id"])))

    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)

    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest.mark.integration
async def test_manager_jwt_gets_403_on_owner_only_endpoint(
    client: AsyncClient, make_token: Any, admin_conn: Any
) -> None:
    """Manager role resolved from DB → owner-only guard returns 403."""
    user = await seed_user(admin_conn, workos_id=_WORKOS_ID, email=_EMAIL)
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]), role="manager")

    token = make_token()
    r = await client.get("/api/v1/invitations", headers=_headers(token, str(tenant["id"])))

    assert r.status_code == 403, r.text
    assert "owner" in r.json()["detail"].lower()

    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest.mark.integration
async def test_staff_jwt_gets_403_on_owner_only_endpoint(
    client: AsyncClient, make_token: Any, admin_conn: Any
) -> None:
    """Staff role resolved from DB → owner-only guard returns 403."""
    user = await seed_user(admin_conn, workos_id=_WORKOS_ID, email=_EMAIL)
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]), role="staff")

    token = make_token()
    r = await client.get("/api/v1/invitations", headers=_headers(token, str(tenant["id"])))

    assert r.status_code == 403, r.text

    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest.mark.integration
async def test_owner_sees_only_own_tenant_invitations(
    client: AsyncClient, make_token: Any, admin_conn: Any
) -> None:
    """Owner for tenant_a cannot see tenant_b invitations — RLS + explicit filter."""
    user = await seed_user(admin_conn, workos_id=_WORKOS_ID, email=_EMAIL)
    tenant_a = await seed_tenant(admin_conn)
    tenant_b = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant_a["id"]), role="owner")

    # Seed an invitation under tenant_b only
    inv_token = secrets.token_hex(32)
    await admin_conn.execute(
        "INSERT INTO invitations (tenant_id, email, role, token)"
        " VALUES ($1, 'other@example.com', 'staff', $2)",
        tenant_b["id"],
        inv_token,
    )

    token = make_token()
    r = await client.get("/api/v1/invitations", headers=_headers(token, str(tenant_a["id"])))

    assert r.status_code == 200, r.text
    # tenant_a has no invitations — tenant_b's must not bleed through
    emails = [i["email"] for i in r.json()]
    assert "other@example.com" not in emails

    await admin_conn.execute("DELETE FROM invitations WHERE token = $1", inv_token)
    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant_a["id"])
    await admin_conn.execute(
        "DELETE FROM tenants WHERE id IN ($1, $2)", tenant_a["id"], tenant_b["id"]
    )
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])
