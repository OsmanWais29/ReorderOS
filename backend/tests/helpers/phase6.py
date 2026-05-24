"""Helpers for Phase 6 webhook tests."""

from __future__ import annotations

import time
import uuid
from typing import Any

from uuid6 import uuid7

from app.core.encryption import TokenEncryption

enc = TokenEncryption()


def make_webhook_payload(
    merchant_id: str,
    events: list[dict] | None = None,
    app_id: str = "TEST_APP_ID",
) -> dict:
    if events is None:
        events = [make_order_event()]
    return {"appId": app_id, "merchants": {merchant_id: events}}


def make_order_event(
    order_id: str | None = None,
    event_type: str = "UPDATE",
    ts: int | None = None,
) -> dict:
    return {
        "objectId": f"O:{order_id or f'order_{uuid.uuid4().hex[:8]}'}",
        "type": event_type,
        "ts": ts if ts is not None else int(time.time() * 1000),
    }


def make_non_order_event(prefix: str = "P", event_id: str | None = None) -> dict:
    return {
        "objectId": f"{prefix}:{event_id or uuid.uuid4().hex[:8]}",
        "type": "UPDATE",
        "ts": int(time.time() * 1000),
    }


async def seed_webhook_prereqs(admin_conn: Any) -> dict:
    """Create tenant + user + user_tenants + active POS connection.

    Schema-accurate (confirmed Phase 5):
    - users: (id, workos_id, email, email_verified) — email_verified NOT NULL, no display_name
    - user_tenants: column is 'active', no 'invited_at'
    """
    tid = str(uuid.uuid4())
    uid = str(uuid.uuid4())
    ut_id = str(uuid7())
    mid = f"wh_merch_{uuid.uuid4().hex[:8]}"
    cid = str(uuid7())

    await admin_conn.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tid, f"WebhookTest_{tid[:8]}", f"wh-{tid[:8]}",
    )
    await admin_conn.execute(
        "INSERT INTO users (id, workos_id, email, email_verified) VALUES ($1, $2, $3, true)",
        uid, f"workos_{uid[:8]}", f"wh-{uid[:8]}@test.com",
    )
    await admin_conn.execute(
        "INSERT INTO user_tenants (id, user_id, tenant_id, role, active) VALUES ($1, $2, $3, 'owner', true)",
        ut_id, uid, tid,
    )
    await admin_conn.execute("""
        INSERT INTO tenant_pos_connections (
            connection_id, tenant_id, vendor, merchant_id, environment,
            access_token_enc, access_token_expires_at,
            refresh_token_enc, refresh_token_expires_at, state
        ) VALUES ($1,$2,'clover',$3,'sandbox',
                  $4, now()+interval'1 hour',
                  $5, now()+interval'30 days', 'active')
    """, cid, tid, mid, enc.encrypt("wh_access"), enc.encrypt("wh_refresh"))

    return {"tenant_id": tid, "merchant_id": mid, "connection_id": cid}
