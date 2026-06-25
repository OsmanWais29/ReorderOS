"""In-container V7 precondition check — run BEFORE ringing the sandbox sale.

Run in the DigitalOcean App Platform Console (api component), from /srv:
    python -m scripts.precondition_check

Replicates inventory/services.py::on_hand for Mode A (recipe_deducted):
on_hand = SUM(delta) over all movement_types EXCEPT sale_signal/sale_signal_reversal.

Per ingredient prints: name, inventory_mode, recipe_qty, recipe_unit, storage_unit,
on_hand, verdict. GREEN requires ALL of:
  - inventory_mode = recipe_deducted (so on_hand is a straight ledger sum)
  - storage_unit = recipe_unit       (so depletion's convert() is identity => the
                                       movement equals the recipe qty exactly)
  - on_hand > recipe_qty             (one burger leaves positive stock)
Ends with "N/10 GREEN" (or lists the RED rows).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import text

from app.core.service_db import get_service_sessionmaker

TENANT_ID = "aaa772e8-c714-4f74-945e-85fc13399f1d"
MENU_ITEM_ID = "4395d6dc-bbc1-4557-9801-7aaca0c37e14"

_QUERY = """
SELECT ii.name            AS name,
       ii.inventory_mode  AS mode,
       ri.quantity        AS recipe_qty,
       ri.unit            AS recipe_unit,
       uom.name           AS storage_unit,
       COALESCE((
           SELECT SUM(m.delta) FROM inventory_movements m
           WHERE m.tenant_id = ri.tenant_id
             AND m.inventory_item_id = ri.inventory_item_id
             AND m.movement_type NOT IN ('sale_signal', 'sale_signal_reversal')
       ), 0) AS on_hand
FROM recipe_ingredients ri
JOIN inventory_items ii  ON ii.id = ri.inventory_item_id
JOIN units_of_measure uom ON uom.id = ii.storage_unit_id
WHERE ri.recipe_version_id = :rv AND ri.tenant_id = :t
ORDER BY ii.name
"""


async def main() -> None:
    sm = get_service_sessionmaker()
    async with sm() as s:
        # service_worker reads are tenant-scoped (RLS) — set the context first.
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_ID})

        rv = (
            await s.execute(
                text("SELECT recipe_version_id FROM menu_items WHERE id = :mi AND tenant_id = :t"),
                {"mi": MENU_ITEM_ID, "t": TENANT_ID},
            )
        ).scalar()
        if rv is None:
            print("RED (gate): menu_item has NO recipe_version_id — recipe not confirmed; a sale "
                  "would mark the event processed and move ZERO stock. Confirm the recipe first.")
            return

        rows = (
            await s.execute(text(_QUERY), {"rv": rv, "t": TENANT_ID})
        ).mappings().all()

    if not rows:
        print("RED (gate): recipe_version is set but has NO recipe_ingredients.")
        return

    header = (
        f"{'name':<14}{'mode':<17}{'recipe_qty':>11}{'unit':>6}{'storage':>9}"
        f"{'on_hand':>11}  verdict"
    )
    print(header)
    print("-" * (len(header) + 6))

    green = 0
    reds: list[tuple[str, str]] = []
    for r in rows:
        recipe_qty = Decimal(str(r["recipe_qty"]))
        on_hand = Decimal(str(r["on_hand"]))
        mode_ok = r["mode"] == "recipe_deducted"
        unit_ok = r["recipe_unit"] == r["storage_unit"]
        stock_ok = on_hand > recipe_qty

        if mode_ok and unit_ok and stock_ok:
            verdict = "GREEN"
            green += 1
        else:
            reasons = []
            if not mode_ok:
                reasons.append(f"mode={r['mode']}")
            if not unit_ok:
                reasons.append(f"unit {r['recipe_unit']}!=storage {r['storage_unit']}")
            if not stock_ok:
                reasons.append(f"on_hand {on_hand}<=recipe {recipe_qty}")
            verdict = "RED: " + ", ".join(reasons)
            reds.append((str(r["name"]), verdict))

        print(
            f"{str(r['name']):<14}{str(r['mode']):<17}{recipe_qty:>11}{str(r['recipe_unit']):>6}"
            f"{str(r['storage_unit']):>9}{on_hand:>11}  {verdict}"
        )

    total = len(rows)
    print()
    if green == total:
        print(f"{green}/{total} GREEN — ready to ring the sale.")
    else:
        print(f"{green}/{total} GREEN — NOT READY. RED rows:")
        for name, v in reds:
            print(f"  - {name}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
