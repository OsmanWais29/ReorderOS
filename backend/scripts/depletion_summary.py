"""Cross-order no-double-count proof: group ALL sale_depletion movements for the
tenant by ingredient and check each was depleted exactly twice (two distinct burger
sales) with total_delta = -2 x recipe qty.

Run in the DO console (api component), from /srv:
    python -m scripts.depletion_summary

PASS  = every ingredient has exactly 2 depletions and the expected -2x total.
FAIL  = 3+ depletions on any ingredient (a duplicate double-counted), a wrong total,
        a missing ingredient, or an unexpected one.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import text

from app.core.service_db import get_service_sessionmaker

TENANT_ID = "aaa772e8-c714-4f74-945e-85fc13399f1d"
EXPECTED_DEPLETIONS = 2  # two distinct identical orders

# -2 x recipe qty (one burger each, two orders)
EXPECTED_TOTAL: dict[str, Decimal] = {
    "bacon": Decimal("-50"),
    "beef patty": Decimal("-300"),
    "bun": Decimal("-2"),
    "cheddar": Decimal("-40"),
    "lettuce": Decimal("-30"),
    "onion": Decimal("-20"),
    "pickle": Decimal("-16"),
    "sauce": Decimal("-24"),
    "sesame seeds": Decimal("-4"),
    "tomato": Decimal("-60"),
}


async def main() -> None:
    sm = get_service_sessionmaker()
    async with sm() as s:
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_ID})
        rows = (
            await s.execute(
                text(
                    "SELECT ii.name AS name, count(*) AS depletions, sum(im.delta) AS total_delta"
                    " FROM inventory_movements im"
                    " JOIN inventory_items ii ON ii.id = im.inventory_item_id"
                    " WHERE im.tenant_id = :t AND im.movement_type = 'sale_depletion'"
                    " GROUP BY ii.name ORDER BY ii.name"
                ),
                {"t": TENANT_ID},
            )
        ).mappings().all()

    print(f"{'name':<14}{'depletions':>11}{'total_delta':>13}  verdict")
    print("-" * 54)

    all_ok = True
    seen: set[str] = set()
    for r in rows:
        name = str(r["name"])
        seen.add(name)
        dep = int(r["depletions"])
        total = Decimal(str(r["total_delta"]))
        exp = EXPECTED_TOTAL.get(name)

        if exp is None:
            verdict = "FAIL: unexpected ingredient"
        elif dep > EXPECTED_DEPLETIONS:
            verdict = f"FAIL: {dep} depletions — DOUBLE-COUNTED"
        elif dep < EXPECTED_DEPLETIONS:
            verdict = f"FAIL: only {dep} depletion(s)"
        elif total != exp:
            verdict = f"FAIL: total {total} != {exp}"
        else:
            verdict = "PASS"
        if verdict != "PASS":
            all_ok = False
        print(f"{name:<14}{dep:>11}{str(total):>13}  {verdict}")

    for missing in sorted(set(EXPECTED_TOTAL) - seen):
        all_ok = False
        print(f"{missing:<14}{0:>11}{'-':>13}  FAIL: no depletions")

    print()
    if all_ok:
        print("OVERALL: PASS — two distinct sales, exactly 2 depletions each, no double-count.")
    else:
        print("OVERALL: FAIL — see rows above.")


if __name__ == "__main__":
    asyncio.run(main())
