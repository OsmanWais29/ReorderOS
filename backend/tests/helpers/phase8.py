"""Helpers for Phase 8 reconciliation tests."""

from __future__ import annotations

import time
import uuid
from typing import Any

from uuid6 import uuid7

from app.core.encryption import TokenEncryption

enc = TokenEncryption()


def make_clover_order_list_item(
    order_id: str | None = None,
    state: str = "locked",
    modified_time: int | None = None,
    total: int = 4200,
) -> dict[str, Any]:
    """Build a single order as returned in Clover's list_orders response."""
    now_ms = int(time.time() * 1000)
    return {
        "id": order_id or f"recon_{uuid.uuid4().hex[:8]}",
        "state": state,
        "total": total,
        "modifiedTime": modified_time if modified_time is not None else now_ms,
        "createdTime": now_ms - 60000,
        "lineItems": {"elements": [{
            "id": f"li_{uuid.uuid4().hex[:8]}",
            "name": "Recon Item",
            "price": 1500,
            "unitQty": 1,
        }]},
    }


async def seed_reconciliation_prereqs(
    admin_conn: Any,
    **kwargs: Any,
) -> dict[str, str]:
    """Create tenant + user + user_tenants + active POS connection."""
    tid = str(uuid.uuid4())
    uid = str(uuid.uuid4())
    ut_id = str(uuid7())
    mid = kwargs.get("merchant_id", f"recon_merch_{uuid.uuid4().hex[:8]}")
    cid = str(uuid7())
    cursor = kwargs.get("last_reconciliation_cursor", None)

    await admin_conn.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tid, f"ReconTest_{tid[:8]}", f"recon-{tid[:8]}",
    )
    await admin_conn.execute(
        "INSERT INTO users (id, workos_id, email) VALUES ($1, $2, $3)",
        uid, f"workos_{uid[:8]}", f"recon-{uid[:8]}@test.com",
    )
    await admin_conn.execute("""
        INSERT INTO user_tenants (id, user_id, tenant_id, role, active)
        VALUES ($1, $2, $3, 'owner', true)
    """, ut_id, uid, tid)
    await admin_conn.execute("""
        INSERT INTO tenant_pos_connections (
            connection_id, tenant_id, vendor, merchant_id, environment,
            access_token_enc, access_token_expires_at,
            refresh_token_enc, refresh_token_expires_at,
            state, last_reconciliation_cursor
        ) VALUES ($1,$2,'clover',$3,'sandbox',
                  $4, now()+interval'1 hour',
                  $5, now()+interval'30 days',
                  'active', $6)
    """, cid, tid, mid,
        enc.encrypt("recon_access"), enc.encrypt("recon_refresh"),
        cursor)

    return {
        "tenant_id": tid,
        "merchant_id": mid,
        "connection_id": cid,
    }
