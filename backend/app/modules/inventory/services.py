"""Inventory service layer — Sprint 3 Phases 4A-4D.

All functions accept an AsyncSession and are intentionally side-effect free
with respect to committing: the caller owns the transaction boundary.
Every write operation is idempotent via idempotency_key on inventory_movements
or receipt commit_state on receipts.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# on_hand() — Python implementation (replaces retired on_hand() SQL function)
#
# Mode A (recipe_deducted):
#   SUM of all movement deltas except sale_signal / sale_signal_reversal.
# Mode B (count_anchored):
#   last_count_quantity
#   + receipts/adjusts strictly AFTER last_count_at
#   - SUM(ABS(sale_signals since last_count_at)) x yield_factor
#   Returns None when last_count_quantity is NULL (count not yet done).
# ═════════════════════════════════════════════════════════════════════════════


async def on_hand(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    inventory_item_id: UUID,
    reconciliation_cutoff: datetime | None = None,
) -> Decimal | None:
    """Compute current on-hand quantity.

    reconciliation_cutoff: when provided (new counts, post-0010), Mode B filters
    movements by created_at > cutoff and uses per-row yield_factor_applied.
    When None (historical rows), falls back to recorded_at > last_count_at and
    the live inventory_yield_factors table — backwards-compatible behaviour.
    """
    if reconciliation_cutoff is not None:
        # Watermark path: created_at boundary, per-row yield snapshot.
        sql = """
            WITH item AS (
                SELECT inventory_mode, last_count_at, last_count_quantity
                  FROM inventory_items
                 WHERE tenant_id = :tid AND id = :iid
            ),
            ledger_sum AS (
                SELECT COALESCE(SUM(delta), 0) AS qty
                  FROM inventory_movements
                 WHERE tenant_id         = :tid
                   AND inventory_item_id = :iid
                   AND movement_type NOT IN ('sale_signal','sale_signal_reversal')
            ),
            receipts_since AS (
                SELECT COALESCE(SUM(m.delta), 0) AS qty
                  FROM inventory_movements m
                 WHERE m.tenant_id         = :tid
                   AND m.inventory_item_id = :iid
                   AND m.movement_type IN ('receive','transfer_in','count_adjust','opening_balance')
                   AND m.created_at > :cutoff
            ),
            signals_since AS (
                SELECT COALESCE(SUM(
                           m.delta
                           * COALESCE(m.yield_factor_applied,
                                      (SELECT yield_factor FROM inventory_yield_factors
                                        WHERE tenant_id         = :tid
                                          AND inventory_item_id = :iid),
                                      1.0)
                       ), 0) AS qty
                  FROM inventory_movements m
                 WHERE m.tenant_id         = :tid
                   AND m.inventory_item_id = :iid
                   AND m.movement_type     IN ('sale_signal', 'sale_signal_reversal')
                   AND m.created_at > :cutoff
            )
            SELECT CASE
                WHEN item.inventory_mode = 'recipe_deducted'
                    THEN ledger_sum.qty
                WHEN item.inventory_mode = 'count_anchored'
                     AND item.last_count_quantity IS NOT NULL
                    THEN item.last_count_quantity
                         + receipts_since.qty
                         - signals_since.qty
                ELSE NULL
            END AS qty
            FROM item, ledger_sum, receipts_since, signals_since
        """
        params: dict[str, Any] = {
            "tid": tenant_id,
            "iid": inventory_item_id,
            "cutoff": reconciliation_cutoff,
        }
    else:
        # Historical path: recorded_at boundary, live yield factor table.
        sql = """
            WITH item AS (
                SELECT inventory_mode, last_count_at, last_count_quantity
                  FROM inventory_items
                 WHERE tenant_id = :tid AND id = :iid
            ),
            ledger_sum AS (
                SELECT COALESCE(SUM(delta), 0) AS qty
                  FROM inventory_movements
                 WHERE tenant_id         = :tid
                   AND inventory_item_id = :iid
                   AND movement_type NOT IN ('sale_signal','sale_signal_reversal')
            ),
            receipts_since AS (
                SELECT COALESCE(SUM(m.delta), 0) AS qty
                  FROM inventory_movements m, item
                 WHERE m.tenant_id         = :tid
                   AND m.inventory_item_id = :iid
                   AND m.recorded_at       > item.last_count_at
                   AND m.movement_type IN ('receive','transfer_in','count_adjust','opening_balance')
            ),
            signals_since AS (
                SELECT COALESCE(SUM(m.delta), 0) AS qty
                  FROM inventory_movements m, item
                 WHERE m.tenant_id         = :tid
                   AND m.inventory_item_id = :iid
                   AND m.recorded_at       > item.last_count_at
                   AND m.movement_type     IN ('sale_signal', 'sale_signal_reversal')
            )
            SELECT CASE
                WHEN item.inventory_mode = 'recipe_deducted'
                    THEN ledger_sum.qty
                WHEN item.inventory_mode = 'count_anchored'
                     AND item.last_count_quantity IS NOT NULL
                    THEN item.last_count_quantity
                         + receipts_since.qty
                         - (signals_since.qty
                            * COALESCE(
                                (SELECT yield_factor FROM inventory_yield_factors
                                  WHERE tenant_id         = :tid
                                    AND inventory_item_id = :iid),
                                1.0))
                ELSE NULL
            END AS qty
            FROM item, ledger_sum, receipts_since, signals_since
        """
        params = {"tid": tenant_id, "iid": inventory_item_id}

    result = await session.execute(text(sql), params)
    return result.scalar_one_or_none()


# ═════════════════════════════════════════════════════════════════════════════
# 4A — record_opening_balance
# ═════════════════════════════════════════════════════════════════════════════


async def record_opening_balance(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    inventory_item_id: UUID,
    quantity: Decimal,
    recorded_at: datetime | None = None,
) -> UUID:
    """Insert an opening_balance movement.  Idempotent.

    For Mode B items the function also sets last_count_quantity / last_count_at
    atomically so that on_hand() returns the correct anchor immediately.

    Idempotency key: ``opening_balance:{inventory_item_id}``
    An opening balance is permanent — delete + recreate the item to correct a
    wrong opening balance before any other movement exists.
    """
    recorded_at = recorded_at or datetime.now(UTC)
    idem_key = f"opening_balance:{inventory_item_id}"

    # ── idempotency check ────────────────────────────────────────────────────
    existing = await session.execute(
        text(
            "SELECT id FROM inventory_movements WHERE tenant_id = :tid AND idempotency_key = :key"
        ),
        {"tid": tenant_id, "key": idem_key},
    )
    row = existing.fetchone()
    if row:
        return UUID(str(row[0]))

    # ── resolve mode ─────────────────────────────────────────────────────────
    item_res = await session.execute(
        text("SELECT inventory_mode FROM inventory_items WHERE tenant_id = :tid AND id = :iid"),
        {"tid": tenant_id, "iid": inventory_item_id},
    )
    item_row = item_res.fetchone()
    if not item_row:
        raise ValueError(f"inventory_item {inventory_item_id} not found for tenant {tenant_id}")
    mode: str = item_row[0]

    # ── insert movement ───────────────────────────────────────────────────────
    mv_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO inventory_movements
                (id, tenant_id, inventory_item_id, movement_type, delta,
                 source_type, idempotency_key, recorded_at)
            VALUES (:id, :tid, :iid, 'opening_balance', :delta,
                    'opening', :key, :at)
        """),
        {
            "id": mv_id,
            "tid": tenant_id,
            "iid": inventory_item_id,
            "delta": quantity,
            "key": idem_key,
            "at": recorded_at,
        },
    )

    # ── Mode B: set anchor (same timestamp → excluded by on_hand()'s > filter) ──
    if mode == "count_anchored":
        await session.execute(
            text("""
                UPDATE inventory_items
                   SET last_count_quantity = :qty,
                       last_count_at       = :at
                 WHERE tenant_id = :tid AND id = :iid
            """),
            {"qty": quantity, "at": recorded_at, "tid": tenant_id, "iid": inventory_item_id},
        )

    await session.flush()
    return mv_id


