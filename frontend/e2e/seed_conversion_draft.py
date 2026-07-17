"""Seed the conversion-review Playwright scenario. SELECT/INSERT on the LOCAL
test database only (DATABASE_URL, default localhost:5433) — never staging/prod.

Usage: python e2e/seed_conversion_draft.py <tenant_id>
Prints JSON: {"receipt_id": ..., "syrup_line_id": ..., "ready_line_id": ...}

Shape = the live-cert failure: one ready line (LAIT, unit L, identity) and one
blocker (SIROP VANILLE 750ML, 6 ea against an L-tracked item, 750 ml pack hint
→ server suggests 1 ea = 0.75 L → 4.5 L).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import asyncpg

DB = (
    os.environ.get("DATABASE_URL", "postgresql+asyncpg://reorderos:reorderos@localhost:5433/reorderos")
    .replace("postgresql+asyncpg://", "postgresql://")
    .split("?")[0]
)


async def main(tenant_id: str) -> None:
    tid = uuid.UUID(tenant_id)
    c = await asyncpg.connect(DB)
    # Idempotent across runs — the dev-local tenant persists between test runs.
    litre = await c.fetchval(
        "SELECT id FROM units_of_measure WHERE tenant_id = $1 AND name = 'L'", tid
    ) or await c.fetchval(
        "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
        "VALUES ($1, 'L', 'L', 'volume') RETURNING id",
        tid,
    )

    async def item(name: str) -> uuid.UUID:
        existing = await c.fetchval(
            "SELECT id FROM inventory_items WHERE tenant_id = $1 AND name = $2", tid, name
        )
        if existing:
            return existing
        return await c.fetchval(
            "INSERT INTO inventory_items (tenant_id, name, inventory_mode, storage_unit_id, "
            "recipe_unit_id) VALUES ($1, $2, 'recipe_deducted', $3, $3) RETURNING id",
            tid, name, litre,
        )

    syrup = await item("SIROP VANILLE")
    milk = await item("LAIT 3.25%")
    # Forget any remembered ea→SIROP factor from a prior run — the suggestion
    # must come from the 750 ml pack evidence, not memory.
    await c.execute(
        "DELETE FROM tenant_item_purchase_conversions "
        "WHERE tenant_id = $1 AND inventory_item_id = $2",
        tid, syrup,
    )
    rid = await c.fetchval(
        "INSERT INTO receipts (tenant_id, commit_state, source, supplier_name, "
        "extraction_status) VALUES ($1, 'draft', 'email', 'Lauzon E2E', 'complete') RETURNING id",
        tid,
    )
    ready = await c.fetchval(
        "INSERT INTO receipt_lines (tenant_id, receipt_id, inventory_item_id, "
        "received_quantity, extracted_unit, extracted_name, match_status, unit_cost_cents, "
        "line_total_cents, line_ordinal) "
        "VALUES ($1, $2, $3, 10, 'L', 'LAIT 3.25% 10L', 'matched', 172, 1720, 0) RETURNING id",
        tid, rid, milk,
    )
    blocker = await c.fetchval(
        "INSERT INTO receipt_lines (tenant_id, receipt_id, inventory_item_id, "
        "received_quantity, extracted_unit, extracted_name, match_status, unit_cost_cents, "
        "line_total_cents, pack_size_qty, pack_size_unit, line_ordinal) "
        "VALUES ($1, $2, $3, 6, 'ea', 'SIROP VANILLE 750ML', 'matched', 895, 5370, "
        "750, 'ml', 1) RETURNING id",
        tid, rid, syrup,
    )
    promo = await c.fetchval(
        "INSERT INTO receipt_lines (tenant_id, receipt_id, line_type, match_status, "
        "extracted_name, line_total_cents, line_ordinal) "
        "VALUES ($1, $2, 'discount', 'skipped', 'PROMO SIROP -10%', -537, 2) RETURNING id",
        tid, rid,
    )
    await c.close()
    print(json.dumps({
        "receipt_id": str(rid),
        "syrup_line_id": str(blocker),
        "ready_line_id": str(ready),
    }))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
