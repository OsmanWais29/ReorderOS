"""Sprint 4 — Phase 5: OAuth flow + state manager + token refresh (22 tests).

Mocking: respx intercepts httpx calls. DB, encryption, RLS are real.
Settings: pydantic-settings uses lowercase attrs (clover_api_base_url etc).
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from uuid6 import uuid7

from app.core.config import settings
from app.main import app
from app.modules.pos.state_manager import OAuthStateManager
from app.modules.pos.token_refresh import refresh_expiring_tokens
from tests.helpers.phase5 import (
    FUTURE_30D,
    NEAR_EXPIRY,
    enc,
    make_clover_token_response,
    seed_tenant_and_user,
    seed_tenant_user_and_connection,
)

# ── Sync TestClient fixture (shadows the async one from conftest for this file) ─


@pytest.fixture
def client():
    """Sync TestClient — calls async routes synchronously via anyio."""
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── Test 1: State Manager — Create and Consume ────────────────────────────────


@pytest.mark.asyncio
async def test_state_create_and_consume(admin_conn):
    """generate() creates a token; validate_and_consume() returns correct IDs;
    second consume raises 400 (single-use).
    """
    seed = await seed_tenant_and_user(admin_conn)
    state_mgr = OAuthStateManager()

    token = await state_mgr.generate(seed["tenant_id"], seed["user_id"])
    assert isinstance(token, str) and len(token) > 20

    tid, uid = await state_mgr.validate_and_consume(token)
    assert tid == seed["tenant_id"]
    assert uid == seed["user_id"]

    with pytest.raises(Exception) as exc:
        await state_mgr.validate_and_consume(token)
    msg = str(exc.value).lower()
    assert "invalid" in msg or "expired" in msg or "not found" in msg


# ── Test 2: State Manager — Expired Token Rejected ───────────────────────────


@pytest.mark.asyncio
async def test_state_expired_rejected(admin_conn):
    seed = await seed_tenant_and_user(admin_conn)
    state_mgr = OAuthStateManager()

    token = await state_mgr.generate(seed["tenant_id"], seed["user_id"])
    await admin_conn.execute(
        "UPDATE oauth_states SET expires_at = $1 WHERE state = $2",
        datetime.now(UTC) - timedelta(minutes=1),
        token,
    )

    with pytest.raises(Exception):
        await state_mgr.validate_and_consume(token)


# ── Test 3: State Manager — Unknown Token Rejected ────────────────────────────


@pytest.mark.asyncio
async def test_state_unknown_rejected():
    state_mgr = OAuthStateManager()
    with pytest.raises(Exception):
        await state_mgr.validate_and_consume(str(uuid.uuid4()))


# ── Test 4: Connect — Manager Gets 403 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_requires_owner(client, admin_conn):
    """Manager role must be blocked (403) from initiating OAuth."""
    seed = await seed_tenant_and_user(admin_conn, role="manager")

    from app.modules.pos.router import get_current_user

    async def mock_manager():
        return {"tenant_id": seed["tenant_id"], "user_id": seed["user_id"], "role": "manager"}

    client.app.dependency_overrides[get_current_user] = mock_manager
    response = client.get("/api/v1/pos/clover/connect", follow_redirects=False)
    assert response.status_code == 403


# ── Test 5: Connect — Owner Gets Redirect with State ─────────────────────────


@pytest.mark.asyncio
async def test_connect_owner_redirects_with_state(client, admin_conn):
    """Owner gets 302 to Clover. State token must be stored in oauth_states."""
    seed = await seed_tenant_and_user(admin_conn)

    from app.modules.pos.router import get_current_user

    async def mock_owner():
        return {"tenant_id": seed["tenant_id"], "user_id": seed["user_id"], "role": "owner"}

    client.app.dependency_overrides[get_current_user] = mock_owner
    response = client.get("/api/v1/pos/clover/connect", follow_redirects=False)
    assert response.status_code in (302, 307)

    location = response.headers["location"]
    assert "clover.com" in location
    # v2/OAuth flow: authorize path MUST be /oauth/v2/authorize to match the /oauth/v2/token
    # exchange in the callback. Legacy /oauth/authorize pairs with a different (legacy) token
    # flow and returns to the callback without a v2-usable code (docs-confirmed 2026-06).
    assert "/oauth/v2/authorize" in location
    assert "/oauth/authorize?" not in location  # not the legacy path
    assert f"client_id={settings.clover_app_id}" in location
    assert "state=" in location

    state_param = location.split("state=")[1].split("&")[0]
    count = await admin_conn.fetchval(
        "SELECT count(*) FROM oauth_states WHERE state = $1",
        state_param,
    )
    assert count == 1


# ── Test 6: Callback — Happy Path ────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_callback_success(admin_conn, client):
    """Full callback: state validated, code exchanged, tokens stored encrypted."""
    seed = await seed_tenant_and_user(admin_conn)
    state_token = str(uuid.uuid4())
    await admin_conn.execute(
        """
        INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
        VALUES ($1, $2, $3, $4)
    """,
        state_token,
        seed["tenant_id"],
        seed["user_id"],
        datetime.now(UTC) + timedelta(minutes=10),
    )

    clover_response = make_clover_token_response("callback_access", "callback_refresh")
    respx.post(f"{settings.clover_api_base_url}/oauth/v2/token").mock(
        return_value=httpx.Response(200, json=clover_response)
    )

    merch_id = f"merch_cb_{uuid.uuid4().hex[:8]}"
    response = client.get(
        f"/api/v1/pos/clover/callback"
        f"?code=test_code&state={state_token}"
        f"&merchant_id={merch_id}"
        f"&client_id={settings.clover_app_id}",
        follow_redirects=False,
    )
    assert response.status_code in (302, 307)

    row = await admin_conn.fetchrow(
        "SELECT access_token_enc, refresh_token_enc, state, environment "
        "FROM tenant_pos_connections WHERE tenant_id = $1 AND vendor = 'clover'",
        seed["tenant_id"],
    )
    assert row is not None
    assert row["state"] == "active"
    assert row["environment"] == settings.clover_environment
    assert enc.decrypt(row["access_token_enc"]) == "callback_access"
    assert enc.decrypt(row["refresh_token_enc"]) == "callback_refresh"

    count = await admin_conn.fetchval(
        "SELECT count(*) FROM oauth_states WHERE state = $1",
        state_token,
    )
    assert count == 0


# ── Test 7: Callback — Invalid State Returns 400 ─────────────────────────────


@pytest.mark.asyncio
async def test_callback_invalid_state(client):
    response = client.get(
        "/api/v1/pos/clover/callback?code=x&state=nonexistent&merchant_id=x",
    )
    assert response.status_code == 400


# ── Test 8: Callback — Expired State Returns 400 ─────────────────────────────


@pytest.mark.asyncio
async def test_callback_expired_state(admin_conn, client):
    seed = await seed_tenant_and_user(admin_conn)
    state_token = str(uuid.uuid4())
    await admin_conn.execute(
        """
        INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
        VALUES ($1, $2, $3, $4)
    """,
        state_token,
        seed["tenant_id"],
        seed["user_id"],
        datetime.now(UTC) - timedelta(minutes=1),
    )

    response = client.get(
        f"/api/v1/pos/clover/callback?code=x&state={state_token}&merchant_id=x",
    )
    assert response.status_code == 400


# ── Test 9: Callback — client_id Mismatch Returns 400 ────────────────────────


@pytest.mark.asyncio
async def test_callback_client_id_mismatch(admin_conn, client):
    seed = await seed_tenant_and_user(admin_conn)
    state_token = str(uuid.uuid4())
    await admin_conn.execute(
        """
        INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
        VALUES ($1, $2, $3, $4)
    """,
        state_token,
        seed["tenant_id"],
        seed["user_id"],
        datetime.now(UTC) + timedelta(minutes=10),
    )

    response = client.get(
        f"/api/v1/pos/clover/callback"
        f"?code=test&state={state_token}&merchant_id=MERCH&client_id=WRONG_APP_ID",
    )
    assert response.status_code == 400
    assert "client_id" in response.text.lower() or "mismatch" in response.text.lower()


# ── Test 10: Callback — Reconnect Updates Existing ───────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_callback_reconnect_updates_existing(admin_conn, client):
    """ON CONFLICT (tenant_id, vendor) WHERE state='active' updates row in place.

    Uses random merchant IDs to avoid the cross-tenant uniqueness index
    (tenant_pos_one_tenant_per_merchant) conflicting with data from other tests.
    """
    old_mid = f"old_merch_{uuid.uuid4().hex[:8]}"
    new_mid = f"new_merch_{uuid.uuid4().hex[:8]}"
    seed = await seed_tenant_user_and_connection(admin_conn, merchant_id=old_mid)

    state_token = str(uuid.uuid4())
    await admin_conn.execute(
        """
        INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
        VALUES ($1, $2, $3, $4)
    """,
        state_token,
        seed["tenant_id"],
        seed["user_id"],
        datetime.now(UTC) + timedelta(minutes=10),
    )

    respx.post(f"{settings.clover_api_base_url}/oauth/v2/token").mock(
        return_value=httpx.Response(
            200, json=make_clover_token_response("reconnect_access", "reconnect_refresh")
        )
    )

    response = client.get(
        f"/api/v1/pos/clover/callback"
        f"?code=recon_code&state={state_token}"
        f"&merchant_id={new_mid}&client_id={settings.clover_app_id}",
        follow_redirects=False,
    )
    assert response.status_code in (302, 307)

    rows = await admin_conn.fetch(
        "SELECT access_token_enc, merchant_id FROM tenant_pos_connections "
        "WHERE tenant_id = $1 AND vendor = 'clover' AND state = 'active'",
        seed["tenant_id"],
    )
    assert len(rows) == 1
    assert enc.decrypt(rows[0]["access_token_enc"]) == "reconnect_access"
    assert rows[0]["merchant_id"] == new_mid


# ── Test 11: Disconnect — Sets State to Revoked ───────────────────────────────


@pytest.mark.asyncio
async def test_disconnect_revokes_connection(admin_conn, client):
    seed = await seed_tenant_user_and_connection(admin_conn)

    from app.modules.pos.router import get_current_user

    async def mock_owner():
        return {"tenant_id": seed["tenant_id"], "user_id": seed["user_id"], "role": "owner"}

    client.app.dependency_overrides[get_current_user] = mock_owner
    response = client.post("/api/v1/pos/clover/disconnect")
    assert response.status_code in (200, 204)

    state = await admin_conn.fetchval(
        "SELECT state FROM tenant_pos_connections WHERE connection_id = $1",
        seed["connection_id"],
    )
    assert state == "revoked"


# ── Test 12: Disconnect — Manager Gets 403 ───────────────────────────────────


@pytest.mark.asyncio
async def test_disconnect_manager_forbidden(admin_conn, client):
    seed = await seed_tenant_user_and_connection(admin_conn, role="manager")

    from app.modules.pos.router import get_current_user

    async def mock_manager():
        return {"tenant_id": seed["tenant_id"], "user_id": seed["user_id"], "role": "manager"}

    client.app.dependency_overrides[get_current_user] = mock_manager
    response = client.post("/api/v1/pos/clover/disconnect")
    assert response.status_code == 403


# ── Test 13: Token Refresh — Happy Path ──────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_token_refresh_success(admin_conn):
    """Near-expiry token refreshed; new tokens stored; prev_refresh populated; count reset."""
    seed = await seed_tenant_user_and_connection(
        admin_conn,
        access_token_expires_at=NEAR_EXPIRY,
        refresh_token_expires_at=FUTURE_30D,
    )

    respx.post(f"{settings.clover_api_base_url}/oauth/v2/refresh").mock(
        return_value=httpx.Response(
            200, json=make_clover_token_response("refreshed_access", "refreshed_refresh")
        )
    )

    await refresh_expiring_tokens()

    row = await admin_conn.fetchrow(
        "SELECT access_token_enc, refresh_token_enc, "
        "prev_refresh_token_enc, refresh_failure_count "
        "FROM tenant_pos_connections WHERE connection_id = $1",
        seed["connection_id"],
    )
    assert enc.decrypt(row["access_token_enc"]) == "refreshed_access"
    assert enc.decrypt(row["refresh_token_enc"]) == "refreshed_refresh"
    assert enc.decrypt(row["prev_refresh_token_enc"]) == "original_refresh"
    assert row["refresh_failure_count"] == 0


# ── Test 14: Token Refresh — 3 Failures → Error ──────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_token_refresh_three_failures_set_error(admin_conn):
    """count=2 + one failure → state='error', count=3, state_reason set."""
    seed = await seed_tenant_user_and_connection(
        admin_conn,
        access_token_expires_at=NEAR_EXPIRY,
        refresh_token_expires_at=FUTURE_30D,
        refresh_failure_count=2,
    )

    respx.post(f"{settings.clover_api_base_url}/oauth/v2/refresh").mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )

    await refresh_expiring_tokens()

    row = await admin_conn.fetchrow(
        "SELECT state, refresh_failure_count, state_reason "
        "FROM tenant_pos_connections WHERE connection_id = $1",
        seed["connection_id"],
    )
    assert row["state"] == "error"
    assert row["refresh_failure_count"] == 3
    assert row["state_reason"] is not None


# ── Test 15: Token Refresh — First Failure Stays Active ──────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_token_refresh_failure_increments(admin_conn):
    """First failure: count 0→1, state stays 'active'."""
    seed = await seed_tenant_user_and_connection(
        admin_conn,
        access_token_expires_at=NEAR_EXPIRY,
        refresh_token_expires_at=FUTURE_30D,
        refresh_failure_count=0,
    )

    respx.post(f"{settings.clover_api_base_url}/oauth/v2/refresh").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    await refresh_expiring_tokens()

    row = await admin_conn.fetchrow(
        "SELECT state, refresh_failure_count FROM tenant_pos_connections WHERE connection_id = $1",
        seed["connection_id"],
    )
    assert row["state"] == "active"
    assert row["refresh_failure_count"] == 1


# ── Test 16: Token Refresh — Skips Healthy Tokens ────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_token_refresh_skips_healthy_tokens(admin_conn):
    """Token expiring in 1 hour (> 10 min threshold) must not be refreshed.

    Uses a catch-all mock (returning error) instead of AssertionError so that
    NEAR_EXPIRY connections left by earlier tests in this session don't cause
    false failures. We assert our specific connection's token is unchanged —
    that is the invariant, not whether the API was called at all.
    """
    seed = await seed_tenant_user_and_connection(admin_conn)
    # Default FUTURE_1H — beyond the 10-minute pickup window

    # Allow the job to process any leftover NEAR_EXPIRY rows from other tests;
    # those calls get a 500 (failure path) and don't affect our FUTURE_1H row.
    respx.post(url__regex=".*/oauth/v2/refresh.*").mock(
        return_value=httpx.Response(500, text="error")
    )

    await refresh_expiring_tokens()

    row = await admin_conn.fetchval(
        "SELECT access_token_enc FROM tenant_pos_connections WHERE connection_id = $1",
        seed["connection_id"],
    )
    assert enc.decrypt(row) == "original_access"


# ── Test 17: Token Refresh — Cleans Expired OAuth States ─────────────────────


@pytest.mark.asyncio
async def test_token_refresh_cleans_expired_states(admin_conn):
    """refresh job deletes expired oauth_states; valid states preserved."""
    seed = await seed_tenant_and_user(admin_conn)

    expired_state = str(uuid.uuid4())
    valid_state = str(uuid.uuid4())
    await admin_conn.execute(
        """
        INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
        VALUES ($1, $2, $3, $4)
    """,
        expired_state,
        seed["tenant_id"],
        seed["user_id"],
        datetime.now(UTC) - timedelta(minutes=5),
    )
    await admin_conn.execute(
        """
        INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
        VALUES ($1, $2, $3, $4)
    """,
        valid_state,
        seed["tenant_id"],
        seed["user_id"],
        datetime.now(UTC) + timedelta(minutes=5),
    )

    await refresh_expiring_tokens()

    assert (
        await admin_conn.fetchval(
            "SELECT count(*) FROM oauth_states WHERE state = $1",
            expired_state,
        )
        == 0
    )
    assert (
        await admin_conn.fetchval(
            "SELECT count(*) FROM oauth_states WHERE state = $1",
            valid_state,
        )
        == 1
    )


# ── Test 18: State Replay Fails ───────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_callback_state_replay_fails(admin_conn, client):
    """Second use of same state token returns 400 (state already consumed)."""
    seed = await seed_tenant_and_user(admin_conn)
    state_token = str(uuid.uuid4())
    await admin_conn.execute(
        """
        INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
        VALUES ($1, $2, $3, $4)
    """,
        state_token,
        seed["tenant_id"],
        seed["user_id"],
        datetime.now(UTC) + timedelta(minutes=10),
    )

    respx.post(f"{settings.clover_api_base_url}/oauth/v2/token").mock(
        return_value=httpx.Response(200, json=make_clover_token_response())
    )

    replay_mid = f"replay_{uuid.uuid4().hex[:8]}"
    resp1 = client.get(
        f"/api/v1/pos/clover/callback?code=code1&state={state_token}&merchant_id={replay_mid}",
        follow_redirects=False,
    )
    assert resp1.status_code in (302, 307)

    resp2 = client.get(
        f"/api/v1/pos/clover/callback?code=code2&state={state_token}&merchant_id={replay_mid}",
    )
    assert resp2.status_code == 400


# ── Test 19: UUIDv7 for connection_id ────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_callback_uses_uuidv7_for_connection(admin_conn, client):
    """The connection_id written to tenant_pos_connections must be a UUIDv7."""
    seed = await seed_tenant_and_user(admin_conn)
    state_token = str(uuid.uuid4())
    await admin_conn.execute(
        """
        INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
        VALUES ($1, $2, $3, $4)
    """,
        state_token,
        seed["tenant_id"],
        seed["user_id"],
        datetime.now(UTC) + timedelta(minutes=10),
    )

    respx.post(f"{settings.clover_api_base_url}/oauth/v2/token").mock(
        return_value=httpx.Response(200, json=make_clover_token_response())
    )

    v7_mid = f"v7m_{uuid.uuid4().hex[:8]}"
    client.get(
        f"/api/v1/pos/clover/callback?code=v7test&state={state_token}&merchant_id={v7_mid}",
        follow_redirects=False,
    )

    cid = await admin_conn.fetchval(
        "SELECT connection_id::text FROM tenant_pos_connections WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert cid is not None
    assert cid[14] == "7"


# ── Test 20: Full Lifecycle ───────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_full_phase5_lifecycle(admin_conn, service_conn, app_conn, client):
    """OAuth → lookup → inbox → order: full Sprint 4 chain."""
    seed = await seed_tenant_and_user(admin_conn)
    mid = f"lifecycle_{uuid.uuid4().hex[:8]}"

    state_token = str(uuid.uuid4())
    await admin_conn.execute(
        """
        INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
        VALUES ($1, $2, $3, $4)
    """,
        state_token,
        seed["tenant_id"],
        seed["user_id"],
        datetime.now(UTC) + timedelta(minutes=10),
    )

    respx.post(f"{settings.clover_api_base_url}/oauth/v2/token").mock(
        return_value=httpx.Response(
            200, json=make_clover_token_response("lifecycle_access", "lifecycle_refresh")
        )
    )

    resp = client.get(
        f"/api/v1/pos/clover/callback"
        f"?code=life_code&state={state_token}"
        f"&merchant_id={mid}&client_id={settings.clover_app_id}",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307)

    conn_row = await admin_conn.fetchrow(
        "SELECT connection_id, state, access_token_enc "
        "FROM tenant_pos_connections WHERE tenant_id = $1 AND merchant_id = $2",
        seed["tenant_id"],
        mid,
    )
    assert conn_row["state"] == "active"
    assert enc.decrypt(conn_row["access_token_enc"]) == "lifecycle_access"

    resolved = await service_conn.fetchval(
        "SELECT lookup_tenant_by_merchant('clover', $1)",
        mid,
    )
    assert str(resolved) == seed["tenant_id"]

    inbox_id = str(uuid7())
    await service_conn.execute(
        """
        INSERT INTO pos_event_inbox (
            inbox_id, tenant_id, connection_id, vendor,
            vendor_event_id, vendor_object_type, vendor_event_type,
            vendor_ts, raw_payload, signature_verified, source
        ) VALUES ($1,$2,$3,'clover',$4,'O','UPDATE',$5,'{}',true,'webhook')
    """,
        inbox_id,
        seed["tenant_id"],
        str(conn_row["connection_id"]),
        f"order_{uuid.uuid4().hex[:8]}",
        int(time.time() * 1000),
    )

    order_id = str(uuid7())
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await service_conn.execute(
            """
            INSERT INTO orders (
                id, tenant_id, pos_event_inbox_id, clover_order_id,
                total_amount_cents, state, payment_state, processed_at
            ) VALUES ($1,$2,$3,$4,4200,'locked','PAID',now())
        """,
            order_id,
            seed["tenant_id"],
            inbox_id,
            f"CO-{uuid.uuid4().hex[:8]}",
        )

    order = await admin_conn.fetchrow(
        "SELECT tenant_id, state FROM orders WHERE id = $1",
        order_id,
    )
    assert str(order["tenant_id"]) == seed["tenant_id"]
    assert order["state"] == "locked"

    async with app_conn.transaction():
        await app_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        rows = await app_conn.fetch("SELECT id FROM orders")
        assert any(str(r["id"]) == order_id for r in rows)

    assert (
        await admin_conn.fetchval(
            "SELECT count(*) FROM oauth_states WHERE state = $1",
            state_token,
        )
        == 0
    )


# ── Test 21: Connect — Staff (non-owner, non-manager) Gets 403 ───────────────


@pytest.mark.asyncio
async def test_connect_staff_forbidden(client, admin_conn):
    seed = await seed_tenant_and_user(admin_conn, role="staff")

    from app.modules.pos.router import get_current_user

    async def mock_staff():
        return {"tenant_id": seed["tenant_id"], "user_id": seed["user_id"], "role": "staff"}

    client.app.dependency_overrides[get_current_user] = mock_staff
    response = client.get("/api/v1/pos/clover/connect", follow_redirects=False)
    assert response.status_code == 403


# ── Test 22: Connect — Unauthenticated Gets 401 ───────────────────────────────


def test_connect_unauthenticated(client):
    """No auth credentials → 401 from get_identity (before role check)."""
    response = client.get("/api/v1/pos/clover/connect", follow_redirects=False)
    assert response.status_code in (401, 403)
