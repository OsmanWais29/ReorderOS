"""Inventory movement writer (Sprint 5 Phase 7 — Commit A relocation).

PURE REFACTOR: record_sale_inventory_effect and record_sale_reversal are moved here
verbatim from inventory/services.py with identical behavior — same formula, same
idempotency keys, same append-only ledger semantics. Commit B (Phase 9) replaces the
body with the new key format / yield_quantity formula / Mode A-B logic; until then this
is behaviorally identical to the old services.py code, just relocated under depletion/.

All functions accept an AsyncSession and never commit: the caller owns the transaction.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


async def record_sale_inventory_effect(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    sale_line_item_id: UUID,
    inventory_item_id: UUID,
    recorded_at: datetime | None = None,
) -> UUID | None:
    """Record the inventory movement caused by one sale line.  Idempotent.

    Formula (using frozen recipe_version_id from sale_line_items):
        theoretical_storage_qty =
            sale_line_items.quantity
            * recipe_ingredients.quantity
            / inventory_items.storage_to_recipe_factor

    Mode A → sale_depletion, delta = -theoretical_storage_qty
    Mode B → sale_signal,    delta = +theoretical_storage_qty

    The current yield factor is snapshotted in yield_factor_applied so that
    on_hand() replay remains deterministic even if inventory_yield_factors
    changes later.

    recorded_at: business event time from the source system (POS timestamp).
    When provided, the late-signal alert fires if the ingestion lag exceeds 30
    minutes and the event crosses a count reconciliation boundary.

    Idempotency key: ``sale_line:{sale_line_item_id}:{inventory_item_id}``
    Returns None if the movement already existed (replay).
    """
    idem_key = f"sale_line:{sale_line_item_id}:{inventory_item_id}"

    # ── idempotency check ─────────────────────────────────────────────────────
    existing = await session.execute(
        text(
            "SELECT id FROM inventory_movements WHERE tenant_id = :tid AND idempotency_key = :key"
        ),
        {"tid": tenant_id, "key": idem_key},
    )
    if existing.fetchone():
        return None

    # ── join sale → recipe → item ─────────────────────────────────────────────
    result = await session.execute(
        text("""
            SELECT s.quantity                     AS sale_qty,
                   ri.quantity                    AS recipe_qty,
                   ii.storage_to_recipe_factor    AS factor,
                   ii.inventory_mode              AS mode
              FROM sale_line_items    s
              JOIN recipe_ingredients ri
                ON ri.recipe_version_id = s.recipe_version_id
               AND ri.inventory_item_id = :iid
               AND ri.tenant_id         = :tid
              JOIN inventory_items     ii
                ON ii.id        = :iid
               AND ii.tenant_id = :tid
             WHERE s.id        = :slid
               AND s.tenant_id = :tid
        """),
        {"tid": tenant_id, "slid": sale_line_item_id, "iid": inventory_item_id},
    )
    row = result.fetchone()
    if not row:
        raise ValueError(
            f"sale_line {sale_line_item_id} or recipe ingredient for item "
            f"{inventory_item_id} not found (tenant {tenant_id})"
        )

    theoretical_qty = (
        Decimal(str(row.sale_qty)) * Decimal(str(row.recipe_qty)) / Decimal(str(row.factor))
    )

    # ── snapshot yield factor (identity 1.0 when no row exists) ──────────────
    yf_res = await session.execute(
        text("""
            SELECT yield_factor FROM inventory_yield_factors
             WHERE tenant_id = :tid AND inventory_item_id = :iid
        """),
        {"tid": tenant_id, "iid": inventory_item_id},
    )
    yf_row = yf_res.fetchone()
    yield_factor = Decimal(str(yf_row[0])) if yf_row else Decimal("1.0")

    if row.mode == "recipe_deducted":
        movement_type = "sale_depletion"
        delta = -theoretical_qty
    else:
        movement_type = "sale_signal"
        delta = theoretical_qty

    mv_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO inventory_movements
                (id, tenant_id, inventory_item_id, movement_type, delta,
                 source_type, source_id, idempotency_key,
                 yield_factor_applied, recorded_at)
            VALUES (:id, :tid, :iid, :mtype, :delta,
                    'sale_line_item', :slid, :key,
                    :yf, COALESCE(:rec_at, NOW()))
        """),
        {
            "id": mv_id,
            "tid": tenant_id,
            "iid": inventory_item_id,
            "mtype": movement_type,
            "delta": delta,
            "slid": sale_line_item_id,
            "key": idem_key,
            "yf": yield_factor,
            "rec_at": recorded_at,
        },
    )
    await session.flush()

    # ── late-signal detection ─────────────────────────────────────────────────
    if recorded_at is not None:
        now = datetime.now(UTC)
        gap_seconds = (now - recorded_at).total_seconds()
        if gap_seconds > 1800:
            boundary_res = await session.execute(
                text("""
                    SELECT 1 FROM inventory_count_events
                     WHERE tenant_id         = :tid
                       AND inventory_item_id = :iid
                       AND (
                           (reconciliation_cutoff_created_at IS NOT NULL
                            AND reconciliation_cutoff_created_at > :rec_at
                            AND reconciliation_cutoff_created_at <= :now)
                        OR (reconciliation_cutoff_created_at IS NULL
                            AND counted_at > :rec_at
                            AND counted_at <= :now)
                       )
                     LIMIT 1
                """),
                {
                    "tid": tenant_id,
                    "iid": inventory_item_id,
                    "rec_at": recorded_at,
                    "now": now,
                },
            )
            if boundary_res.fetchone():
                payload_str = json.dumps({
                    "inventory_item_id": str(inventory_item_id),
                    "movement_id": str(mv_id),
                    "recorded_at": recorded_at.isoformat(),
                    "created_at": now.isoformat(),
                    "gap_seconds": round(gap_seconds),
                })
                await session.execute(
                    text("""
                        INSERT INTO monitoring_alerts
                            (id, tenant_id, monitor_name, severity, trigger_payload)
                        VALUES (gen_random_uuid(), :tid,
                                'late_signal_reconciliation', 'warn',
                                CAST(:payload AS jsonb))
                        ON CONFLICT (tenant_id, monitor_name) WHERE resolved_at IS NULL
                        DO UPDATE SET
                            last_seen_at    = now(),
                            alert_count     = monitoring_alerts.alert_count + 1,
                            trigger_payload = EXCLUDED.trigger_payload
                    """),
                    {"tid": tenant_id, "payload": payload_str},
                )
                await session.flush()

    return mv_id


