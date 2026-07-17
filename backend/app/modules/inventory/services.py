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
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.inventory.depletion.conversions import convert
from app.modules.inventory.depletion.units import DIMENSION_OF
from app.modules.inventory.depletion.units import is_canonical as is_canonical_unit

log = logging.getLogger(__name__)

# Receipt-intake sources subject to the human-confirm gate (D-606-05). 'pos' is
# exempt (deterministic Clover walk); the receipt path enforces it.
_INTAKE_SOURCES = frozenset({"mobile_photo", "gmail", "email", "webhook", "manual"})


class ReceiptNothingToCommit(Exception):
    """Every line is skipped / non-inventory — nothing to receive (D-606-06).
    The operator dismisses; this is not a commit error (→ RECEIPT_NOTHING_TO_COMMIT)."""


class ReceiptLinesUnmatched(Exception):
    """At least one non-skipped line has no inventory item. Distinct from
    ReceiptNothingToCommit: the receipt HAS receivable lines, the operator just
    hasn't linked/created items (or skipped them) yet. Committing anyway would
    silently drop those lines from inventory (→ RECEIPT_LINES_UNMATCHED)."""


class ReceiptConversionRequired(Exception):
    """One or more movable lines cannot convert to their item's storage unit —
    a purchase unit (CS, SAC, ...) with no operator-confirmed conversion, OR a
    canonical unit in a DIFFERENT dimension (ea → L). Commit never guesses a
    factor (→ RECEIPT_UNIT_CONVERSION_REQUIRED).

    `errors` is the structured per-line payload (machine-readable ids + names +
    package evidence + safe suggestion). The exception MESSAGE stays free of
    ids/internal terms — it may reach a UI verbatim."""

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class ItemHasMovements(Exception):
    """Storage-unit change refused: the item already has ledger movements —
    rewriting the unit would silently re-denominate history
    (→ 409 ITEM_HAS_MOVEMENTS)."""


class ItemInRecipes(Exception):
    """Storage-unit change refused: recipes/modifiers reference the item —
    depletion conversions would silently change meaning (→ 409 ITEM_IN_RECIPES)."""


class ReceiptReviewRequired(Exception):
    """An intake-source commit lacks the human gate: confirm + at least one
    manually-corrected line OR a review affirmation (D-606-22) (→ RECEIPT_REVIEW_REQUIRED)."""


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


# Sale depletion writers live in app.modules.inventory.depletion.writer
# (write_movement, record_sale_reversal) — moved/rewritten in Sprint 5 Phase 7/9.


# ═════════════════════════════════════════════════════════════════════════════
# 4C — record_count_event
# ═════════════════════════════════════════════════════════════════════════════


