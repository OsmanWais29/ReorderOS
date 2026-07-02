"""
V7 Tier-2 — staging Clover-ingestion SIMULATION (founder-runnable, no Clover account).

WHAT IT DOES
    Seeds one tenant + one recipe-deducted ingredient + one menu item, then drops a
    Clover-shaped "locked" order into the STAGING `pos_event_inbox` with a *pre-fetched*
    payload. Because `worker.process_event` short-circuits on `fetched_payload`
    (app/modules/pos/worker.py:149), the LIVE staging inbox-worker processes it WITHOUT
    calling Clover's API — exercising the real parser -> order upsert -> line insert ->
    depletion ledger path against real staging infra. Then it polls and prints the
    5-hop trace and a PASS/FAIL.

WHAT IT PROVES
    The staging deployment's worker is alive and the parse+depletion logic runs
    end-to-end on real Postgres (SSL, schema 0021, env wiring).

WHAT IT DOES NOT PROVE (needs the Tier-3 real sandbox sale)
    - That REAL Clover JSON matches our parser (this uses OUR authored payload shape).
    - OAuth v2 connect / token decrypt / webhook X-Clover-Auth match.
    - service_worker grants + RLS — ONLY if staging's SERVICE_DATABASE_URL still points
      at doadmin (the setup fallback). If so, the worker bypasses RLS and this sim won't
      catch a missing grant. Switch SERVICE_DATABASE_URL to the service_worker role to
      close that gap.

USAGE  (run locally, pointed at staging — never paste the URL into chat):
    export STAGING_DB_URL='postgresql://doadmin:<pw>@<host>:25060/defaultdb?sslmode=require'
    python tests/staging_sim.py
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
import time
import uuid
from decimal import Decimal

import asyncpg

RECIPE_QTY = "2.0"  # grams of the ingredient per 1 unit of the menu item
LINE_QTY = 3  # units sold in the simulated order
EXPECT_DELTA = Decimal(RECIPE_QTY) * LINE_QTY * -1  # -6.0 (sale_depletion is negative)
POLL_SECONDS = int(os.environ.get("SIM_POLL_SECONDS", "60"))


def _dsn() -> str:
    raw = os.environ.get("STAGING_DB_URL")
    if not raw:
        sys.exit("ERROR: set STAGING_DB_URL to the staging doadmin connection string")
    # asyncpg wants a bare DSN: drop the SQLAlchemy driver tag and libpq query args.
    return raw.replace("postgresql+asyncpg://", "postgresql://").split("?")[0]


async def _connect() -> asyncpg.Connection:
    dsn = _dsn()
    # Local dev Postgres has no SSL; DO managed PG requires it (match the app's
    # production CERT_NONE handling). Pick by host.
    if "localhost" in dsn or "127.0.0.1" in dsn:
        return await asyncpg.connect(dsn)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return await asyncpg.connect(dsn, ssl=ctx)


async def seed(conn: asyncpg.Connection) -> dict:
    tid = uuid.uuid4()
    h = tid.hex[:8]
    await conn.execute(
        "INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)",
        tid,
        f"v7sim-{h}",
        f"V7Sim_{h}",
    )
    uom = await conn.fetchval(
        "INSERT INTO units_of_measure (tenant_id,name,abbreviation,unit_type)"
        " VALUES ($1,'g','g','weight') RETURNING id",
        tid,
    )
    inv = await conn.fetchval(
        "INSERT INTO inventory_items"
        " (tenant_id,name,inventory_mode,storage_unit_id,storage_to_recipe_factor)"
        " VALUES ($1,$2,'recipe_deducted',$3,1.0) RETURNING id",
        tid,
        f"flour-{h}",
        uom,
    )
    pos_item_id = f"SIMITEM_{h}"
    mi = await conn.fetchval(
        "INSERT INTO menu_items (tenant_id,pos_item_id,name) VALUES ($1,$2,$3) RETURNING id",
        tid,
        pos_item_id,
        f"Sim Burger {h}",
    )
    rec = await conn.fetchval(
        "INSERT INTO recipes (tenant_id,menu_item_id,status) VALUES ($1,$2,'confirmed') RETURNING id",
        tid,
        mi,
    )
    rv = await conn.fetchval(
        "INSERT INTO recipe_versions (tenant_id,recipe_id,version_number,yield_quantity)"
        " VALUES ($1,$2,1,1) RETURNING id",
        tid,
        rec,
    )
    await conn.execute(
        "INSERT INTO recipe_ingredients"
        " (tenant_id,recipe_version_id,inventory_item_id,quantity,unit)"
        " VALUES ($1,$2,$3,$4::numeric,'g')",
        tid,
        rv,
        inv,
        RECIPE_QTY,
    )
    await conn.execute("UPDATE menu_items SET recipe_version_id=$1 WHERE id=$2", rv, mi)

    # The Clover-shaped order — what clover.get_order() would return. state MUST be 'locked'
    # or the worker treats it as an open order and skips depletion (worker.py:217).
    order_id = f"order_{h}"
    li_id = f"li_{h}"
    now_ms = int(time.time() * 1000)
    order = {
        "id": order_id,
        "state": "locked",
        "paymentState": "PAID",
        "total": 4200,
        "createdTime": now_ms - 60000,
        "modifiedTime": now_ms,
        "lineItems": {
            "elements": [
                {
                    "id": li_id,
                    "item": {"id": pos_item_id},
                    "name": "Sim Burger",
                    "price": 1400,
                    "unitQty": LINE_QTY,
                    "discountAmount": 0,
                },
            ]
        },
    }
    inbox_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO pos_event_inbox"
        " (inbox_id,tenant_id,connection_id,vendor,vendor_event_id,vendor_object_type,"
        "  vendor_event_type,vendor_ts,raw_payload,signature_verified,source,state,"
        "  fetched_payload,fetched_at)"
        " VALUES ($1,$2,NULL,'clover',$3,'O','UPDATE',$4,$5::jsonb,true,'webhook','pending',"
        "         $6::jsonb,now())",
        inbox_id,
        tid,
        order_id,
        now_ms,
        json.dumps({"sim": True}),
        json.dumps(order),
    )
    return {
        "tid": tid,
        "inv": inv,
        "li_id": li_id,
        "order_id": order_id,
        "inbox_id": inbox_id,
        "pos_item_id": pos_item_id,
    }


async def wait_for_worker(conn: asyncpg.Connection, s: dict) -> str:
    deadline = time.time() + POLL_SECONDS
    while time.time() < deadline:
        state = await conn.fetchval(
            "SELECT state FROM pos_event_inbox WHERE inbox_id=$1",
            s["inbox_id"],
        )
        if state in ("processed", "failed"):
            return state
        await asyncio.sleep(2)
    return "timeout"


async def report(conn: asyncpg.Connection, s: dict, final_state: str) -> bool:
    print("\n──────── V7 staging-sim trace ────────")
    print(f"tenant_id = {s['tid']}")
    # hop 1-2: inbox row
    row = await conn.fetchrow(
        "SELECT state, retry_count, last_error FROM pos_event_inbox WHERE inbox_id=$1",
        s["inbox_id"],
    )
    print(
        f"1-2 inbox       : state={row['state']} retries={row['retry_count']} "
        f"err={row['last_error']!r}"
    )
    # hop 3: order
    o = await conn.fetchrow(
        "SELECT state, payment_state, total_amount_cents FROM orders"
        " WHERE tenant_id=$1 AND clover_order_id=$2",
        s["tid"],
        s["order_id"],
    )
    print(f"3   order       : {dict(o) if o else 'MISSING'}")
    # hop 4: line item
    li = await conn.fetchrow(
        "SELECT quantity, depletion_status, depletion_reason FROM sale_line_items"
        " WHERE tenant_id=$1 AND clover_line_item_id=$2",
        s["tid"],
        s["li_id"],
    )
    print(f"4   line        : {dict(li) if li else 'MISSING'}")
    # hop 5: inventory movement (the depletion ledger)
    mv = await conn.fetch(
        "SELECT movement_type, delta, source_type FROM inventory_movements"
        " WHERE tenant_id=$1 AND inventory_item_id=$2 ORDER BY created_at",
        s["tid"],
        s["inv"],
    )
    print(f"5   movements   : {[dict(m) for m in mv]}")
    print(f"    expected delta = {EXPECT_DELTA} (recipe {RECIPE_QTY}g x {LINE_QTY} units)")

    ok = (
        final_state == "processed"
        and li is not None
        and li["depletion_status"] == "depleted"
        and len(mv) == 1
        and mv[0]["movement_type"] == "sale_depletion"
        and mv[0]["delta"] == EXPECT_DELTA
    )
    print(
        "\nRESULT:",
        "✅ PASS — staging worker depleted correctly" if ok else "❌ FAIL — see trace above",
    )
    return ok


async def main() -> int:
    conn = await _connect()
    try:
        s = await seed(conn)
        print(
            f"seeded inbox event {s['inbox_id']} (order {s['order_id']}); "
            f"waiting up to {POLL_SECONDS}s for the live staging worker…"
        )
        final_state = await wait_for_worker(conn, s)
        ok = await report(conn, s, final_state)
        return 0 if ok else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
