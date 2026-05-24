"""Helpers for Phase 7 inbox worker tests."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from uuid6 import uuid7

from app.core.encryption import TokenEncryption

enc = TokenEncryption()


def make_clover_order(
    order_id: str | None = None,
    state: str = "locked",
    total: int = 4200,
    payment_state: str | None = None,
    line_items: list[dict] | None = None,
    employee_id: str | None = None,
) -> dict[str, Any]:
    """Build a realistic Clover order response."""
    oid = order_id or f"order_{uuid.uuid4().hex[:8]}"
    now_ms = int(time.time() * 1000)

    if line_items is None:
        line_items = [{
            "id": f"li_{uuid.uuid4().hex[:8]}",
            "name": "Default Item",
            "price": 1500,
            "unitQty": 1,
        }]

    order: dict[str, Any] = {
        "id": oid,
        "state": state,
        "total": total,
        "createdTime": now_ms - 60000,
        "modifiedTime": now_ms,
        "lineItems": {"elements": line_items},
    }

    if payment_state:
        order["paymentState"] = payment_state

    if employee_id:
        order["employee"] = {"id": employee_id}

    return order


def make_line_item(
    item_id: str | None = None,
    name: str = "Test Item",
    price: int = 1500,
    qty: int = 1,
    discount: int = 0,
) -> dict[str, Any]:
    # None → auto-generate; empty string → preserve as empty (tests skip logic)
    generated_id = f"li_{uuid.uuid4().hex[:8]}" if item_id is None else item_id
    return {
        "id": generated_id,
        "name": name,
        "price": price,
        "unitQty": qty,
        "discountAmount": discount,
    }


async def seed_worker_prereqs(admin_conn: Any) -> dict[str, str]:
    """Create tenant + user + user_tenants + active connection + inbox event."""
    tid = str(uuid.uuid4())
    uid = str(uuid.uuid4())
    ut_id = str(uuid7())
    mid = f"wk_merch_{uuid.uuid4().hex[:8]}"
    cid = str(uuid7())
    inbox_id = str(uuid7())
    vendor_event_id = f"order_{uuid.uuid4().hex[:8]}"

    await admin_conn.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tid, f"WorkerTest_{tid[:8]}", f"wk-{tid[:8]}",
    )
    await admin_conn.execute(
        "INSERT INTO users (id, workos_id, email) VALUES ($1, $2, $3)",
        uid, f"workos_{uid[:8]}", f"wk-{uid[:8]}@test.com",
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
    """, cid, tid, mid, enc.encrypt("worker_access"), enc.encrypt("worker_refresh"))

    await admin_conn.execute("""
        INSERT INTO pos_event_inbox (
            inbox_id, tenant_id, connection_id, vendor,
            vendor_event_id, vendor_object_type, vendor_event_type,
            vendor_ts, raw_payload, signature_verified, source, state
        ) VALUES ($1,$2,$3,'clover',$4,'O','UPDATE',$5,
                  $6, true, 'webhook', 'pending')
    """, inbox_id, tid, cid, vendor_event_id,
        int(time.time() * 1000), json.dumps({"source": "test"}))

    return {
        "tenant_id": tid,
        "merchant_id": mid,
        "connection_id": cid,
        "inbox_id": inbox_id,
        "vendor_event_id": vendor_event_id,
    }


async def seed_inbox_event(
    admin_conn: Any,
    seed: dict[str, str],
    **overrides: Any,
) -> dict[str, str]:
    """Insert an additional inbox event for an existing seed."""
    inbox_id = str(uuid7())
    vendor_event_id = overrides.get("vendor_event_id", f"order_{uuid.uuid4().hex[:8]}")
    vendor_event_type = overrides.get("vendor_event_type", "UPDATE")
    vendor_ts = overrides.get("vendor_ts", int(time.time() * 1000))
    state = overrides.get("state", "pending")
    fetched_payload = overrides.get("fetched_payload", None)

    await admin_conn.execute("""
        INSERT INTO pos_event_inbox (
            inbox_id, tenant_id, connection_id, vendor,
            vendor_event_id, vendor_object_type, vendor_event_type,
            vendor_ts, raw_payload, fetched_payload, fetched_at,
            signature_verified, source, state
        ) VALUES ($1,$2,$3,'clover',$4,'O',$5,$6,$7,$8,
                  CASE WHEN $8::jsonb IS NOT NULL THEN now() ELSE NULL END,
                  true, 'webhook', $9)
    """, inbox_id, seed["tenant_id"], seed["connection_id"],
        vendor_event_id, vendor_event_type, vendor_ts,
        json.dumps({"source": "test"}),
        json.dumps(fetched_payload) if fetched_payload else None,
        state)

    return {
        "inbox_id": inbox_id,
        "vendor_event_id": str(vendor_event_id),
    }