async def record_sale_reversal(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    original_movement_id: UUID,
    inventory_item_id: UUID,
) -> UUID | None:
    """Reverse a sale_depletion or sale_signal movement.  Idempotent.

    Emits sale_depletion_reversal or sale_signal_reversal with the exact
    arithmetic negation of the original delta.  The original row is never
    touched — append-only ledger semantics are preserved.

    Idempotency key: ``reversal:{original_movement_id}:{inventory_item_id}``
    Returns None if the reversal already existed (replay).
    """
    idem_key = f"reversal:{original_movement_id}:{inventory_item_id}"

    # ── idempotency check ─────────────────────────────────────────────────────
    existing = await session.execute(
        text(
            "SELECT id FROM inventory_movements WHERE tenant_id = :tid AND idempotency_key = :key"
        ),
        {"tid": tenant_id, "key": idem_key},
    )
    if existing.fetchone():
        return None

    # ── read original movement ────────────────────────────────────────────────
    orig_res = await session.execute(
        text("""
            SELECT delta, movement_type, yield_factor_applied FROM inventory_movements
             WHERE tenant_id = :tid AND id = :oid
        """),
        {"tid": tenant_id, "oid": original_movement_id},
    )
    orig_row = orig_res.fetchone()
    if not orig_row:
        raise ValueError(
            f"movement {original_movement_id} not found for tenant {tenant_id}"
        )

    orig_delta: Decimal = Decimal(str(orig_row[0]))
    orig_type: str = orig_row[1]
    orig_yield = Decimal(str(orig_row[2])) if orig_row[2] is not None else None

    if orig_type == "sale_depletion":
        reversal_type = "sale_depletion_reversal"
    elif orig_type == "sale_signal":
        reversal_type = "sale_signal_reversal"
    else:
        raise ValueError(
            f"movement {original_movement_id} has type {orig_type!r}; "
            "only sale_depletion and sale_signal can be reversed via this function"
        )

    mv_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO inventory_movements
                (id, tenant_id, inventory_item_id, movement_type, delta,
                 source_type, source_id, idempotency_key, yield_factor_applied)
            VALUES (:id, :tid, :iid, :mtype, :delta,
                    'reversal', :orig_id, :key, :yf)
        """),
        {
            "id": mv_id,
            "tid": tenant_id,
            "iid": inventory_item_id,
            "mtype": reversal_type,
            "delta": -orig_delta,
            "orig_id": original_movement_id,
            "key": idem_key,
            "yf": orig_yield,
        },
    )

    await session.flush()
    return mv_id