# ═════════════════════════════════════════════════════════════════════════════
# 4B — record_sale_inventory_effect
# ═════════════════════════════════════════════════════════════════════════════


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


# ═════════════════════════════════════════════════════════════════════════════
# 4B-reversal — record_sale_reversal
# ═════════════════════════════════════════════════════════════════════════════


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


# ═════════════════════════════════════════════════════════════════════════════
# 4C — record_count_event
# ═════════════════════════════════════════════════════════════════════════════


async def record_count_event(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    inventory_item_id: UUID,
    counted_quantity: Decimal,
    counted_at: datetime | None = None,
    counted_by: UUID | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Record a physical count event.

    Steps:
      1. Lock item row and read inventory_mode.
      2. Call Python on_hand() BEFORE inserting to capture predicted stock.
      3. INSERT into inventory_count_events.
      4. Mode-specific correction (was trigger fn_count_event_emits_adjust):
           Mode B → UPDATE inventory_items to re-anchor last_count_at/quantity.
           Mode A → INSERT count_adjust movement if drift is non-trivial.
      5. UPSERT monitoring_alerts when drift >= 5%.

    Drift sign convention (for reporting):
        alert_drift = predicted - counted  (positive = we overpredicted / a loss)
        count_adjust delta = counted - predicted  (corrects the ledger upward or downward)
    """
    counted_at = counted_at or datetime.now(UTC)
    idem_key = f"count:{inventory_item_id}:{counted_at.isoformat()}:{counted_quantity}"

    # ── 1. lock item row + read mode — serialises concurrent counts ───────────
    lock_res = await session.execute(
        text(
            "SELECT id, inventory_mode FROM inventory_items"
            " WHERE tenant_id = :tid AND id = :iid FOR UPDATE"
        ),
        {"tid": tenant_id, "iid": inventory_item_id},
    )
    lock_row = lock_res.fetchone()
    if lock_row is None:
        raise ValueError(f"inventory_item {inventory_item_id} not found for tenant {tenant_id}")
    item_mode: str = lock_row[1]

    # Watermark captured while the item row lock is held.  Any sale_signal
    # that arrives concurrently must wait for this transaction; its created_at
    # will be > cutoff and correctly attributed to the post-count period.
    cutoff = datetime.now(UTC)

    # ── idempotency ────────────────────────────────────────────────────────────
    existing = await session.execute(
        text(
            "SELECT id FROM inventory_count_events"
            " WHERE tenant_id = :tid AND idempotency_key = :key"
        ),
        {"tid": tenant_id, "key": idem_key},
    )
    existing_row = existing.fetchone()
    if existing_row:
        return {
            "id": UUID(str(existing_row[0])),
            "predicted_on_hand_at_count": None,
            "counted_quantity": counted_quantity,
        }

    # ── 2. snapshot predicted on_hand using watermark (lock held) ─────────────
    predicted: Decimal | None = await on_hand(
        session,
        tenant_id=tenant_id,
        inventory_item_id=inventory_item_id,
        reconciliation_cutoff=cutoff,
    )

    # ── 3. insert count event with reconciliation watermark ───────────────────
    event_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO inventory_count_events
                (id, tenant_id, inventory_item_id,
                 counted_quantity, predicted_on_hand_at_count,
                 counted_at, counted_by, notes, idempotency_key,
                 reconciliation_cutoff_created_at)
            VALUES
                (:id, :tid, :iid,
                 :counted, :predicted,
                 :at, :by, :notes, :key, :cutoff)
        """),
        {
            "id": event_id,
            "tid": tenant_id,
            "iid": inventory_item_id,
            "counted": counted_quantity,
            "predicted": predicted,
            "at": counted_at,
            "by": counted_by,
            "notes": notes,
            "key": idem_key,
            "cutoff": cutoff,
        },
    )
    await session.flush()

    # ── 4. mode-specific correction (was trigger fn_count_event_emits_adjust) ─
    if item_mode == "count_anchored":
        # Mode B: re-anchor to the physical count; no ledger row needed.
        await session.execute(
            text("""
                UPDATE inventory_items
                   SET last_count_at       = :at,
                       last_count_quantity = :qty
                 WHERE tenant_id = :tid AND id = :iid
            """),
            {"at": counted_at, "qty": counted_quantity, "tid": tenant_id, "iid": inventory_item_id},
        )
    else:
        # Mode A: emit count_adjust if drift is non-trivial.
        # delta sign: counted - predicted  (negative = stock loss vs. ledger)
        count_adjust_delta = counted_quantity - (
            predicted if predicted is not None else counted_quantity
        )
        if abs(count_adjust_delta) >= Decimal("0.0001"):
            await session.execute(
                text("""
                    INSERT INTO inventory_movements
                        (tenant_id, inventory_item_id, movement_type, delta,
                         source_type, source_id, idempotency_key, recorded_at, notes)
                    VALUES
                        (:tid, :iid, 'count_adjust', :delta,
                         'count_event', :event_id,
                         :idem_key, :at, :notes)
                """),
                {
                    "tid": tenant_id,
                    "iid": inventory_item_id,
                    "delta": count_adjust_delta,
                    "event_id": event_id,
                    "idem_key": f"count_adjust:{event_id}",
                    "at": counted_at,
                    "notes": f"System count correction from {event_id}",
                },
            )
    await session.flush()

    # ── 5. monitoring alert (only when drift >= 5%) ───────────────────────────
    if predicted is not None and counted_quantity > 0:
        alert_drift = predicted - counted_quantity
        drift_pct = alert_drift / counted_quantity

        if abs(drift_pct) > Decimal("0.20"):
            severity = "critical"
        elif abs(drift_pct) > Decimal("0.05"):
            severity = "warn"
        else:
            severity = "info"

        payload_str = json.dumps(
            {
                "inventory_item_id": str(inventory_item_id),
                "counted_quantity": str(counted_quantity),
                "predicted_on_hand": str(predicted),
                "drift": str(alert_drift),
                "drift_pct": str(drift_pct),
            }
        )
        await session.execute(
            text("""
                INSERT INTO monitoring_alerts
                    (id, tenant_id, monitor_name, severity, trigger_payload)
                VALUES (
                    gen_random_uuid(), :tid,
                    'integrity_drift_high', :severity,
                    CAST(:payload AS jsonb)
                )
                ON CONFLICT (tenant_id, monitor_name) WHERE resolved_at IS NULL
                DO UPDATE SET
                    last_seen_at    = now(),
                    alert_count     = monitoring_alerts.alert_count + 1,
                    severity        = EXCLUDED.severity,
                    trigger_payload = EXCLUDED.trigger_payload
            """),
            {"tid": tenant_id, "severity": severity, "payload": payload_str},
        )
        await session.flush()

    return {
        "id": event_id,
        "predicted_on_hand_at_count": predicted,
        "counted_quantity": counted_quantity,
    }


