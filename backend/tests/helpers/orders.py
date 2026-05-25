"""Helpers for Phase 3+ tests: orders and sale_line_items row factories."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

from uuid6 import uuid7


def make_order_row(overrides: dict | None = None) -> dict:
    """Build a valid orders row with sensible defaults.

    Defaults to a completed Clover order (state=locked, payment_state=PAID).
    """
    defaults: dict[str, Any] = {
        "id": str(uuid7()),
        "tenant_id": None,  # Must be set by test
        "pos_event_inbox_id": None,  # Must be set by test
        "clover_order_id": f"CO-{uuid.uuid4().hex[:12]}",
        "total_amount_cents": 4200,
        "state": "locked",
        "payment_state": "PAID",
        "payment_state_previous": None,
        "channel_label": "unknown",
        "channel_category": "unknown",
        "opened_at": None,
        "closed_at": datetime.now(UTC),
        "processed_at": datetime.now(UTC),
        "clover_employee_id": None,
    }
    if overrides:
        defaults.update(overrides)
    return defaults


def make_sli_row(overrides: dict | None = None) -> dict:
    """Build a valid sale_line_items row with sensible defaults.

    Defaults to a non-refunded, non-voided line item.
    """
    defaults: dict[str, Any] = {
        "id": str(uuid7()),
        "tenant_id": None,  # Must be set by test
        "order_id": None,  # Must be set by test
        "clover_line_item_id": f"CLI-{uuid.uuid4().hex[:12]}",
        "menu_item_id": None,  # NULL = unmapped (Sprint 4 default)
        "name_at_sale": "Default Item",
        "quantity": 1,
        "price_cents_at_sale": 1500,
        "discount_amount_cents": 0,
        "net_revenue_cents": 1500,
        "is_refunded": False,
        "refunded_quantity": 0,
        "refunded_amount_cents": 0,
        "is_voided": False,
    }
    if overrides:
        defaults.update(overrides)
    return defaults


async def insert_order(conn: Any, row: dict) -> None:
    """Insert into orders table. Must be called inside SET LOCAL context."""
    await conn.execute(
        """
        INSERT INTO orders (
            id, tenant_id, pos_event_inbox_id, clover_order_id,
            total_amount_cents, state, payment_state, payment_state_previous,
            channel_label, channel_category,
            opened_at, closed_at, processed_at, clover_employee_id
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
    """,
        row["id"],
        row["tenant_id"],
        row["pos_event_inbox_id"],
        row["clover_order_id"],
        row["total_amount_cents"],
        row["state"],
        row["payment_state"],
        row.get("payment_state_previous"),
        row.get("channel_label", "unknown"),
        row.get("channel_category", "unknown"),
        row.get("opened_at"),
        row.get("closed_at"),
        row["processed_at"],
        row.get("clover_employee_id"),
    )


async def insert_sli(conn: Any, row: dict) -> None:
    """Insert into sale_line_items table. Must be called inside SET LOCAL context."""
    await conn.execute(
        """
        INSERT INTO sale_line_items (
            id, tenant_id, order_id, clover_line_item_id,
            menu_item_id, name_at_sale, quantity,
            price_cents_at_sale, discount_amount_cents, net_revenue_cents,
            is_refunded, refunded_quantity, refunded_amount_cents, is_voided
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
    """,
        row["id"],
        row["tenant_id"],
        row["order_id"],
        row["clover_line_item_id"],
        row.get("menu_item_id"),
        row["name_at_sale"],
        row["quantity"],
        row["price_cents_at_sale"],
        row.get("discount_amount_cents", 0),
        row["net_revenue_cents"],
        row.get("is_refunded", False),
        row.get("refunded_quantity", 0),
        row.get("refunded_amount_cents", 0),
        row.get("is_voided", False),
    )


async def seed_inbox_and_order(admin_conn: Any, tenant_id: str) -> dict:
    """Create a pos_event_inbox row + orders row via superuser. Returns IDs."""
    inbox_id = str(uuid7())
    await admin_conn.execute(
        """
        INSERT INTO pos_event_inbox (
            inbox_id, tenant_id, vendor, vendor_event_id, vendor_object_type,
            vendor_event_type, vendor_ts, raw_payload, signature_verified, source
        ) VALUES ($1,$2,'clover',$3,'O','UPDATE',$4,'{}',false,'webhook')
    """,
        inbox_id,
        tenant_id,
        f"O:{uuid.uuid4().hex[:16]}",
        int(time.time() * 1000),
    )
    order_id = str(uuid7())
    clover_id = f"clv_{uuid.uuid4().hex[:12]}"
    await admin_conn.execute(
        """
        INSERT INTO orders (
            id, tenant_id, pos_event_inbox_id, clover_order_id,
            total_amount_cents, state, payment_state, processed_at
        ) VALUES ($1,$2,$3,$4,1000,'locked','PAID',now())
    """,
        order_id,
        tenant_id,
        inbox_id,
        clover_id,
    )
    return {"inbox_id": inbox_id, "order_id": order_id, "clover_order_id": clover_id}