async def _emit_count_adjust(
    session: AsyncSession,
    tenant_id: UUID,
    inventory_item_id: UUID,
    event_id: UUID,
    delta: Decimal,
    recorded_at: datetime,
) -> None:
    """Emit the Mode-A `count_adjust` ledger movement for a count event. Extracted as the autospec
    seam for the atomicity test — it is the count-event/correction boundary. Runs in the CALLER's
    session/txn at the SAME sequence point as the prior inline INSERT; introduces NO new
    boundary."""
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
            "delta": delta,
            "event_id": event_id,
            "idem_key": f"count_adjust:{event_id}",
            "at": recorded_at,
            "notes": f"System count correction from {event_id}",
        },
    )


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
            await _emit_count_adjust(
                session, tenant_id, inventory_item_id, event_id, count_adjust_delta, counted_at
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
            INSERT INTO receipts (id, tenant_id, commit_state, source, received_at,
                                  created_by, notes)
            VALUES (:id, :tid, 'draft', 'manual', COALESCE(:at, NOW()), :by, :notes)
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


async def _mark_receipt_committed(
    session: AsyncSession,
    tenant_id: UUID,
    receipt_id: UUID,
    *,
    confirm: bool = False,
    reviewed_affirmation: bool = False,
    source: str | None = None,
) -> None:
    """Set the receipt's terminal committed state — the autospec seam for the atomicity
    test. Stamps confirmed_at (server-side, only when confirm=true on an intake source —
    D-606-04) and reviewed_affirmation in the SAME UPDATE that flips commit_state, so the
    BEFORE-UPDATE trigger (trg_receipt_commit_integrity) reads the NEW gate values. Runs in
    the CALLER's session/txn; introduces no new session or commit."""
    set_confirmed = confirm and source in _INTAKE_SOURCES
    await session.execute(
        text("""
            UPDATE receipts
               SET commit_state = 'committed',
                   committed_at = NOW(),
                   confirmed_at = CASE WHEN :set_confirmed THEN NOW() ELSE confirmed_at END,
                   reviewed_affirmation = :affirmation
             WHERE tenant_id = :tid AND id = :rid
        """),
        {
            "set_confirmed": set_confirmed,
            "affirmation": reviewed_affirmation,
            "tid": tenant_id,
            "rid": receipt_id,
        },
    )


async def _idempotent_movement_insert(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    inventory_item_id: UUID,
    delta: Decimal,
    line_id: UUID,
    key: str,
) -> UUID:
    """Insert a receive movement, race-safe via a SAVEPOINT: on a UNIQUE(tenant_id,
    idempotency_key) collision (a partial-then-retried commit), roll back the savepoint
    and return the existing movement id deterministically (D-606-16). N per-line keys
    therefore never need N top-level idempotency claims."""
    mv_id = uuid4()
    try:
        async with session.begin_nested():
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
                    "iid": inventory_item_id,
                    "delta": delta,
                    "line_id": line_id,
                    "key": key,
                },
            )
        return mv_id
    except IntegrityError:
        existing = await session.execute(
            text(
                "SELECT id FROM inventory_movements "
                "WHERE tenant_id = :tid AND idempotency_key = :key"
            ),
            {"tid": tenant_id, "key": key},
        )
        return UUID(str(existing.scalar_one()))


async def change_item_storage_unit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    inventory_item_id: UUID,
    storage_unit: str,
) -> dict[str, Any]:
    """Correct an item's storage unit — SAFE cases only (the goblet lesson:
    a mis-picked oz_weight item for count goods).

    Allowed only while the item's history is empty: zero inventory_movements
    and zero recipe/modifier references. Anything else would silently
    re-denominate the ledger or depletion math — those need an explicit
    migration story, not an UPDATE. recipe_unit_id moves with storage_unit_id
    (resolver invariant: items are created with both equal)."""
    from app.modules.inventory.item_resolver import resolve_unit

    row = (
        await session.execute(
            text("SELECT id FROM inventory_items WHERE tenant_id = :tid AND id = :iid FOR UPDATE"),
            {"tid": tenant_id, "iid": inventory_item_id},
        )
    ).fetchone()
    if row is None:
        raise ValueError(f"inventory_item {inventory_item_id} not found")

    movements = (
        await session.execute(
            text(
                "SELECT count(*) FROM inventory_movements "
                "WHERE tenant_id = :tid AND inventory_item_id = :iid"
            ),
            {"tid": tenant_id, "iid": inventory_item_id},
        )
    ).scalar_one()
    if movements:
        raise ItemHasMovements(f"{movements} movement(s) exist")

    refs = (
        await session.execute(
            text(
                "SELECT (SELECT count(*) FROM recipe_ingredients "
                "         WHERE tenant_id = :tid AND inventory_item_id = :iid) + "
                "       (SELECT count(*) FROM modifier_ingredients "
                "         WHERE tenant_id = :tid AND inventory_item_id = :iid)"
            ),
            {"tid": tenant_id, "iid": inventory_item_id},
        )
    ).scalar_one()
    if refs:
        raise ItemInRecipes(f"{refs} recipe/modifier reference(s) exist")

    unit_id = await resolve_unit(session, tenant_id, storage_unit)
    await session.execute(
        text(
            "UPDATE inventory_items SET storage_unit_id = :uid, recipe_unit_id = :uid "
            "WHERE tenant_id = :tid AND id = :iid"
        ),
        {"uid": unit_id, "tid": tenant_id, "iid": inventory_item_id},
    )
    return {"id": inventory_item_id, "storage_unit": storage_unit}


