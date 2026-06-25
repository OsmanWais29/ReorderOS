"""Trace + verify the depletion for one Clover order (the V7 end-to-end proof).

Run in the DO console (api component), from /srv:
    python -m scripts.trace_depletion                          # defaults to the order below
    CLOVER_ORDER_ID=3W9V62MQ043B2 python -m scripts.trace_depletion

Prints, for the order:
  (1) pos_event_inbox rows — vendor_event_type, state, processed_at, last_error
  (2) orders.state/payment_state + each sale_line_item name/qty/depletion_status/reason
  (3) inventory_movements ⋈ sale_line_items — name, movement_type, delta, idempotency_key

PASS requires ALL of: a processed lock event; order state=locked, payment_state=PAID;
all 10 lines depleted; exactly 10 sale_depletion rows with the expected per-ingredient
deltas. On FAIL it dumps fetched_payload (the real Clover JSON) so we can read which
field real Clover actually sent.
"""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal

from sqlalchemy import text

from app.core.service_db import get_service_sessionmaker

TENANT_ID = "aaa772e8-c714-4f74-945e-85fc13399f1d"
ORDER_ID = os.environ.get("CLOVER_ORDER_ID", "3W9V62MQ043B2")

EXPECTED: dict[str, Decimal] = {
    "bacon": Decimal("-25"),
    "beef patty": Decimal("-150"),
    "bun": Decimal("-1"),
    "cheddar": Decimal("-20"),
    "lettuce": Decimal("-15"),
    "onion": Decimal("-10"),
    "pickle": Decimal("-8"),
    "sauce": Decimal("-12"),
    "sesame seeds": Decimal("-2"),
    "tomato": Decimal("-30"),
}


async def main() -> None:
    sm = get_service_sessionmaker()
    async with sm() as s:
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_ID})

        # (1) inbox
        inbox = (
            await s.execute(
                text(
                    "SELECT vendor_event_type, state, processed_at, last_error"
                    " FROM pos_event_inbox WHERE tenant_id = :t AND vendor_event_id = :o"
                    " ORDER BY received_at"
                ),
                {"t": TENANT_ID, "o": ORDER_ID},
            )
        ).mappings().all()
        print(f"=== (1) pos_event_inbox for {ORDER_ID} ===")
        if not inbox:
            print("  NO inbox rows — the webhook never arrived for this order.")
        for r in inbox:
            print(f"  type={r['vendor_event_type']:<7} state={r['state']:<10} "
                  f"processed_at={r['processed_at']} err={r['last_error']}")

        # (2) order + lines
        lines = (
            await s.execute(
                text(
                    "SELECT o.state AS ostate, o.payment_state AS pstate,"
                    " sli.name_at_sale AS name, sli.quantity AS qty,"
                    " sli.depletion_status AS dstatus, sli.depletion_reason AS dreason"
                    " FROM orders o JOIN sale_line_items sli ON sli.order_id = o.id"
                    " WHERE o.clover_order_id = :o AND o.tenant_id = :t"
                    " ORDER BY sli.name_at_sale"
                ),
                {"t": TENANT_ID, "o": ORDER_ID},
            )
        ).mappings().all()
        print("\n=== (2) orders + sale_line_items ===")
        ostate = pstate = None
        if not lines:
            print("  NO order/sale_line_items — not ingested (state != locked at fetch?), "
                  "or depletion still running.")
        else:
            ostate, pstate = lines[0]["ostate"], lines[0]["pstate"]
            print(f"  order: state={ostate} payment_state={pstate}")
            for r in lines:
                print(f"    {r['name']:<14} qty={r['qty']} status={r['dstatus']} reason={r['dreason']}")

        # (3) movements
        movements = (
            await s.execute(
                text(
                    "SELECT ii.name AS name, im.movement_type AS mtype, im.delta AS delta,"
                    " im.idempotency_key AS key FROM inventory_movements im"
                    " JOIN sale_line_items sli ON sli.id = im.source_id"
                    " JOIN orders o ON o.id = sli.order_id"
                    " JOIN inventory_items ii ON ii.id = im.inventory_item_id"
                    " WHERE o.clover_order_id = :o AND im.tenant_id = :t"
                    " ORDER BY ii.name"
                ),
                {"t": TENANT_ID, "o": ORDER_ID},
            )
        ).mappings().all()
        print("\n=== (3) inventory_movements ===")
        for r in movements:
            print(f"  {r['name']:<14} {r['mtype']:<16} delta={r['delta']}  key={r['key']}")

        # ── verdict ──────────────────────────────────────────────────────────
        sale_depl = [m for m in movements if m["mtype"] == "sale_depletion"]
        got = {m["name"]: Decimal(str(m["delta"])) for m in sale_depl}
        checks = {
            "lock event processed": any(r["state"] == "processed" for r in inbox),
            "order state=locked": ostate == "locked",
            "payment_state=PAID": pstate == "PAID",
            "10 lines all depleted": len(lines) == 10
            and all(r["dstatus"] == "depleted" for r in lines),
            "exactly 10 sale_depletion": len(sale_depl) == 10,
            "deltas match expected": got == EXPECTED,
        }
        print("\n=== VERDICT ===")
        for name, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

        if all(checks.values()):
            print("\nPASS — depletion proven end-to-end on real Clover JSON.")
            return

        print("\nFAIL — diagnostics:")
        for name, exp in EXPECTED.items():
            g = got.get(name)
            if g != exp:
                print(f"  delta mismatch {name}: expected {exp} got {g}")
        extra = set(got) - set(EXPECTED)
        if extra:
            print(f"  unexpected movement names: {extra}")

        fp = (
            await s.execute(
                text(
                    "SELECT fetched_payload FROM pos_event_inbox"
                    " WHERE tenant_id = :t AND vendor_event_id = :o AND fetched_payload IS NOT NULL"
                    " ORDER BY received_at DESC LIMIT 1"
                ),
                {"t": TENANT_ID, "o": ORDER_ID},
            )
        ).scalar()
        print("\n--- fetched_payload (real Clover JSON) ---")
        if fp is None:
            print("  (none stored — if (2)/(3) are empty, the worker may not have run yet; re-run in ~20s)")
        else:
            try:
                obj = fp if isinstance(fp, dict) else json.loads(fp)
                print(json.dumps(obj, indent=2)[:5000])
            except Exception:  # noqa: BLE001 - diagnostic
                print(str(fp)[:5000])


if __name__ == "__main__":
    asyncio.run(main())
