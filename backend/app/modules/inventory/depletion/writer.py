"""Inventory ledger movement writers (Sprint 5 Phase 9, v5 §11).

write_movement — the Phase 9 base/modifier movement primitive: atomic idempotency via
INSERT ... ON CONFLICT (tenant_id, idempotency_key) DO NOTHING, plus a legacy-key guard
(reads the old sale_line:{sli}:{ii} format to skip pre-existing legacy rows; never writes
it). record_sale_reversal — reverses a sale movement (unchanged; wired in Phase 11).

All functions accept an AsyncSession and never commit: the caller owns the transaction.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def write_movement(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    idempotency_key: str,
    legacy_key: str | None,
    inventory_item_id: UUID,
    movement_type: str,
    delta: Decimal,
    yield_factor_applied: Decimal,
    source_type: str,
    source_id: UUID,
    recorded_at: datetime | None = None,
) -> UUID | None:
    """Write one ledger movement, idempotently and race-safely (Sprint 5 Phase 9).

    Concurrency: the new-key write is ``INSERT ... ON CONFLICT (tenant_id, idempotency_key)
    DO NOTHING`` — the UNIQUE(tenant_id, idempotency_key) constraint is the arbiter, so two
    workers processing the same sale (duplicate webhook / retry overlap) cannot both insert.
    This replaces the old check-then-insert, which had a TOCTOU double-depletion race.

    Legacy-key guard (phase-map edit 3): before writing, skip if a movement already exists
    under the legacy ``sale_line:{sli}:{ii}`` key. This check-then-act is safe ONLY because
    no code writes legacy-format keys anymore (Phase 7 retired the old writer), so the legacy
    key set is frozen — there is no concurrent writer to race against. The sole concurrent
    write is the new-key INSERT above, which is atomic.

    Returns the new movement id if a row was written, or None if skipped (legacy present or
    new-key conflict). The id is the "a movement was newly written" signal — the caller
    gates write-coupled side effects (e.g. late-signal detection) on a non-None return, so
    a replay/skip never re-fires them (matching the old early-return-on-existing semantics).
    """
    if legacy_key is not None:
        legacy = await session.execute(
            text(
                "SELECT 1 FROM inventory_movements"
                " WHERE tenant_id = :tid AND idempotency_key = :key"
            ),
            {"tid": tenant_id, "key": legacy_key},
        )
        if legacy.first() is not None:
            return None

    res = await session.execute(
        text("""
            INSERT INTO inventory_movements
                (id, tenant_id, inventory_item_id, movement_type, delta,
                 source_type, source_id, idempotency_key,
                 yield_factor_applied, recorded_at)
            VALUES (gen_random_uuid(), :tid, :iid, :mtype, :delta,
                    :stype, :sid, :key,
                    :yf, COALESCE(:rec_at, NOW()))
            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
            RETURNING id
        """),
        {
            "tid": tenant_id,
            "iid": inventory_item_id,
            "mtype": movement_type,
            "delta": delta,
            "stype": source_type,
            "sid": source_id,
            "key": idempotency_key,
            "yf": yield_factor_applied,
            "rec_at": recorded_at,
        },
    )
    row = res.first()
    return row[0] if row is not None else None


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