async def commit_receipt(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    receipt_id: UUID,
    confirm: bool = False,
    reviewed_affirmation: bool = False,
) -> dict[str, Any]:
    """Commit a draft receipt — the ONE commit path (D-606-16), shared by every source.

    1. Lock the receipt (FOR UPDATE) + its lines (FOR UPDATE OF rl) → prevents a
       double-commit race and freezes the line set the affirmation guard validates.
    2. Already committed → idempotent no-op.
    3. Integrity floor (D-606-06): >=1 non-skipped line with an item, else
       ReceiptNothingToCommit. Intake sources additionally require the human gate
       (confirm + manually-corrected line OR reviewed_affirmation, D-606-22), else
       ReceiptReviewRequired. 'pos' is exempt (deterministic). The DB trigger is the
       backstop for all of this.
    4. Per movable line: convert() purchase→storage unit (identity when equal — so
       existing same-unit receipts are bit-identical), receive movement (savepoint
       idempotency, key = the line's idempotency_key or receipt_line:{id}), cost
       snapshot, back-link. Skipped / item-less lines write nothing.
    5. commit_state='committed', committed_at, confirmed_at (server-set), affirmation.
    """
    # ── 1. lock receipt ───────────────────────────────────────────────────────
    receipt_row = (
        await session.execute(
            text(
                "SELECT commit_state, source FROM receipts "
                "WHERE tenant_id = :tid AND id = :rid FOR UPDATE"
            ),
            {"tid": tenant_id, "rid": receipt_id},
        )
    ).fetchone()
    if not receipt_row:
        raise ValueError(f"receipt {receipt_id} not found")
    if receipt_row[0] == "committed":
        return {"receipt_id": receipt_id, "status": "already_committed"}
    source = receipt_row[1]

    # ── 2. lock + fetch lines (with unit names + item storage unit for convert) ──
    lines = (
        (
            await session.execute(
                text("""
                SELECT rl.id, rl.inventory_item_id, rl.received_quantity,
                       rl.purchase_unit_id, rl.unit_cost_cents, rl.idempotency_key,
                       rl.match_status, rl.manually_corrected, rl.extracted_unit,
                       rl.received_unit, rl.line_total_cents, rl.extracted_name,
                       rl.pack_count, rl.pack_size_qty, rl.pack_size_unit,
                       rl.actual_weight_qty, rl.actual_weight_unit,
                       pu.name AS purchase_unit_name, su.name AS storage_unit_name,
                       ii.name AS item_name
                  FROM receipt_lines rl
                  LEFT JOIN units_of_measure pu ON pu.id = rl.purchase_unit_id
                  -- tenant predicate on the item join: a (hypothetical) cross-tenant
                  -- item id yields NULL name — another tenant's data never leaves here
                  LEFT JOIN inventory_items   ii
                         ON ii.id = rl.inventory_item_id AND ii.tenant_id = rl.tenant_id
                  LEFT JOIN units_of_measure  su ON su.id = ii.storage_unit_id
                 WHERE rl.tenant_id = :tid AND rl.receipt_id = :rid
                 FOR UPDATE OF rl
            """),
                {"tid": tenant_id, "rid": receipt_id},
            )
        )
        .mappings()
        .all()
    )

    # ── 3. integrity floor + human-review gate ───────────────────────────────
    movable = [
        ln
        for ln in lines
        if ln["match_status"] != "skipped" and ln["inventory_item_id"] is not None
    ]
    # Every non-skipped line must be RESOLVED (item linked/created) before any
    # line commits — silently ignoring unmatched lines would receive a partial
    # invoice with no operator signal. Skip is the only deliberate way out.
    unresolved = [
        ln for ln in lines if ln["match_status"] != "skipped" and ln["inventory_item_id"] is None
    ]
    if unresolved:
        raise ReceiptLinesUnmatched(
            f"receipt {receipt_id} has {len(unresolved)} line(s) without an inventory item"
        )
    if not movable:
        raise ReceiptNothingToCommit(f"receipt {receipt_id} has no inventory-moving line")

    # Unit-conversion gate — validate EVERY movable line BEFORE any write and
    # return all blockers together. Two ways a line can't convert:
    #   (a) non-canonical purchase unit (CS, SAC, BOX...) with no operator-
    #       confirmed conversion;
    #   (b) a CANONICAL unit in a DIFFERENT dimension than the storage unit
    #       (ea → L) — convert() has no path across dimensions, and the old
    #       canonical exemption let exactly this reach the movement loop (live
    #       cert: SIROP VANILLE, 'ea' invoice unit, litre-tracked item).
    # We NEVER guess a factor; a wrong silent conversion corrupts stock+costing.
    from app.modules.receipts.conversion import suggest_conversion

    conversion_errors: list[dict[str, Any]] = []
    for ln in movable:
        eff = ln["received_unit"] or ln["purchase_unit_name"] or ln["extracted_unit"]
        storage = ln["storage_unit_name"]
        if not eff or eff == storage:
            continue
        if storage is not None:
            cross_dimension = (
                is_canonical_unit(eff)
                and DIMENSION_OF.get(eff) is not None
                and DIMENSION_OF.get(storage) is not None
                and DIMENSION_OF[eff] != DIMENSION_OF[storage]
            )
            if is_canonical_unit(eff) and not cross_dimension:
                continue  # same-dimension canonical (ml → L) converts deterministically
        # storage None (tenant-scoped item join yielded nothing — e.g. a foreign
        # item id) can never validate → block; names stay None, nothing leaks.
        qty = Decimal(str(ln["received_quantity"])) if ln["received_quantity"] else None
        suggestion = (
            suggest_conversion(
                purchase_qty=qty,
                purchase_unit=eff,
                storage_unit=storage,
                pack_count=ln["pack_count"],
                pack_size_qty=ln["pack_size_qty"],
                pack_size_unit=ln["pack_size_unit"],
                actual_weight_qty=ln["actual_weight_qty"],
                actual_weight_unit=ln["actual_weight_unit"],
            )
            if qty and qty > 0 and storage is not None
            else None
        )
        package_hint = (
            f"{ln['actual_weight_qty']} {ln['actual_weight_unit'] or ''}".strip()
            if ln["actual_weight_qty"]
            else (
                f"{ln['pack_count']} x {ln['pack_size_qty']} {ln['pack_size_unit'] or ''}".strip()
                if ln["pack_count"] and ln["pack_size_qty"]
                else (
                    f"{ln['pack_size_qty']} {ln['pack_size_unit'] or ''}".strip()
                    if ln["pack_size_qty"]
                    else None
                )
            )
        )
        conversion_errors.append(
            {
                "receipt_line_id": str(ln["id"]),
                "inventory_item_id": str(ln["inventory_item_id"]),
                "inventory_item_name": ln["item_name"],  # tenant-safe join above
                "invoice_name": ln["extracted_name"],
                "purchase_quantity": str(qty) if qty is not None else None,
                "purchase_unit": eff,
                "storage_unit": storage,
                "package_hint": package_hint,
                "suggested_factor": str(suggestion.factor) if suggestion else None,
                "suggested_received_quantity": str(suggestion.quantity) if suggestion else None,
            }
        )
    if conversion_errors:
        # Message is user-safe by contract: counts only — ids/units live in `errors`.
        raise ReceiptConversionRequired(
            f"{len(conversion_errors)} item(s) need a package conversion before receiving.",
            errors=conversion_errors,
        )

    if source in _INTAKE_SOURCES:
        if not confirm:
            raise ReceiptReviewRequired("confirm:true required for an intake-source commit")
        any_corrected = any(ln["manually_corrected"] for ln in lines)
        if not any_corrected and not reviewed_affirmation:
            raise ReceiptReviewRequired(
                "requires >=1 manually-corrected line or a review affirmation"
            )

    # ── 4. per movable line → movement + cost snapshot + back-link ────────────
    movement_ids = []
    for ln in movable:
        line_id = UUID(str(ln["id"]))
        item_id = UUID(str(ln["inventory_item_id"]))
        received_qty = Decimal(str(ln["received_quantity"]))
        key = ln["idempotency_key"] or f"receipt_line:{line_id}"

        # Precedence: operator-confirmed received_unit → resolved purchase uom →
        # raw extracted unit. The gate above guarantees this is canonical or
        # literally the storage unit by the time we get here.
        from_unit = ln["received_unit"] or ln["purchase_unit_name"] or ln["extracted_unit"]
        to_unit = ln["storage_unit_name"]
        if from_unit and to_unit and from_unit != to_unit:
            storage_qty = await convert(
                session,
                received_qty,
                from_unit,
                to_unit,
                inventory_item_id=item_id,
                tenant_id=tenant_id,
            )
        else:
            storage_qty = received_qty  # identity (same unit, or no unit info)

        mv_id = await _idempotent_movement_insert(
            session,
            tenant_id=tenant_id,
            inventory_item_id=item_id,
            delta=storage_qty,
            line_id=line_id,
            key=key,
        )

        # Cost basis precedence: the invoice's printed line total over storage
        # qty (exact, survives weight-priced lines and sub-cent unit costs) →
        # else the line's unit_cost_cents (manual path; already per received
        # unit). NUMERIC column is the truth; the legacy int column is a
        # rounded convenience for existing readers.
        exact_cost: Decimal | None = None
        if ln["line_total_cents"] is not None and storage_qty > 0:
            exact_cost = Decimal(ln["line_total_cents"]) / storage_qty
        elif ln["unit_cost_cents"] is not None:
            exact_cost = Decimal(ln["unit_cost_cents"])
        if exact_cost is not None:
            await session.execute(
                text("""
                    INSERT INTO ingredient_cost_snapshots
                        (id, tenant_id, inventory_item_id, unit_cost_cents,
                         unit_cost_cents_exact, purchase_unit_id,
                         source_receipt_line_id)
                    VALUES (gen_random_uuid(), :tid, :iid, :cost, :exact, :puid, :lid)
                """),
                {
                    "tid": tenant_id,
                    "iid": item_id,
                    "cost": int(exact_cost.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
                    "exact": exact_cost.quantize(Decimal("0.0001")),
                    "puid": ln["purchase_unit_id"],
                    "lid": line_id,
                },
            )
        await session.execute(
            text(
                "UPDATE receipt_lines SET emits_movement_id = :mv_id "
                "WHERE tenant_id = :tid AND id = :lid"
            ),
            {"mv_id": mv_id, "tid": tenant_id, "lid": line_id},
        )
        movement_ids.append(mv_id)

    # ── 5. mark committed (stamps confirmed_at + affirmation for the trigger) ──
    await _mark_receipt_committed(
        session,
        tenant_id,
        receipt_id,
        confirm=confirm,
        reviewed_affirmation=reviewed_affirmation,
        source=source,
    )
    await session.flush()
    return {
        "receipt_id": receipt_id,
        "movement_ids": movement_ids,
        "status": "committed",
        "confirmed": confirm and source in _INTAKE_SOURCES,
    }
