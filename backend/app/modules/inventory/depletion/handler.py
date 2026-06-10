"""Per-line depletion handler (Sprint 5 Phase 9, v5 §11).

process_line is the single per-line entry point: process EXACTLY ONCE (terminal statuses
are a no-op on replay) → eligibility (resolver) → walk (read-only) → write movements +
terminal status. All writes happen within the CALLER's transaction; the worker owns the
per-line commit boundary. Compute-then-commit: failure paths write only a terminal status,
never partial ledger rows (nothing to roll back).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.inventory.depletion import walker
from app.modules.inventory.depletion.resolver import resolve_eligibility
from app.modules.inventory.depletion.writer import record_sale_reversal, write_movement


async def _maybe_late_signal_alert(
    session: AsyncSession,
    tenant_id: UUID,
    inventory_item_id: UUID,
    movement_id: UUID,
    recorded_at: datetime,
) -> None:
    """Late-signal detection (ported from the retired writer, v5 monitoring). Fires when a
    NEWLY WRITTEN movement's ingestion lag exceeds 30 min AND crosses a count-reconciliation
    boundary for that item. Called per written movement (gated on write_movement returning a
    non-None id), in the same transaction as the write — so a replay/skip never re-fires it,
    matching the old early-return semantics. The monitoring_alerts partial-unique
    (tenant_id, monitor_name) WHERE resolved_at IS NULL makes the upsert idempotent: no
    duplicate active row, only an alert_count bump."""
    now = datetime.now(UTC)
    if (now - recorded_at).total_seconds() <= 1800:
        return
    boundary = await session.execute(
        text("""
            SELECT 1 FROM inventory_count_events
             WHERE tenant_id = :tid AND inventory_item_id = :iid
               AND (
                   (reconciliation_cutoff_created_at IS NOT NULL
                    AND reconciliation_cutoff_created_at > :rec_at
                    AND reconciliation_cutoff_created_at <= :now)
                OR (reconciliation_cutoff_created_at IS NULL
                    AND counted_at > :rec_at AND counted_at <= :now)
               )
             LIMIT 1
        """),
        {"tid": tenant_id, "iid": inventory_item_id, "rec_at": recorded_at, "now": now},
    )
    if boundary.first() is None:
        return
    payload = json.dumps({
        "inventory_item_id": str(inventory_item_id),
        "movement_id": str(movement_id),
        "recorded_at": recorded_at.isoformat(),
        "created_at": now.isoformat(),
        "gap_seconds": round((now - recorded_at).total_seconds()),
    })
    await session.execute(
        text("""
            INSERT INTO monitoring_alerts
                (id, tenant_id, monitor_name, severity, trigger_payload)
            VALUES (gen_random_uuid(), :tid, 'late_signal_reconciliation', 'warn',
                    CAST(:payload AS jsonb))
            ON CONFLICT (tenant_id, monitor_name) WHERE resolved_at IS NULL
            DO UPDATE SET last_seen_at = now(),
                          alert_count = monitoring_alerts.alert_count + 1,
                          trigger_payload = EXCLUDED.trigger_payload
        """),
        {"tid": tenant_id, "payload": payload},
    )


async def _set_status(
    session: AsyncSession, tenant_id: UUID, sale_line_item_id: UUID, status: str, reason: str | None
) -> None:
    await session.execute(
        text(
            "UPDATE sale_line_items SET depletion_status = :s, depletion_reason = :r"
            " WHERE id = :sli AND tenant_id = :tid"
        ),
        {"s": status, "r": reason, "sli": sale_line_item_id, "tid": tenant_id},
    )


async def process_line(
    session: AsyncSession,
    tenant_id: UUID,
    sale_line_item_id: UUID,
    *,
    recorded_at: datetime | None = None,
    partial_refunds_enabled: bool = False,
) -> tuple[str, str | None]:
    """Process one already-inserted (pending/NULL) sale line to a terminal depletion
    state (Sprint 5 Phase 9, v5 §11). Compute-then-commit:

      eligibility (pure)  → ineligible: write failed+reason, no movements
      walk base           → read-only; non-deplete verdict: write status+reason, no movements
                            (ConversionError surfaces as failed/missing_conversion)
      write movements (ON CONFLICT) + status='depleted'   ← the only writes, atomic

    Writes within the CALLER's transaction — does NOT commit. The worker owns the per-line
    transaction boundary so one line's failure can't roll back another's depletion.
    Returns the terminal (status, reason)."""
    line = (
        await session.execute(
            text("""
                SELECT s.quantity AS qty, s.is_voided AS is_voided,
                       s.is_refunded AS is_refunded, s.recipe_version_id AS rv,
                       s.menu_item_id AS mid, s.depletion_status AS dep_status,
                       s.depletion_reason AS dep_reason, o.payment_state AS payment_state,
                       o.state AS order_state
                FROM sale_line_items s
                JOIN orders o ON o.id = s.order_id
                WHERE s.id = :sli AND s.tenant_id = :tid
            """),
            {"sli": sale_line_item_id, "tid": tenant_id},
        )
    ).mappings().fetchone()
    if line is None:
        raise ValueError(f"sale_line {sale_line_item_id} not found (tenant {tenant_id})")

    # Process EXACTLY ONCE: only NULL (old unprocessed row) or 'pending' is processable;
    # the four terminal statuses are a no-op on replay. This freezes the captured reason
    # against drift — e.g. a line recorded 'unmapped/recipe_draft' when ingested unconfirmed
    # must NOT be recomputed (and possibly re-reasoned) after the operator later confirms.
    # First processing wins; replay cannot rewrite a past sale's recorded outcome.
    if line["dep_status"] in ("depleted", "failed", "unmapped", "skipped"):
        return str(line["dep_status"]), line["dep_reason"]

    # ── eligibility (pure; no writes) ─────────────────────────────────────────
    verdict = resolve_eligibility(
        payment_state=line["payment_state"],
        order_state=line["order_state"],
        is_voided=line["is_voided"],
        is_refunded=line["is_refunded"],
        partial_refunds_enabled=partial_refunds_enabled,
    )
    if not verdict.eligible:
        await _set_status(session, tenant_id, sale_line_item_id, "failed", verdict.reason)
        return "failed", verdict.reason

    # ── walk base + modifiers as ONE compute phase (read-only; no writes yet) ──
    # A conversion failure in EITHER fails the whole line — base and modifier are one
    # atomic unit (the intra-line form of the 9/10 coupling): a line is depleted fully or
    # not at all, never base-depleted-while-modifier-failed (which would mark it depleted
    # while under-depleted).
    line_qty = Decimal(str(line["qty"]))
    base = await walker.walk_base(
        session, tenant_id, sale_line_item_id=sale_line_item_id,
        recipe_version_id=line["rv"], line_quantity=line_qty,
    )
    if base.status != "depleted":
        await _set_status(session, tenant_id, sale_line_item_id, base.status, base.reason)
        return base.status, base.reason
    mods = await walker.walk_modifiers(
        session, tenant_id, sale_line_item_id=sale_line_item_id, line_quantity=line_qty,
    )
    if mods.status != "depleted":  # modifier conversion failure → whole line, no base writes
        await _set_status(session, tenant_id, sale_line_item_id, mods.status, mods.reason)
        return mods.status, mods.reason

    # ── both succeeded → write all movements (base + modifier) + terminal status ──
    for mv in (*base.movements, *mods.movements):
        movement_id = await write_movement(
            session,
            tenant_id=tenant_id,
            idempotency_key=mv.idempotency_key,
            legacy_key=mv.legacy_key,
            inventory_item_id=mv.inventory_item_id,
            movement_type=mv.movement_type,
            delta=mv.delta,
            yield_factor_applied=mv.yield_factor_applied,
            source_type=mv.source_type,
            source_id=mv.source_id,
            recorded_at=recorded_at,
        )
        # Late-signal fires per NEWLY-written movement (non-None id), base OR modifier —
        # a movement is a movement for reconciliation; never on a replay/skip.
        if movement_id is not None and recorded_at is not None:
            await _maybe_late_signal_alert(
                session, tenant_id, mv.inventory_item_id, movement_id, recorded_at
            )
    await _set_status(session, tenant_id, sale_line_item_id, "depleted", None)
    return "depleted", None


async def reverse_line(
    session: AsyncSession,
    tenant_id: UUID,
    sale_line_item_id: UUID,
) -> int:
    """Reverse ALL forward depletion movements for one sale line (Sprint 5 Phase 11,
    ADR 0001 D2/D3/D4). A PURE LEDGER OPERATION — it reverses the forward movements that
    actually exist; it does NOT decide whether the line is refunded and it does NOT write
    is_refunded (the worker owns refund detection + that write, per ADR D2). It never walks
    recipes/modifiers (the guardrail): reversal negates the rows that were written against the
    version frozen at sale time, never recomputes from current state.

    Reads every forward movement for the line — base AND modifier, plus legacy-format keys —
    via the shared ``sale_line:{sli}:`` idempotency-key prefix, filtered to the forward sale
    types, and emits an arithmetic-negation reversal for each (record_sale_reversal: type-
    specific reversal movement_type, source linkage, yield_factor preserved, ON CONFLICT
    idempotent). Writes within the CALLER's transaction; does not commit — the worker calls
    this in the SAME per-line transaction as its is_refunded write, so a crash can never leave
    a line marked-but-unreversed or reversed-but-unmarked.

    Driven by movement EXISTENCE, not line status (ADR D4): a line that never depleted
    (unmapped/skipped/failed/pending) has no forward movements, so this is a no-op by
    construction — no error, no status change. A refunded 'depleted' line keeps its 'depleted'
    status (ADR D5); the reversal rows net it to zero. Returns the count of NEW reversal rows
    written (0 on replay or no forward movement)."""
    forward = (
        await session.execute(
            text("""
                SELECT id, inventory_item_id
                FROM inventory_movements
                WHERE tenant_id = :tid
                  AND movement_type IN ('sale_depletion', 'sale_signal')
                  AND idempotency_key LIKE 'sale_line:' || :sli || ':%'
            """),
            {"tid": tenant_id, "sli": str(sale_line_item_id)},
        )
    ).mappings().all()

    reversed_count = 0
    for mv in forward:
        new_id = await record_sale_reversal(
            session,
            tenant_id=tenant_id,
            original_movement_id=mv["id"],
            inventory_item_id=mv["inventory_item_id"],
        )
        if new_id is not None:
            reversed_count += 1
    return reversed_count
