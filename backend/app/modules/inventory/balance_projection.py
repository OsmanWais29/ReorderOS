"""Canonical inventory-balance projection (PR-A1).

The on-hand balance is computed in exactly ONE place — `on_hand()` in
inventory/services.py. This module does NOT add a fourth formula:

- `current_balance()` delegates verbatim to `on_hand()` (historical path). It is
  the single-item entry point the insights endpoint uses.
- `current_balance_batch()` is the set form (one query, no N+1). A characterization
  test (`test_balance_projection_characterization.py`) proves it is Decimal-
  identical to `on_hand()` for every item across the fixture matrix.

  NOTE (PR-A1 condition 2 — list NOT consolidated): the GET /inventory/items
  inline SQL is a THIRD implementation that is NOT identical to on_hand() for
  count_anchored items — it uses `SUM(ABS(delta))` over `sale_signal` ONLY, while
  on_hand() sums signed `delta` over `sale_signal` AND `sale_signal_reversal`.
  They diverge whenever a Mode-B item has a signal reversal. Switching the list
  onto this projection would therefore SILENTLY CHANGE what the Stock list shows.
  Because the three implementations are not identical today, we do NOT consolidate
  the list in PR-A1; `current_balance_batch()` exists and is proven-equal to
  on_hand() so a dedicated later PR can reconcile the list's Mode-B meaning and
  switch it deliberately. Insights adds NO new balance formula — it delegates to
  on_hand() via `current_balance()`.
- `balance_before_mode_a()` is the SAME Mode-A canonical rule (Σ of every
  movement delta except sale signals) evaluated at an upper time bound. For a
  recipe_deducted item, on-hand IS that unbounded sum, so the bounded form is the
  one canonical formula at a boundary, not a new one — the characterization test
  asserts `balance_before_mode_a(before=+∞) == on_hand()` for Mode A items. It is
  defined ONLY for Mode A; for count_anchored items it returns None and the
  insights reconciliation reports RECONCILIATION_UNAVAILABLE (a time-bounded
  count-anchor balance is not built in PR-A1 — safe consolidation pending).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.inventory.services import on_hand

# Movement types that DECREMENT/INCREMENT the recipe_deducted ledger — i.e. every
# type except the count-anchored sale SIGNALS. This is the single source of the
# Mode-A inclusion rule, mirrored from on_hand()'s ledger_sum CTE.
_MODE_A_EXCLUDED = ("sale_signal", "sale_signal_reversal")


async def current_balance(
    session: AsyncSession, *, tenant_id: UUID, inventory_item_id: UUID
) -> Decimal | None:
    """Authoritative current on-hand for one item — delegates to on_hand()."""
    return await on_hand(session, tenant_id=tenant_id, inventory_item_id=inventory_item_id)


async def current_balance_batch(
    session: AsyncSession, *, tenant_id: UUID, item_ids: list[UUID]
) -> dict[UUID, Decimal | None]:
    """Set form of the historical on-hand SQL — one query for N items.

    Decimal-identical to calling on_hand() per item (characterization-proven).
    Returns {item_id: qty|None}; None for a count_anchored item with no count.
    """
    if not item_ids:
        return {}
    sql = """
        WITH items AS (
            SELECT id, inventory_mode, last_count_at, last_count_quantity
              FROM inventory_items
             WHERE tenant_id = :tid AND id = ANY(:ids)
        ),
        ledger AS (
            SELECT m.inventory_item_id AS iid, COALESCE(SUM(m.delta), 0) AS qty
              FROM inventory_movements m
             WHERE m.tenant_id = :tid AND m.inventory_item_id = ANY(:ids)
               AND m.movement_type NOT IN ('sale_signal','sale_signal_reversal')
             GROUP BY m.inventory_item_id
        ),
        receipts AS (
            SELECT m.inventory_item_id AS iid, COALESCE(SUM(m.delta), 0) AS qty
              FROM inventory_movements m
              JOIN items i ON i.id = m.inventory_item_id
             WHERE m.tenant_id = :tid
               AND m.recorded_at > i.last_count_at
               AND m.movement_type IN ('receive','transfer_in','count_adjust','opening_balance')
             GROUP BY m.inventory_item_id
        ),
        signals AS (
            SELECT m.inventory_item_id AS iid, COALESCE(SUM(m.delta), 0) AS qty
              FROM inventory_movements m
              JOIN items i ON i.id = m.inventory_item_id
             WHERE m.tenant_id = :tid
               AND m.recorded_at > i.last_count_at
               AND m.movement_type IN ('sale_signal','sale_signal_reversal')
             GROUP BY m.inventory_item_id
        ),
        yf AS (
            SELECT inventory_item_id AS iid, yield_factor
              FROM inventory_yield_factors
             WHERE tenant_id = :tid AND inventory_item_id = ANY(:ids)
        )
        SELECT i.id AS iid,
               CASE
                   WHEN i.inventory_mode = 'recipe_deducted'
                       THEN COALESCE(ledger.qty, 0)
                   WHEN i.inventory_mode = 'count_anchored'
                        AND i.last_count_quantity IS NOT NULL
                       THEN i.last_count_quantity
                            + COALESCE(receipts.qty, 0)
                            - (COALESCE(signals.qty, 0) * COALESCE(yf.yield_factor, 1.0))
                   ELSE NULL
               END AS qty
          FROM items i
          LEFT JOIN ledger   ON ledger.iid   = i.id
          LEFT JOIN receipts ON receipts.iid = i.id
          LEFT JOIN signals  ON signals.iid  = i.id
          LEFT JOIN yf        ON yf.iid       = i.id
    """
    rows = (
        await session.execute(text(sql), {"tid": tenant_id, "ids": item_ids})
    ).all()
    out: dict[UUID, Decimal | None] = {}
    for iid, qty in rows:
        out[iid] = Decimal(str(qty)) if qty is not None else None
    return out


async def balance_before_mode_a(
    session: AsyncSession, *, tenant_id: UUID, inventory_item_id: UUID, before: datetime
) -> Decimal | None:
    """Mode-A on-hand as of an upper time bound (created_at < before).

    The canonical Mode-A rule (Σ delta excluding sale signals) evaluated at a
    boundary. Returns None for a count_anchored item (a time-bounded count-anchor
    balance is not built in PR-A1). Used for the reconciliation's
    balance_at_window_start on recipe_deducted items only.
    """
    mode = (
        await session.execute(
            text("SELECT inventory_mode FROM inventory_items WHERE tenant_id = :tid AND id = :iid"),
            {"tid": tenant_id, "iid": inventory_item_id},
        )
    ).scalar_one_or_none()
    if mode != "recipe_deducted":
        return None
    qty = (
        await session.execute(
            text("""
                SELECT COALESCE(SUM(delta), 0)
                  FROM inventory_movements
                 WHERE tenant_id = :tid AND inventory_item_id = :iid
                   AND movement_type NOT IN ('sale_signal','sale_signal_reversal')
                   AND created_at < :before
            """),
            {"tid": tenant_id, "iid": inventory_item_id, "before": before},
        )
    ).scalar_one()
    return Decimal(str(qty))
