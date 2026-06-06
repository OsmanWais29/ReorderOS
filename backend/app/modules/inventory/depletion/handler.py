"""Inventory effect dispatch (Sprint 5 Phase 7 — Commit A relocation).

PURE REFACTOR: emit_inventory_effects was formerly a private dispatch method on
InboxWorker (pos/worker.py), lifted to a module function with identical behavior. The
method referenced no instance state (no `self.*`), so the lift is a clean signature
change (drop `self`) with the body unchanged. Commit B (Phase 9) replaces this dispatch
with the walker/resolver; until then it is behaviorally identical to the old method.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.modules.inventory.depletion.writer import record_sale_inventory_effect


async def emit_inventory_effects(
    session: Any,
    tenant_id: str,
    sale_line_item_id: str,
    vendor_ts: datetime | None,
) -> None:
    """Call record_sale_inventory_effect for every recipe ingredient on the line.

    Skips gracefully when the sale line has no recipe (menu_item not yet
    catalogued, or item not linked to any recipe version).  Each call is
    idempotent, so safe to retry on a replay.
    """
    rows = (
        await session.execute(
            text("""
                SELECT ri.inventory_item_id
                  FROM sale_line_items s
                  JOIN recipe_ingredients ri
                    ON ri.recipe_version_id = s.recipe_version_id
                 WHERE s.tenant_id = :tid AND s.id = :sli
            """),
            {"tid": tenant_id, "sli": sale_line_item_id},
        )
    ).fetchall()

    for row in rows:
        await record_sale_inventory_effect(
            session,
            tenant_id=UUID(tenant_id),
            sale_line_item_id=UUID(sale_line_item_id),
            inventory_item_id=UUID(str(row[0])),
            recorded_at=vendor_ts,
        )
