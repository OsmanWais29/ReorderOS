"""Helpers for Phase 5 tests: tenant/user/connection seeding and token factory."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from uuid6 import uuid7

from app.core.encryption import TokenEncryption

enc = TokenEncryption()

FUTURE_1H = datetime.now(UTC) + timedelta(hours=1)
FUTURE_30D = datetime.now(UTC) + timedelta(days=30)
NEAR_EXPIRY = datetime.now(UTC) + timedelta(minutes=5)


async def seed_tenant_and_user(admin_conn: Any, role: str = "owner") -> dict:
    """Create tenant + user + user_tenants row.

    Matches the actual schema:
      users:       id, workos_id, email, email_verified
      user_tenants: id, user_id, tenant_id, role, active
    """
    tid = str(uuid.uuid4())
    uid = str(uuid.uuid4())
    workos_id = f"user_p5_{uid[:16]}"
    ut_id = str(uuid7())

    await admin_conn.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tid,
        f"Phase5_{tid[:8]}",
        f"p5-{tid[:8]}",
    )
    await admin_conn.execute(
        "INSERT INTO users (id, workos_id, email, email_verified) VALUES ($1, $2, $3, true)",
        uid,
        workos_id,
        f"user-{uid[:8]}@test.com",
    )
    await admin_conn.execute(
        "INSERT INTO user_tenants (id, user_id, tenant_id, role, active) VALUES ($1, $2, $3, $4, true)",
        ut_id,
        uid,
        tid,
        role,
    )
    return {"tenant_id": tid, "user_id": uid, "role": role}


async def seed_tenant_user_and_connection(
    admin_conn: Any,
    role: str = "owner",
    **kwargs: Any,
) -> dict:
    """Create tenant + user + active POS connection with real encrypted tokens."""
    seed = await seed_tenant_and_user(admin_conn, role=role)
    mid = kwargs.get("merchant_id", f"merch_{uuid.uuid4().hex[:8]}")
    cid = str(uuid7())
    access_exp = kwargs.get("access_token_expires_at", FUTURE_1H)
    refresh_exp = kwargs.get("refresh_token_expires_at", FUTURE_30D)
    state = kwargs.get("state", "active")
    failure_count = kwargs.get("refresh_failure_count", 0)

    await admin_conn.execute(
        """
        INSERT INTO tenant_pos_connections (
            connection_id, tenant_id, vendor, merchant_id, environment,
            access_token_enc, access_token_expires_at,
            refresh_token_enc, refresh_token_expires_at,
            prev_refresh_token_enc, refresh_failure_count, state
        ) VALUES ($1,$2,'clover',$3,'sandbox',
                  $4,$5,$6,$7,$8,$9,$10)
    """,
        cid,
        seed["tenant_id"],
        mid,
        enc.encrypt("original_access"),
        access_exp,
        enc.encrypt("original_refresh"),
        refresh_exp,
        None,
        failure_count,
        state,
    )
    return {**seed, "connection_id": cid, "merchant_id": mid}


def make_clover_token_response(
    access_token: str = "new_access_tok",
    refresh_token: str = "new_refresh_tok",
) -> dict:
    """Build a Clover-format token exchange response.

    Uses access_token_expiration (Unix timestamp in seconds), NOT expires_in.
    Source: https://docs.clover.com/dev/docs/generate-expiring-tokens-using-v2-oauth-flow
    """
    now_unix = int(datetime.now(UTC).timestamp())
    return {
        "access_token": access_token,
        "access_token_expiration": now_unix + 1800,  # 30 min
        "refresh_token": refresh_token,
        "refresh_token_expiration": now_unix + 2592000,  # 30 days
    }