# ═════════════════════════════════════════════════════════════════════════════
# 4D — Receipt helpers (called by router)
# ═════════════════════════════════════════════════════════════════════════════


async def create_receipt(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    created_by: UUID | None = None,
    notes: str | None = None,
    received_at: datetime | None = None,
) -> UUID:
    """Create a receipt in 'draft' state."""
    receipt_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO receipts (id, tenant_id, commit_state, received_at,
                                  created_by, notes)
            VALUES (:id, :tid, 'draft', COALESCE(:at, NOW()), :by, :notes)
        """),
        {
            "id": receipt_id,
            "tid": tenant_id,
            "at": received_at,
            "by": created_by,
            "notes": notes,
        },
    )
    await session.flush()
    return receipt_id


async def add_receipt_line(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    receipt_id: UUID,
    inventory_item_id: UUID,
    received_quantity: Decimal,
    purchase_unit_id: UUID | None = None,
    unit_cost_cents: int | None = None,
) -> UUID:
    """Add a line to a draft receipt."""
    line_id = uuid4()
    idem_key = f"receipt_line:{line_id}"
    await session.execute(
        text("""
            INSERT INTO receipt_lines
                (id, tenant_id, receipt_id, inventory_item_id,
                 received_quantity, purchase_unit_id, unit_cost_cents, idempotency_key)
            VALUES (:id, :tid, :rid, :iid, :qty, :puid, :cost, :key)
        """),
        {
            "id": line_id,
            "tid": tenant_id,
            "rid": receipt_id,
            "iid": inventory_item_id,
            "qty": received_quantity,
            "puid": purchase_unit_id,
            "cost": unit_cost_cents,
            "key": idem_key,
        },
    )
    await session.flush()
    return line_id


async def commit_receipt(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    receipt_id: UUID,
) -> dict[str, Any]:
    """Commit a draft receipt.

    Transaction:
      1. Lock the receipt row (pessimistic — prevents double-commit race).
      2. Skip if already committed (idempotent).
      3. For each line:
           a. Convert received_quantity to storage units.
              (Sprint 3: purchase_unit = storage_unit, factor = 1.0)
           b. Insert receive movement, idempotency_key = receipt_line:{line_id}.
           c. Insert ingredient_cost_snapshot.
           d. Set receipt_lines.emits_movement_id.
      4. Set receipts.commit_state = 'committed', committed_at = now().
    """
    # ── 1. lock + state check ─────────────────────────────────────────────────
    receipt_res = await session.execute(
        text("SELECT commit_state FROM receipts WHERE tenant_id = :tid AND id = :rid FOR UPDATE"),
        {"tid": tenant_id, "rid": receipt_id},
    )
    receipt_row = receipt_res.fetchone()
    if not receipt_row:
        raise ValueError(f"receipt {receipt_id} not found")
    if receipt_row[0] == "committed":
        return {"receipt_id": receipt_id, "status": "already_committed"}

    # ── 2. fetch lines ─────────────────────────────────────────────────────────
    lines_res = await session.execute(
        text("""
            SELECT id, inventory_item_id, received_quantity,
                   purchase_unit_id, unit_cost_cents, idempotency_key
              FROM receipt_lines
             WHERE tenant_id = :tid AND receipt_id = :rid
        """),
        {"tid": tenant_id, "rid": receipt_id},
    )
    lines = lines_res.fetchall()

    movement_ids = []
    for line in lines:
        line_id = UUID(str(line[0]))
        item_id = UUID(str(line[1]))
        received_qty = Decimal(str(line[2]))
        purchase_unit_id = line[3]
        unit_cost_cents = line[4]
        idem_key = str(line[5])

        # ── idempotency: skip if movement already exists ──────────────────────
        existing_res = await session.execute(
            text(
                "SELECT id FROM inventory_movements"
                " WHERE tenant_id = :tid AND idempotency_key = :key"
            ),
            {"tid": tenant_id, "key": idem_key},
        )
        existing_mv = existing_res.fetchone()

        if existing_mv:
            mv_id = UUID(str(existing_mv[0]))
        else:
            # ── a. storage qty (Sprint 3: 1:1 since no unit conversion yet) ──
            storage_qty = received_qty

            # ── b. receive movement ───────────────────────────────────────────
            mv_id = uuid4()
            await session.execute(
                text("""
                    INSERT INTO inventory_movements
                        (id, tenant_id, inventory_item_id, movement_type, delta,
                         source_type, source_id, idempotency_key)
                    VALUES (:id, :tid, :iid, 'receive', :delta,
                            'receipt_line', :line_id, :key)
                """),
                {
                    "id": mv_id,
                    "tid": tenant_id,
                    "iid": item_id,
                    "delta": storage_qty,
                    "line_id": line_id,
                    "key": idem_key,
                },
            )

            # ── c. cost snapshot ──────────────────────────────────────────────
            if unit_cost_cents is not None:
                await session.execute(
                    text("""
                        INSERT INTO ingredient_cost_snapshots
                            (id, tenant_id, inventory_item_id, unit_cost_cents,
                             purchase_unit_id, source_receipt_line_id)
                        VALUES (gen_random_uuid(), :tid, :iid, :cost, :puid, :lid)
                    """),
                    {
                        "tid": tenant_id,
                        "iid": item_id,
                        "cost": unit_cost_cents,
                        "puid": purchase_unit_id,
                        "lid": line_id,
                    },
                )

            # ── d. back-link line → movement ──────────────────────────────────
            await session.execute(
                text(
                    "UPDATE receipt_lines SET emits_movement_id = :mv_id"
                    " WHERE tenant_id = :tid AND id = :lid"
                ),
                {"mv_id": mv_id, "tid": tenant_id, "lid": line_id},
            )

        movement_ids.append(mv_id)

    # ── 4. mark committed ─────────────────────────────────────────────────────
    await session.execute(
        text("""
            UPDATE receipts
               SET commit_state = 'committed',
                   committed_at = NOW()
             WHERE tenant_id = :tid AND id = :rid
        """),
        {"tid": tenant_id, "rid": receipt_id},
    )

    await session.flush()
    return {"receipt_id": receipt_id, "movement_ids": movement_ids, "status": "committed"}
