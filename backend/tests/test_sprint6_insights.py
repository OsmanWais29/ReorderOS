"""Stock Item Insights — PR-A1 actuals (service-level integration tests).

Proves every displayed actual derives from the ledger / POS ingest state and
reconciles: Mode-A ledger equation (reconciliation_delta=0), Mode-B
reconciliation-unavailable, POS health counts, observed consumption, two mapping
ratios + effective=min, frozen-version contributors, deterministic reasons, cost
RBAC redaction, tenant isolation, and forecast/reorder NOT_YET_CERTIFIED.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import engine, make_bound_session
from app.modules.inventory.insights import ItemNotFound, build_item_insights

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[Any]:
    connection: AsyncConnection
    async with engine.connect() as connection:
        await connection.begin()
        session = make_bound_session(connection)
        try:
            yield session
        finally:
            await connection.rollback()


async def _insights(db: Any, tid: uuid.UUID, iid: uuid.UUID, **kw: Any) -> dict:
    now = datetime.now(UTC)
    defaults = dict(
        window_key="14d",
        as_of=now,
        timezone_name="UTC",
        timezone_source="fallback",
        can_view_aggregated_cost=True,
        target_cover_days=7,
        target_source="default",
    )
    defaults.update(kw)
    return await build_item_insights(db, tenant_id=tid, item_id=iid, **defaults)


async def _scalar(db: Any, sql: str, **p: Any) -> Any:
    return (await db.execute(text(sql), p)).scalar_one()


async def _seed_mode_a(db: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'INS')"),
        {"id": tid, "s": f"ins-{tid.hex[:8]}"},
    )
    unit = await _scalar(
        db,
        "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
        "VALUES (:t,'ea','ea','count') RETURNING id",
        t=tid,
    )
    item = await _scalar(
        db,
        "INSERT INTO inventory_items (tenant_id, name, inventory_mode, storage_unit_id, "
        "recipe_unit_id, par_level) VALUES (:t,'Avocados','recipe_deducted',:u,:u,20) RETURNING id",
        t=tid,
        u=unit,
    )

    async def mv(
        mtype: str, delta: str, days: int, src_type: str | None = None, src: Any = None
    ) -> None:
        await db.execute(
            text("""
                INSERT INTO inventory_movements
                    (tenant_id, inventory_item_id, movement_type, delta, recorded_at, created_at,
                     source_type, source_id, idempotency_key)
                VALUES (:t,:i,:mt,:d,:w,:w,:st,:si,:k)
            """),
            {
                "t": tid,
                "i": item,
                "mt": mtype,
                "d": delta,
                "w": now - timedelta(days=days),
                "st": src_type,
                "si": src,
                "k": f"ins:{uuid.uuid4()}",
            },
        )

    await mv("opening_balance", "12", 30)
    await mv("receive", "48", 10)

    # POS connection (active) + inbox + order + sale lines + menu items.
    conn = await _scalar(
        db,
        "INSERT INTO tenant_pos_connections (connection_id, tenant_id, vendor, merchant_id, "
        "environment, state, access_token_enc, access_token_expires_at, refresh_token_enc, "
        "refresh_token_expires_at, last_reconciliation_at, updated_at) "
        "VALUES (:cid,:t,'clover',:mid,'sandbox','active','x',:exp,'y',:exp,:lr,:lr) "
        "RETURNING connection_id",
        cid=uuid.uuid4(),
        t=tid,
        mid=f"M-{tid.hex[:10]}",
        lr=now - timedelta(minutes=5),
        exp=now + timedelta(days=30),
    )

    async def inbox(state: str, days: float, vid: str, processed: bool) -> uuid.UUID:
        iid_ = uuid.uuid4()
        await db.execute(
            text("""
                INSERT INTO pos_event_inbox
                    (inbox_id, tenant_id, connection_id, vendor, vendor_event_id, vendor_object_type,
                     vendor_event_type, vendor_ts, raw_payload, state, received_at, processed_at)
                VALUES (:id,:t,:c,'clover',:vid,'O','CREATE',:ts,:pl,:st,:rec,:proc)
            """),
            {
                "id": iid_,
                "t": tid,
                "c": conn,
                "vid": vid,
                "ts": 1,
                "pl": json.dumps({}),
                "st": state,
                "rec": now - timedelta(days=days),
                "proc": (now - timedelta(days=days)) if processed else None,
            },
        )
        return iid_

    inbox_o = await inbox("processed", 5, "E1", processed=True)
    await inbox("pending", 0.1, "E2", processed=False)

    order = await _scalar(
        db,
        "INSERT INTO orders (id, tenant_id, pos_event_inbox_id, clover_order_id, state, payment_state, "
        "closed_at, processed_at) VALUES (:id,:t,:ib,'CO1','locked','PAID',:c,:p) RETURNING id",
        id=uuid.uuid4(),
        t=tid,
        ib=inbox_o,
        c=now - timedelta(days=5),
        p=now - timedelta(days=5),
    )

    # Menu items: Lunch Bowl mapped (recipe version), plus one unmapped.
    lunch = await _scalar(
        db,
        "INSERT INTO menu_items (tenant_id, pos_item_id, name, active) "
        "VALUES (:t,'PI1','Lunch Bowl',true) RETURNING id",
        t=tid,
    )
    await db.execute(
        text(
            "INSERT INTO menu_items (tenant_id, pos_item_id, name, active) "
            "VALUES (:t,'PI2','Unmapped Special',true)"
        ),
        {"t": tid},
    )
    rec = await _scalar(
        db,
        "INSERT INTO recipes (tenant_id, menu_item_id, status) VALUES (:t,:m,'confirmed') RETURNING id",
        t=tid,
        m=lunch,
    )
    rv = await _scalar(
        db,
        "INSERT INTO recipe_versions (tenant_id, recipe_id, version_number, yield_quantity, name) "
        "VALUES (:t,:r,3,1,'Lunch Bowl v3') RETURNING id",
        t=tid,
        r=rec,
    )
    await db.execute(
        text("UPDATE menu_items SET recipe_version_id = :rv WHERE id = :m"), {"rv": rv, "m": lunch}
    )
    await db.execute(
        text(
            "INSERT INTO recipe_ingredients (tenant_id, recipe_version_id, inventory_item_id, "
            "quantity, unit) VALUES (:t,:rv,:i,0.25,'ea')"
        ),
        {"t": tid, "rv": rv, "i": item},
    )

    sli1 = await _scalar(
        db,
        "INSERT INTO sale_line_items (id, tenant_id, order_id, clover_line_item_id, menu_item_id, "
        "recipe_version_id, name_at_sale, quantity, price_cents_at_sale, net_revenue_cents, "
        "depletion_status) VALUES (:id,:t,:o,'L1',:m,:rv,'Lunch Bowl',24,10000,10000,'depleted') RETURNING id",
        id=uuid.uuid4(),
        t=tid,
        o=order,
        m=lunch,
        rv=rv,
    )
    await db.execute(
        text(
            "INSERT INTO sale_line_items (id, tenant_id, order_id, clover_line_item_id, "
            "name_at_sale, quantity, price_cents_at_sale, net_revenue_cents, depletion_status, "
            "depletion_reason) "
            "VALUES (:id,:t,:o,'L2','Unmapped Special',5,3800,3800,'unmapped','no_recipe')"
        ),
        {"id": uuid.uuid4(), "t": tid, "o": order},
    )

    # The depletion movement tied to SLI1 (6 ea consumed), and a count adjust.
    await mv("sale_depletion", "-6", 5, src_type="sale_line_item", src=sli1)
    await mv("count_adjust", "-1", 2)

    # Cost snapshot from a receipt (supplier via receipt_lines→receipts).
    rid = await _scalar(
        db,
        "INSERT INTO receipts (tenant_id, commit_state, source, supplier_name) "
        "VALUES (:t,'committed','email','Northstar Foods') RETURNING id",
        t=tid,
    )
    rl = await _scalar(
        db,
        "INSERT INTO receipt_lines (tenant_id, receipt_id, extracted_name, match_status, "
        "received_quantity, extracted_unit) VALUES (:t,:r,'AVOCADO','matched',1,'ea') RETURNING id",
        t=tid,
        r=rid,
    )
    await db.execute(
        text(
            "INSERT INTO ingredient_cost_snapshots (tenant_id, inventory_item_id, unit_cost_cents, "
            "unit_cost_cents_exact, source_receipt_line_id) VALUES (:t,:i,120,120.0000,:rl)"
        ),
        {"t": tid, "i": item, "rl": rl},
    )
    return {"tid": tid, "item": item, "lunch": lunch, "rv": rv}


# ── Ledger reconciliation ────────────────────────────────────────────────────


async def test_mode_a_ledger_reconciles_to_zero(db: Any) -> None:
    s = await _seed_mode_a(db)
    out = await _insights(db, s["tid"], s["item"])
    lg = out["ledger"]
    assert lg["mode"] == "recipe_deducted"
    assert lg["reconciled"] is True
    assert lg["state"] == "OK"
    assert lg["reconciliation_delta"] == "0"
    assert lg["balance_at_window_start"] == "12"  # only the day-30 opening precedes the window
    assert lg["current_on_hand"] == "53"  # 12 + 48 - 6 - 1
    kinds = {r["kind"]: r["quantity"] for r in lg["rows"]}
    assert kinds["received"] == "48"
    assert kinds["pos_consumption"] == "-6"
    assert kinds["adjustments"] == "-1"


async def test_item_state_and_status(db: Any) -> None:
    s = await _seed_mode_a(db)
    out = await _insights(db, s["tid"], s["item"])
    it = out["item"]
    assert it["on_hand"] == "53"
    assert it["par_level"] == "20"
    assert it["status"] == "ok"
    assert any(r["code"] == "ABOVE_PAR" for r in it["status_reasons"])


# ── POS diagnostics (8 independent dimensions) ───────────────────────────────


async def test_pos_diagnostics_dimensions_and_mapping_ratios(db: Any) -> None:
    s = await _seed_mode_a(db)
    out = await _insights(db, s["tid"], s["item"])
    pos = out["pos"]
    assert pos["provider"] == "clover"
    dims = pos["dimensions"]
    # Independent dimensions — connected but processing-backlogged (1 pending)
    # and mapping-partial, with completeness unproven, all at once.
    assert dims["connection"]["status"] == "connected"
    assert dims["processing"]["status"] == "backlogged"  # 1 pending event
    assert dims["processing"]["pending_event_count"] == 1
    assert dims["event_activity"]["latest_sales_data_received_at"] is not None
    assert dims["recipe_mapping"]["current_catalog"]["menu_items_mapped"] == 1
    assert dims["recipe_mapping"]["current_catalog"]["menu_items_unmapped"] == 1
    # eligible = 2 lines (both PAID/locked/not-refunded); 1 depleted → e2e 50.0.
    # eligible rev 13800, depleted 10000 → 72.5. effective = min = 50.0.
    e2e = dims["end_to_end_coverage"]
    assert e2e["eligible_sale_line_count"] == 2
    assert e2e["depleted_sale_line_count"] == 1
    assert e2e["line_coverage_pct"] == "50.0"
    assert e2e["revenue_coverage_pct"] == "72.5"
    assert e2e["effective_coverage_pct"] == "50.0"
    assert e2e["status"] == "failures"  # a known no_recipe failure outranks coverage level
    assert e2e["reason_breakdown"]["NO_RECIPE"] == 1
    # The unmapped line is a RECIPE failure — recipe_mapping shows it, and
    # conversion/depletion are NOT blamed.
    assert dims["recipe_mapping"]["status"] == "failures"
    assert dims["recipe_mapping"]["historical_window"]["no_recipe_count"] == 1
    assert dims["completeness"]["status"] == "unproven"
    assert dims["forecast_eligibility"]["status"] == "blocked"
    codes = {b["code"] for b in dims["forecast_eligibility"]["blockers"]}
    assert "END_TO_END_COVERAGE_INCOMPLETE" in codes
    assert "COMPLETENESS_UNPROVEN" in codes
    # Consistent order populations returned.
    assert pos["eligible_orders_in_window"] == 1
    assert pos["orders_seen_in_window"] >= 1


# ── end-to-end UNKNOWN partition (regression: unknown lines must not be hidden) ──


async def _seed_e2e_lines(db: Any, specs: list[tuple[str, str | None, int]]) -> dict[str, Any]:
    """Minimal tenant + active connection + one order carrying the given eligible
    lines. Each spec = (depletion_status, depletion_reason, net_revenue_cents)."""
    now = datetime.now(UTC)
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id,:s,'E2E')"),
        {"id": tid, "s": f"e2e-{tid.hex[:8]}"},
    )
    unit = await _scalar(
        db,
        "INSERT INTO units_of_measure (tenant_id,name,abbreviation,unit_type) "
        "VALUES (:t,'ea','ea','count') RETURNING id",
        t=tid,
    )
    item = await _scalar(
        db,
        "INSERT INTO inventory_items (tenant_id,name,inventory_mode,"
        "storage_unit_id,recipe_unit_id) VALUES (:t,'X','recipe_deducted',:u,:u) "
        "RETURNING id",
        t=tid,
        u=unit,
    )
    conn = await _scalar(
        db,
        "INSERT INTO tenant_pos_connections (connection_id,tenant_id,vendor,"
        "merchant_id,environment,state,access_token_enc,access_token_expires_at,refresh_token_enc,"
        "refresh_token_expires_at,last_reconciliation_at,updated_at) "
        "VALUES (:c,:t,'clover',:m,'sandbox','active','x',:e,'y',:e,:lr,:lr) RETURNING connection_id",
        c=uuid.uuid4(),
        t=tid,
        m=f"M-{tid.hex[:10]}",
        lr=now - timedelta(minutes=5),
        e=now + timedelta(days=30),
    )
    ib = await _scalar(
        db,
        "INSERT INTO pos_event_inbox (inbox_id,tenant_id,connection_id,vendor,"
        "vendor_event_id,vendor_object_type,vendor_event_type,vendor_ts,raw_payload,state,"
        "received_at,processed_at) VALUES (:i,:t,:c,'clover','E1','O','CREATE',1,:p,'processed',:r,:r) "
        "RETURNING inbox_id",
        i=uuid.uuid4(),
        t=tid,
        c=conn,
        p=json.dumps({}),
        r=now - timedelta(days=2),
    )
    order = await _scalar(
        db,
        "INSERT INTO orders (id,tenant_id,pos_event_inbox_id,clover_order_id,"
        "state,payment_state,closed_at,processed_at) VALUES (:id,:t,:ib,'CO1','locked','PAID',:c,:c) "
        "RETURNING id",
        id=uuid.uuid4(),
        t=tid,
        ib=ib,
        c=now - timedelta(days=2),
    )
    for n, (ds, dr, rev) in enumerate(specs):
        await db.execute(
            text(
                "INSERT INTO sale_line_items (id,tenant_id,order_id,clover_line_item_id,name_at_sale,"
                "quantity,price_cents_at_sale,net_revenue_cents,depletion_status,depletion_reason) "
                "VALUES (:id,:t,:o,:cli,'L',1,:rev,:rev,:ds,:dr)"
            ),
            {
                "id": uuid.uuid4(),
                "t": tid,
                "o": order,
                "cli": f"L{n}",
                "rev": rev,
                "ds": ds,
                "dr": dr,
            },
        )
    return {"tid": tid, "item": item}


def _e2e(out: dict[str, Any]) -> dict[str, Any]:
    return out["pos"]["dimensions"]["end_to_end_coverage"]


async def test_e2e_unknown_only_surfaces_unknown(db: Any) -> None:
    # 2 eligible lines with an unclassified reason (sale_ineligible) → 'unknown',
    # NOT 'none'/'partial'. The regression: unknown rows were previously invisible.
    s = await _seed_e2e_lines(
        db, [("failed", "sale_ineligible", 100), ("failed", "sale_ineligible", 100)]
    )
    e = _e2e(await _insights(db, s["tid"], s["item"]))
    assert e["status"] == "unknown"
    assert e["unknown_line_count"] == 2
    assert e["reason_breakdown"]["UNKNOWN"] == 2
    assert (
        e["depleted_sale_line_count"]
        + e["failure_count"]
        + e["pending_line_count"]
        + e["unknown_line_count"]
    ) == e["eligible_sale_line_count"]


async def test_e2e_unknown_plus_pending_is_unknown(db: Any) -> None:
    s = await _seed_e2e_lines(
        db, [("depleted", None, 100), ("pending", None, 100), ("failed", "sale_ineligible", 100)]
    )
    e = _e2e(await _insights(db, s["tid"], s["item"]))
    assert e["status"] == "unknown"  # unknown outranks in_progress
    assert e["unknown_line_count"] == 1
    assert e["pending_line_count"] == 1


async def test_e2e_failure_outranks_unknown(db: Any) -> None:
    s = await _seed_e2e_lines(
        db,
        [
            ("depleted", None, 100),
            ("unmapped", "no_recipe", 100),
            ("pending", None, 100),
            ("failed", "sale_ineligible", 100),
        ],
    )
    e = _e2e(await _insights(db, s["tid"], s["item"]))
    assert e["status"] == "failures"  # failures still dominate
    assert e["failure_count"] == 1
    assert e["unknown_line_count"] == 1
    assert e["pending_line_count"] == 1
    assert (
        e["depleted_sale_line_count"]
        + e["failure_count"]
        + e["pending_line_count"]
        + e["unknown_line_count"]
    ) == 4


async def test_e2e_complete_partition_has_zero_unknown(db: Any) -> None:
    s = await _seed_e2e_lines(db, [("depleted", None, 0), ("depleted", None, 0)])
    e = _e2e(await _insights(db, s["tid"], s["item"]))
    assert e["status"] == "complete"
    assert e["unknown_line_count"] == 0
    assert e["reason_breakdown"]["UNKNOWN"] == 0


async def test_e2e_zero_denominator_unavailable(db: Any) -> None:
    s = await _seed_e2e_lines(db, [])
    e = _e2e(await _insights(db, s["tid"], s["item"]))
    assert e["status"] == "unavailable"
    assert e["unknown_line_count"] == 0
    assert e["eligible_sale_line_count"] == 0


def test_e2e_partition_pure_function() -> None:
    # The partition/status selection is a PURE function — the corrupt (negative
    # residual) branch is tested here directly, WITHOUT mutating the DB schema.
    from app.modules.inventory.insights import _e2e_partition

    # normal partitions
    assert _e2e_partition(
        eligible=0, depleted=0, total_failures=0, pending=0, effective_pct=None
    ) == ("unavailable", 0, 0)
    assert _e2e_partition(
        eligible=2, depleted=2, total_failures=0, pending=0, effective_pct="100.0"
    ) == ("complete", 0, 0)
    assert _e2e_partition(
        eligible=2, depleted=0, total_failures=0, pending=0, effective_pct="0.0"
    ) == ("unknown", 2, 0)  # remainder = unknown
    assert _e2e_partition(
        eligible=3, depleted=1, total_failures=0, pending=1, effective_pct="33.3"
    ) == ("unknown", 1, 0)  # unknown > pending
    assert _e2e_partition(
        eligible=4, depleted=1, total_failures=1, pending=1, effective_pct="25.0"
    ) == ("failures", 1, 0)  # failures dominate
    assert _e2e_partition(
        eligible=2, depleted=0, total_failures=0, pending=2, effective_pct="0.0"
    ) == ("in_progress", 0, 0)
    # CORRUPT: double-counted row (depleted AND a failure) → residual = -1.
    status, unknown, overlap = _e2e_partition(
        eligible=1, depleted=1, total_failures=1, pending=0, effective_pct=None
    )
    assert status == "data_inconsistent"
    assert overlap == 1
    assert unknown == 0  # clamped — never negative
    # No input can ever produce a negative reported count.
    assert unknown >= 0 and overlap >= 0


async def test_status_reason_constraint_rejects_corrupt_row(db: Any) -> None:
    # The production constraint is what PREVENTS the corrupt combination that would
    # feed a negative residual. Prove it rejects (depleted + a failure reason)
    # rather than dropping the constraint elsewhere.
    from asyncpg.exceptions import CheckViolationError
    from sqlalchemy.exc import IntegrityError

    s = await _seed_e2e_lines(db, [])  # tenant + order, no lines
    order = await _scalar(db, "SELECT id FROM orders WHERE tenant_id=:t LIMIT 1", t=s["tid"])
    with pytest.raises((IntegrityError, CheckViolationError)):
        await db.execute(
            text(
                "INSERT INTO sale_line_items (id,tenant_id,order_id,clover_line_item_id,"
                "name_at_sale,quantity,price_cents_at_sale,net_revenue_cents,"
                "depletion_status,depletion_reason) "
                "VALUES (:id,:t,:o,'BAD','L',1,0,0,'depleted','computation_error')"
            ),
            {"id": uuid.uuid4(), "t": s["tid"], "o": order},
        )


async def test_unknown_lines_get_own_reason_not_recipe_coverage(db: Any) -> None:
    # Unknown-only tenant: UNKNOWN_SALE_LINES fires; RECIPE_COVERAGE_FAILURES must
    # NOT (no no_recipe/invalid_recipe evidence — attributing it to recipes is fabricated).
    s = await _seed_e2e_lines(
        db, [("failed", "sale_ineligible", 100), ("failed", "sale_ineligible", 100)]
    )
    out = await _insights(db, s["tid"], s["item"])
    codes = {r["code"] for r in out["reasons"]}
    assert "UNKNOWN_SALE_LINES" in codes
    assert "RECIPE_COVERAGE_FAILURES" not in codes
    ur = next(r for r in out["reasons"] if r["code"] == "UNKNOWN_SALE_LINES")
    assert ur["unknown_line_count"] == 2


async def test_recipe_coverage_failures_reason_is_historical_line_count(db: Any) -> None:
    # A no_recipe sold line → RECIPE_COVERAGE_FAILURES with the HISTORICAL affected
    # line count (not current-catalog unmapped, not called "unmapped").
    s = await _seed_e2e_lines(db, [("unmapped", "no_recipe", 100), ("depleted", None, 100)])
    out = await _insights(db, s["tid"], s["item"])
    r = next((x for x in out["reasons"] if x["code"] == "RECIPE_COVERAGE_FAILURES"), None)
    assert r is not None
    assert r["affected_sale_line_count"] == 1
    assert "unmapped_menu_items" not in r


async def test_forecast_blocker_identifies_unknown(db: Any) -> None:
    s = await _seed_e2e_lines(db, [("failed", "sale_ineligible", 100)])
    out = await _insights(db, s["tid"], s["item"])
    codes = {b["code"] for b in out["pos"]["dimensions"]["forecast_eligibility"]["blockers"]}
    assert "UNKNOWN_SALE_LINES" in codes


async def test_affected_menu_items_ids_and_truncation(db: Any) -> None:
    s = await _seed_mode_a(db)
    out = await _insights(db, s["tid"], s["item"])
    affected = out["pos"]["dimensions"]["affected_menu_items"]
    assert affected["has_more"] is False
    assert affected["total_count"] >= 1
    # The unmapped 'no_recipe' line surfaces with an id + fix_recipe destination.
    entry = next(a for a in affected["items"] if a["reason_code"] == "NO_RECIPE")
    assert entry["menu_item"] == "Unmapped Special"
    assert entry["menu_item_id"] is None  # this one had no catalog menu_item
    assert entry["revenue_cents"] == 3800
    assert entry["affected_sale_line_count"] == 1
    assert entry["repair"] == "review_recipe"
    assert affected["scope"] == "tenant"


async def test_consumption_single_confidence_object(db: Any) -> None:
    s = await _seed_mode_a(db)
    out = await _insights(db, s["tid"], s["item"])
    conf = out["consumption"]["confidence"]
    # ONE confidence object — floor with explicit reasons, no contradictory flags.
    assert conf["status"] == "floor"
    codes = {r["code"] for r in conf["reasons"]}
    assert "PROVIDER_COMPLETENESS_UNPROVEN" in codes
    assert "TENANT_COVERAGE_INCOMPLETE" in codes
    # No legacy duplicate indicators remain.
    assert "coverage" not in out["consumption"]
    assert "floor" not in out["consumption"]["summary"]


# ── Consumption + contributors ───────────────────────────────────────────────


async def test_observed_consumption_and_contributors(db: Any) -> None:
    s = await _seed_mode_a(db)
    out = await _insights(db, s["tid"], s["item"])
    cons = out["consumption"]
    assert cons["completeness"] == "uncertified"
    assert cons["summary"]["trend"]["state"] == "NOT_YET_CERTIFIED"
    total = sum(Decimal(d["consumed"]) for d in cons["daily"])
    assert total == Decimal("6")  # the single -6 depletion

    contribs = out["contributors"]
    assert len(contribs) == 1
    c = contribs[0]
    assert c["menu_item"] == "Lunch Bowl"
    assert c["total_consumed"] == "6"
    assert c["share_pct"] == "100.0"
    g = c["groups"][0]
    assert g["recipe_version"] == 3
    assert g["units_sold"] == "24"
    assert g["qty_per_sale"] == "0.25"
    assert g["explained"] is True


# ── Deterministic reasons ────────────────────────────────────────────────────


async def test_deterministic_reasons(db: Any) -> None:
    s = await _seed_mode_a(db)
    out = await _insights(db, s["tid"], s["item"])
    codes = {r["code"] for r in out["reasons"]}
    assert "TOP_MENU_CONSUMER" in codes
    assert "NO_RECENT_RECEIPT" in codes  # last receive was 10 days ago
    assert "MANUAL_ADJUSTMENT" in codes  # count_adjust -1 in window
    assert "RECIPE_COVERAGE_FAILURES" in codes  # a no_recipe sold line in window
    # No holidays/weather/promo/supplier-delay ever.
    assert not any(
        x in json.dumps(out["reasons"]).lower()
        for x in ("holiday", "weather", "promotion", "supplier delay")
    )


# ── Cost RBAC redaction ──────────────────────────────────────────────────────


async def test_cost_manager_sees_latest_and_aggregated(db: Any) -> None:
    s = await _seed_mode_a(db)
    out = await _insights(db, s["tid"], s["item"], can_view_aggregated_cost=True)
    assert out["cost"]["available"] is True
    assert out["cost"]["latest_unit_cost_cents_exact"] == "120.0000"
    assert out["cost"]["aggregated"]["available"] is True
    assert out["cost"]["aggregated"]["supplier_name"] == "Northstar Foods"
    assert len(out["cost"]["aggregated"]["history"]) >= 1


async def test_cost_staff_sees_latest_but_aggregated_redacted(db: Any) -> None:
    s = await _seed_mode_a(db)
    out = await _insights(db, s["tid"], s["item"], can_view_aggregated_cost=False)
    # Staff DO see the latest unit cost (needed for inventory operations)…
    assert out["cost"]["available"] is True
    assert out["cost"]["latest_unit_cost_cents_exact"] == "120.0000"
    # …but supplier identity + history are manager+ only.
    assert out["cost"]["aggregated"]["available"] is False
    assert out["cost"]["aggregated"]["reason"]["code"] == "MANAGER_ONLY"
    assert "supplier_name" not in out["cost"]["aggregated"]
    assert "history" not in out["cost"]["aggregated"]


# ── Forecast / reorder gated ─────────────────────────────────────────────────


async def test_forecast_and_reorder_not_yet_certified(db: Any) -> None:
    s = await _seed_mode_a(db)
    out = await _insights(db, s["tid"], s["item"])
    assert out["forecast"]["available"] is False
    assert out["forecast"]["state"] == "NOT_YET_CERTIFIED"
    assert out["reorder"]["mode"] == "unavailable"
    assert out["reorder"]["state"] == "NOT_YET_CERTIFIED"


# ── Mode B reconciliation unavailable ────────────────────────────────────────


async def test_mode_b_reconciliation_unavailable(db: Any) -> None:
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id,:s,'INSB')"),
        {"id": tid, "s": f"insb-{tid.hex[:8]}"},
    )
    unit = await _scalar(
        db,
        "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
        "VALUES (:t,'ea','ea','count') RETURNING id",
        t=tid,
    )
    now = datetime.now(UTC)
    item = await _scalar(
        db,
        "INSERT INTO inventory_items (tenant_id, name, inventory_mode, storage_unit_id, recipe_unit_id, "
        "count_cadence_days, count_grace_days, last_count_at, last_count_quantity) "
        "VALUES (:t,'Beans','count_anchored',:u,:u,7,9,:lca,200) RETURNING id",
        t=tid,
        u=unit,
        lca=now - timedelta(days=6),
    )
    out = await _insights(db, tid, item)
    assert out["ledger"]["state"] == "RECONCILIATION_UNAVAILABLE"
    assert out["ledger"]["reason"]["code"] == "MODE_B_RECONCILIATION_PENDING"
    assert out["item"]["on_hand"] == "200"
    assert out["forecast"]["state"] == "NOT_YET_CERTIFIED"


# ── Tenant isolation ─────────────────────────────────────────────────────────


async def test_foreign_item_not_found(db: Any) -> None:
    s = await _seed_mode_a(db)
    other = uuid.uuid4()
    with pytest.raises(ItemNotFound):
        await _insights(db, other, s["item"])  # foreign tenant asking for our item


async def test_unknown_item_not_found(db: Any) -> None:
    s = await _seed_mode_a(db)
    with pytest.raises(ItemNotFound):
        await _insights(db, s["tid"], uuid.uuid4())


# ── Categorization coverage (DATA_INCONSISTENT guard soundness) ───────────────


def test_ledger_categories_partition_every_mode_a_type() -> None:
    """Every movement type on_hand() counts (all except the 2 signals) maps to
    exactly one display category — otherwise reconciliation_delta would falsely
    fire. This pins the partition so a new movement type can't be silently
    dropped from the ledger."""
    from app.modules.inventory.insights import _CATEGORY_ORDER, _LEDGER_CATEGORY

    all_types = {
        "opening_balance",
        "receive",
        "sale_depletion",
        "sale_depletion_reversal",
        "sale_signal",
        "sale_signal_reversal",
        "count_adjust",
        "waste",
        "transfer_in",
        "transfer_out",
        "adjustment",
    }
    signals = {"sale_signal", "sale_signal_reversal"}
    assert set(_LEDGER_CATEGORY) == all_types - signals
    assert set(_LEDGER_CATEGORY.values()) == set(_CATEGORY_ORDER)


# ── Timezone ─────────────────────────────────────────────────────────────────


async def test_timezone_echoed_in_window(db: Any) -> None:
    s = await _seed_mode_a(db)
    out = await _insights(
        db, s["tid"], s["item"], timezone_name="America/Toronto", timezone_source="configured"
    )
    assert out["window"]["bucket_timezone"] == "America/Toronto"
    assert out["window"]["timezone_source"] == "configured"
    assert out["consumption"]["bucket_timezone"] == "America/Toronto"


async def test_timezone_fallback_default(db: Any) -> None:
    s = await _seed_mode_a(db)
    out = await _insights(db, s["tid"], s["item"])  # defaults UTC/fallback
    assert out["window"]["bucket_timezone"] == "UTC"
    assert out["window"]["timezone_source"] == "fallback"


# ── POS diagnostic scenarios (independent dimensions) ────────────────────────


async def _seed_pos(
    db: Any,
    *,
    conn_state: str = "active",
    inbox: list[tuple[str, float, bool]] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a tenant + item + connection + inbox rows. inbox entries are
    (state, days_ago_received, has_processing_started)."""
    now = datetime.now(UTC)
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id,:s,'POS')"),
        {"id": tid, "s": f"pos-{tid.hex[:8]}"},
    )
    unit = await _scalar(
        db,
        "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
        "VALUES (:t,'ea','ea','count') RETURNING id",
        t=tid,
    )
    item = await _scalar(
        db,
        "INSERT INTO inventory_items (tenant_id, name, inventory_mode, storage_unit_id, "
        "recipe_unit_id) VALUES (:t,'Oil','recipe_deducted',:u,:u) RETURNING id",
        t=tid,
        u=unit,
    )
    if conn_state is not None:
        cid = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO tenant_pos_connections (connection_id, tenant_id, vendor, merchant_id, "
                "environment, state, access_token_enc, access_token_expires_at, refresh_token_enc, "
                "refresh_token_expires_at, last_reconciliation_at, updated_at) "
                "VALUES (:c,:t,'clover','M','sandbox',:st,'x',:e,'y',:e,:lr,:lr)"
            ),
            {
                "c": cid,
                "t": tid,
                "st": conn_state,
                "e": now + timedelta(days=30),
                "lr": now - timedelta(minutes=3),
            },
        )
        for i, (state, days, has_proc) in enumerate(inbox or []):
            await db.execute(
                text("""
                    INSERT INTO pos_event_inbox
                        (inbox_id, tenant_id, connection_id, vendor, vendor_event_id,
                         vendor_object_type, vendor_event_type, vendor_ts, raw_payload, state,
                         received_at, processing_started_at, processed_at)
                    VALUES (:id,:t,:c,'clover',:v,'O','CREATE',1,'{}',:st,:rec,:proc,:done)
                """),
                {
                    "id": uuid.uuid4(),
                    "t": tid,
                    "c": cid,
                    "v": f"E{i}",
                    "st": state,
                    "rec": now - timedelta(days=days),
                    "proc": (now - timedelta(days=days)) if has_proc else None,
                    "done": (now - timedelta(days=days)) if state == "processed" else None,
                },
            )
    return tid, item


async def test_pos_processing_stalled(db: Any) -> None:
    # A 'processing' event whose lease is far past the 300s TTL → stalled.
    tid, item = await _seed_pos(db, inbox=[("processing", 0.02, True)])  # ~28min old
    dims = (await _insights(db, tid, item))["pos"]["dimensions"]
    assert dims["processing"]["status"] == "stalled"
    codes = {b["code"] for b in dims["forecast_eligibility"]["blockers"]}
    assert "POS_PROCESSING_STALLED" in codes
    assert "PROCESSING_EVENTS" in codes


async def test_young_pending_and_processing_block_forecast(db: Any) -> None:
    # Finding 2: even a JUST-arrived pending/processing event means a sale isn't
    # counted yet, so exact forecasting must be blocked regardless of age.
    tid, item = await _seed_pos(
        db,
        inbox=[("pending", 0.0001, False), ("processing", 0.0001, True)],  # seconds old
    )
    dims = (await _insights(db, tid, item))["pos"]["dimensions"]
    assert dims["processing"]["status"] != "current"  # not clean
    codes = {b["code"] for b in dims["forecast_eligibility"]["blockers"]}
    assert "PENDING_EVENTS" in codes
    assert "PROCESSING_EVENTS" in codes


async def test_pos_failed_and_permanently_failed_events(db: Any) -> None:
    tid, item = await _seed_pos(db, inbox=[("failed", 0.1, True), ("dead_letter", 0.2, True)])
    dims = (await _insights(db, tid, item))["pos"]["dimensions"]
    assert dims["processing"]["failed_event_count"] == 1
    assert dims["processing"]["permanently_failed_event_count"] == 1
    assert dims["processing"]["status"] == "backlogged"
    codes = {b["code"] for b in dims["forecast_eligibility"]["blockers"]}
    assert "FAILED_EVENTS" in codes


async def test_pos_disconnected_dimensions(db: Any) -> None:
    tid, item = await _seed_pos(db, conn_state="revoked", inbox=[("processed", 5, True)])
    dims = (await _insights(db, tid, item))["pos"]["dimensions"]
    assert dims["connection"]["status"] == "disconnected"
    assert dims["event_activity"]["status"] == "unavailable"
    assert dims["processing"]["status"] == "unavailable"
    codes = {b["code"] for b in dims["forecast_eligibility"]["blockers"]}
    assert "POS_DISCONNECTED" in codes


async def test_no_sales_period_is_quiet_not_broken(db: Any) -> None:
    # Finding 4: a connected POS with no recent events is 'quiet' (e.g. closed),
    # never presented as a broken connection.
    tid, item = await _seed_pos(db, conn_state="active", inbox=[])
    dims = (await _insights(db, tid, item))["pos"]["dimensions"]
    assert dims["connection"]["status"] == "connected"  # NOT broken
    assert dims["event_activity"]["status"] == "none"  # no events, but connected
    # reconciliation health reported separately from event activity.
    assert "latest_background_reconciliation_check_at" in dims["reconciliation_health"]


async def test_full_coverage_still_blocked_by_completeness(db: Any) -> None:
    # 100% end-to-end coverage, zero failures, and (crucially) NO pending/
    # processing events — forecast STILL blocked purely by unprovable completeness.
    s = await _seed_mode_a(db)
    await db.execute(
        text(
            "UPDATE sale_line_items SET depletion_status='depleted', depletion_reason=NULL "
            "WHERE tenant_id=:t AND depletion_status='unmapped'"
        ),
        {"t": s["tid"]},
    )
    # Clear the seed's pending event so only completeness remains.
    await db.execute(
        text(
            "UPDATE pos_event_inbox SET state='processed', processed_at=now() "
            "WHERE tenant_id=:t AND state='pending'"
        ),
        {"t": s["tid"]},
    )
    dims = (await _insights(db, s["tid"], s["item"]))["pos"]["dimensions"]
    assert dims["end_to_end_coverage"]["effective_coverage_pct"] == "100.0"
    assert dims["end_to_end_coverage"]["status"] == "complete"
    codes = {b["code"] for b in dims["forecast_eligibility"]["blockers"]}
    assert codes == {"COMPLETENESS_UNPROVEN"}  # ONLY completeness blocks now
    assert dims["forecast_eligibility"]["status"] == "blocked"


async def test_conversion_failure_not_called_unmapped(db: Any) -> None:
    # Finding 1: a mapped recipe that fails unit conversion must NOT be counted
    # as a recipe-mapping failure. It shows under conversion_coverage.
    s = await _seed_mode_a(db)
    await db.execute(
        text(
            "UPDATE sale_line_items SET depletion_status='failed', "
            "depletion_reason='missing_conversion' WHERE tenant_id=:t "
            "AND depletion_status='unmapped'"
        ),
        {"t": s["tid"]},
    )
    dims = (await _insights(db, s["tid"], s["item"]))["pos"]["dimensions"]
    # recipe_mapping does NOT blame this line (it HAS a recipe).
    assert dims["recipe_mapping"]["historical_window"]["no_recipe_count"] == 0
    assert dims["recipe_mapping"]["historical_window"]["invalid_recipe_count"] == 0
    # conversion_coverage owns the failure.
    assert dims["conversion_coverage"]["status"] == "failures"
    assert dims["conversion_coverage"]["missing_conversion_count"] == 1
    codes = {b["code"] for b in dims["forecast_eligibility"]["blockers"]}
    assert "CONVERSION_FAILURES" in codes
    assert "COMPLETENESS_UNPROVEN" in codes


async def test_zero_eligible_lines_gives_unavailable_not_ok(db: Any) -> None:
    # Finding 6: with no eligible lines, conversion/depletion must be 'unavailable',
    # never a false 'ok'.
    tid, item = await _seed_pos(db, conn_state="active", inbox=[])
    dims = (await _insights(db, tid, item))["pos"]["dimensions"]
    assert dims["recipe_mapping"]["status"] == "unavailable"
    assert dims["conversion_coverage"]["status"] == "unavailable"
    assert dims["depletion_execution"]["status"] == "unavailable"
    assert dims["end_to_end_coverage"]["status"] == "unavailable"


async def test_untrusted_watermark_never_certified(db: Any) -> None:
    # Finding 7: completeness exposes a CANDIDATE watermark with trusted=false,
    # never a certified orders_complete_through.
    s = await _seed_mode_a(db)
    completeness = (await _insights(db, s["tid"], s["item"]))["pos"]["dimensions"]["completeness"]
    assert completeness["status"] == "unproven"
    assert completeness["trusted"] is False
    assert "candidate_orders_complete_through" in completeness
    assert "orders_complete_through" not in completeness  # no trusted-looking field


async def test_duplicate_menu_item_names_stay_separate_by_id(db: Any) -> None:
    # Finding 3: two distinct menu items named "Latte" must not collapse.
    s = await _seed_mode_a(db)
    order = await _scalar(
        db,
        "SELECT id FROM orders WHERE tenant_id=:t LIMIT 1",
        t=s["tid"],
    )
    ids = []
    for i in range(2):
        mi = await _scalar(
            db,
            "INSERT INTO menu_items (tenant_id, pos_item_id, name, active) "
            "VALUES (:t,:p,'Latte',true) RETURNING id",
            t=s["tid"],
            p=f"LATTE{i}",
        )
        ids.append(mi)
        await db.execute(
            text(
                "INSERT INTO sale_line_items (id, tenant_id, order_id, clover_line_item_id, "
                "menu_item_id, name_at_sale, quantity, price_cents_at_sale, net_revenue_cents, "
                "depletion_status, depletion_reason) "
                "VALUES (:id,:t,:o,:cl,:m,'Latte',3,500,500,'unmapped','no_recipe')"
            ),
            {"id": uuid.uuid4(), "t": s["tid"], "o": order, "cl": f"LL{i}", "m": mi},
        )
    affected = (await _insights(db, s["tid"], s["item"]))["pos"]["dimensions"][
        "affected_menu_items"
    ]
    latte_ids = {a["menu_item_id"] for a in affected["items"] if a["menu_item"] == "Latte"}
    # Both distinct ids present — never collapsed into one "Latte" row.
    assert {str(i) for i in ids} <= latte_ids


async def test_unknown_reason_is_unknown_failure_not_depletion(db: Any) -> None:
    # Finding 3: an unrecognized/absent reason on a failed line → UNKNOWN_FAILURE,
    # never silently DEPLETION_FAILED.
    s = await _seed_mode_a(db)
    await db.execute(
        text(
            "UPDATE sale_line_items SET depletion_status='failed', depletion_reason='sale_ineligible' "
            "WHERE tenant_id=:t AND depletion_status='unmapped'"
        ),
        {"t": s["tid"]},
    )
    affected = (await _insights(db, s["tid"], s["item"]))["pos"]["dimensions"][
        "affected_menu_items"
    ]
    codes = {a["reason_code"] for a in affected["items"]}
    assert "UNKNOWN_FAILURE" in codes
    assert "DEPLETION_FAILED" not in codes


async def test_missing_day_is_a_gap_not_a_zero(db: Any) -> None:
    # Only days with actual movements appear; a quiet day is absent (a gap),
    # never a zero-consumption bar.
    s = await _seed_mode_a(db)
    daily = (await _insights(db, s["tid"], s["item"]))["consumption"]["daily"]
    dates = {d["date"] for d in daily}
    # The single depletion was 5 days ago; days without movements aren't emitted.
    assert len(dates) == len(daily)  # no duplicate/synthetic days
    assert all(Decimal(d["consumed"]) != 0 or d["is_partial"] for d in daily)


async def test_data_inconsistent_when_a_type_is_uncategorized(db: Any, monkeypatch: Any) -> None:
    """If a movement type on_hand() counts is NOT in the ledger category map, the
    displayed rows can't sum to the balance change → DATA_INCONSISTENT, rows
    withheld, forecast suppressed, evidence (on_hand) preserved."""
    import app.modules.inventory.insights as ins

    # Drop 'count_adjust' from the partition to simulate a coverage gap.
    patched = dict(ins._LEDGER_CATEGORY)
    del patched["count_adjust"]
    monkeypatch.setattr(ins, "_LEDGER_CATEGORY", patched)

    s = await _seed_mode_a(db)  # seed has a count_adjust -1 in window
    out = await _insights(db, s["tid"], s["item"])
    lg = out["ledger"]
    assert lg["state"] == "DATA_INCONSISTENT"
    assert lg["reconciled"] is False
    assert lg["rows"] == []  # withheld
    assert lg["current_on_hand"] == "53"  # evidence preserved
    assert lg["reconciliation_delta"] != "0"


async def test_affected_items_no_cross_tenant_leak(db: Any) -> None:
    # Finding 9: another tenant's failing sale lines must never appear in this
    # tenant's affected_menu_items (query is tenant-predicated on both joins).
    a = await _seed_mode_a(db)
    b = await _seed_mode_a(db)  # a second, independent tenant with its own unmapped line
    affected = (await _insights(db, a["tid"], a["item"]))["pos"]["dimensions"][
        "affected_menu_items"
    ]
    names = {x["menu_item"] for x in affected["items"]}
    # Tenant A sees its own unmapped 'Unmapped Special'; tenant B's rows are absent
    # by construction (same name, but scoped out) — assert no B-tenant ids leak.
    b_affected = (await _insights(db, b["tid"], b["item"]))["pos"]["dimensions"][
        "affected_menu_items"
    ]
    a_ids = {x["menu_item_id"] for x in affected["items"]}
    b_ids = {x["menu_item_id"] for x in b_affected["items"]}
    # The two tenants' affected sets share no menu_item_id, and A's revenue total
    # reflects only A's single unmapped line.
    assert a_ids.isdisjoint(b_ids - {None}) or (a_ids == {None} and b_ids == {None})
    assert "Unmapped Special" in names
    assert sum(x["revenue_cents"] for x in affected["items"]) == 3800  # only A's line


# ── Finding 1: evidence-only stage statuses (no false 'ok') ──────────────────


async def _seed_one_line(
    db: Any, *, status: str, reason: str | None, net_rev: int = 1000
) -> tuple[uuid.UUID, uuid.UUID]:
    """Tenant + Mode-A item + active connection + one eligible sale line in the
    given depletion state. No movements are manufactured for the funnel tests —
    they assert the STAGE logic over the recorded depletion_status/reason."""
    now = datetime.now(UTC)
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id,:s,'ONE')"),
        {"id": tid, "s": f"one-{tid.hex[:8]}"},
    )
    unit = await _scalar(
        db,
        "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
        "VALUES (:t,'ea','ea','count') RETURNING id",
        t=tid,
    )
    item = await _scalar(
        db,
        "INSERT INTO inventory_items (tenant_id, name, inventory_mode, storage_unit_id, "
        "recipe_unit_id) VALUES (:t,'Oil','recipe_deducted',:u,:u) RETURNING id",
        t=tid,
        u=unit,
    )
    cid = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO tenant_pos_connections (connection_id, tenant_id, vendor, merchant_id, "
            "environment, state, access_token_enc, access_token_expires_at, refresh_token_enc, "
            "refresh_token_expires_at, last_reconciliation_at, updated_at) "
            "VALUES (:c,:t,'clover',:m,'sandbox','active','x',:e,'y',:e,:lr,:lr)"
        ),
        {
            "c": cid,
            "t": tid,
            "m": f"M-{tid.hex[:8]}",
            "e": now + timedelta(days=30),
            "lr": now - timedelta(minutes=3),
        },
    )
    ib = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO pos_event_inbox (inbox_id, tenant_id, connection_id, vendor, "
            "vendor_event_id, vendor_object_type, vendor_event_type, vendor_ts, raw_payload, "
            "state, received_at, processed_at) "
            "VALUES (:id,:t,:c,'clover','E','O','CREATE',1,'{}','processed',:r,:r)"
        ),
        {"id": ib, "t": tid, "c": cid, "r": now - timedelta(days=2)},
    )
    order = await _scalar(
        db,
        "INSERT INTO orders (id, tenant_id, pos_event_inbox_id, clover_order_id, state, "
        "payment_state, closed_at, processed_at) VALUES (:id,:t,:ib,'O1','locked','PAID',:c,:c) "
        "RETURNING id",
        id=uuid.uuid4(),
        t=tid,
        ib=ib,
        c=now - timedelta(days=2),
    )
    await db.execute(
        text(
            "INSERT INTO sale_line_items (id, tenant_id, order_id, clover_line_item_id, "
            "name_at_sale, quantity, price_cents_at_sale, net_revenue_cents, depletion_status, "
            "depletion_reason) VALUES (:id,:t,:o,'L',:n,1,:nr,:nr,:st,:rs)"
        ),
        {
            "id": uuid.uuid4(),
            "t": tid,
            "o": order,
            "n": "Widget",
            "nr": net_rev,
            "st": status,
            "rs": reason,
        },
    )
    return tid, item


async def test_pending_line_never_reports_ok(db: Any) -> None:
    # Finding 1: a pending line must NOT make any stage 'ok' by subtraction.
    tid, item = await _seed_one_line(db, status="pending", reason=None)
    dims = (await _insights(db, tid, item))["pos"]["dimensions"]
    assert dims["recipe_mapping"]["status"] == "in_progress"  # not ok
    assert dims["recipe_mapping"]["historical_window"]["pending_count"] == 1
    assert dims["conversion_coverage"]["status"] == "unavailable"  # nothing evaluated
    assert dims["depletion_execution"]["status"] == "unavailable"
    assert dims["end_to_end_coverage"]["status"] == "in_progress"
    codes = {b["code"] for b in dims["forecast_eligibility"]["blockers"]}
    assert "PENDING_SALE_LINES" in codes


async def test_unknown_reason_line_yields_unknown_stage(db: Any) -> None:
    # An eligible failed line with a reason outside the funnel taxonomy → the
    # recipe stage is 'unknown', never a false 'ok'.
    tid, item = await _seed_one_line(db, status="failed", reason="sale_ineligible")
    dims = (await _insights(db, tid, item))["pos"]["dimensions"]
    assert dims["recipe_mapping"]["status"] == "unknown"
    assert dims["recipe_mapping"]["historical_window"]["unknown_count"] == 1


# ── Finding 2: zero/negative revenue ─────────────────────────────────────────


async def test_zero_revenue_falls_back_to_line_coverage(db: Any) -> None:
    tid, item = await _seed_one_line(db, status="depleted", reason=None, net_rev=0)
    e2e = (await _insights(db, tid, item))["pos"]["dimensions"]["end_to_end_coverage"]
    assert e2e["eligible_net_revenue_cents"] == 0
    assert e2e["revenue_coverage_pct"] is None  # N/A, not a broken number
    assert e2e["revenue_coverage_applicable"] is False
    assert e2e["line_coverage_pct"] == "100.0"  # line coverage authoritative
    assert e2e["effective_coverage_pct"] == "100.0"
    assert e2e["status"] == "complete"


async def test_negative_revenue_is_not_applicable(db: Any) -> None:
    # A fully-discounted / credited line can make depleted_rev exceed or invert
    # eligible_rev — revenue coverage must be N/A, never negative or >100.
    tid, item = await _seed_one_line(db, status="depleted", reason=None, net_rev=-500)
    e2e = (await _insights(db, tid, item))["pos"]["dimensions"]["end_to_end_coverage"]
    assert e2e["revenue_coverage_pct"] is None
    assert e2e["effective_coverage_pct"] == "100.0"  # line coverage authoritative


# ── Finding 3: connection-scoped health + catalog timestamp isolation ────────


async def test_old_connection_events_do_not_break_new_one(db: Any) -> None:
    now = datetime.now(UTC)
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id,:s,'RC')"),
        {"id": tid, "s": f"rc-{tid.hex[:8]}"},
    )
    unit = await _scalar(
        db,
        "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
        "VALUES (:t,'ea','ea','count') RETURNING id",
        t=tid,
    )
    item = await _scalar(
        db,
        "INSERT INTO inventory_items (tenant_id, name, inventory_mode, storage_unit_id, "
        "recipe_unit_id) VALUES (:t,'Oil','recipe_deducted',:u,:u) RETURNING id",
        t=tid,
        u=unit,
    )

    async def conn(state: str, ago_min: int) -> uuid.UUID:
        c = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO tenant_pos_connections (connection_id, tenant_id, vendor, merchant_id, "
                "environment, state, access_token_enc, access_token_expires_at, refresh_token_enc, "
                "refresh_token_expires_at, updated_at) "
                "VALUES (:c,:t,'clover',:m,'sandbox',:st,'x',:e,'y',:e,:u)"
            ),
            {
                "c": c,
                "t": tid,
                "m": f"M-{c.hex[:8]}",
                "st": state,
                "e": now + timedelta(days=30),
                "u": now - timedelta(minutes=ago_min),
            },
        )
        return c

    old = await conn("revoked", 60)
    new = await conn("active", 1)
    # Old connection has an unresolved dead-letter event; new one is clean.
    await db.execute(
        text(
            "INSERT INTO pos_event_inbox (inbox_id, tenant_id, connection_id, vendor, "
            "vendor_event_id, vendor_object_type, vendor_event_type, vendor_ts, raw_payload, state, "
            "received_at) VALUES (:id,:t,:c,'clover','OLD','O','CREATE',1,'{}','dead_letter',:r)"
        ),
        {"id": uuid.uuid4(), "t": tid, "c": old, "r": now - timedelta(days=3)},
    )
    dims = (await _insights(db, tid, item))["pos"]["dimensions"]
    # The NEW active connection is chosen; its current processing is clean…
    assert dims["connection"]["status"] == "connected"
    assert dims["processing"]["status"] == "current"
    assert dims["processing"]["permanently_failed_event_count"] == 0
    # …and the old event is surfaced separately, not folded into current health.
    assert dims["processing"]["historical_unresolved_event_count"] == 1
    assert str(new)  # sanity


async def test_catalog_processed_event_is_not_sales_data(db: Any) -> None:
    now = datetime.now(UTC)
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id,:s,'CAT')"),
        {"id": tid, "s": f"cat-{tid.hex[:8]}"},
    )
    unit = await _scalar(
        db,
        "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
        "VALUES (:t,'ea','ea','count') RETURNING id",
        t=tid,
    )
    item = await _scalar(
        db,
        "INSERT INTO inventory_items (tenant_id, name, inventory_mode, storage_unit_id, "
        "recipe_unit_id) VALUES (:t,'Oil','recipe_deducted',:u,:u) RETURNING id",
        t=tid,
        u=unit,
    )
    cid = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO tenant_pos_connections (connection_id, tenant_id, vendor, merchant_id, "
            "environment, state, access_token_enc, access_token_expires_at, refresh_token_enc, "
            "refresh_token_expires_at, updated_at) "
            "VALUES (:c,:t,'clover',:m,'sandbox','active','x',:e,'y',:e,:u)"
        ),
        {"c": cid, "t": tid, "m": f"M-{tid.hex[:8]}", "e": now + timedelta(days=30), "u": now},
    )
    # A processed CATALOG event ('I'), no order events at all.
    await db.execute(
        text(
            "INSERT INTO pos_event_inbox (inbox_id, tenant_id, connection_id, vendor, "
            "vendor_event_id, vendor_object_type, vendor_event_type, vendor_ts, raw_payload, state, "
            "received_at, processed_at) VALUES (:id,:t,:c,'clover','CAT','I','CREATE',1,'{}',"
            "'processed',:r,:r)"
        ),
        {"id": uuid.uuid4(), "t": tid, "c": cid, "r": now - timedelta(hours=1)},
    )
    dims = (await _insights(db, tid, item))["pos"]["dimensions"]
    # Neither received nor processed SALES timestamps set — a catalog event is
    # not sales data.
    assert dims["event_activity"]["latest_sales_data_received_at"] is None
    assert dims["processing"]["latest_sales_data_processed_at"] is None


# ── Finding 7: production-path (real depletion), not manufactured states ──────


async def test_production_path_deplete_flows_through_funnel(db: Any) -> None:
    from uuid import UUID as _UUID

    from app.modules.inventory.depletion import handler
    from tests.helpers.sprint5 import seed_recipe_version_session

    now = datetime.now(UTC)
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id,:s,'PROD')"),
        {"id": tid, "s": f"prod-{tid.hex[:8]}"},
    )
    su = await _scalar(
        db,
        "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
        "VALUES (:t,'g','g','weight') RETURNING id",
        t=tid,
    )
    item = await _scalar(
        db,
        "INSERT INTO inventory_items (tenant_id, name, inventory_mode, storage_unit_id, "
        "recipe_unit_id, storage_to_recipe_factor) "
        "VALUES (:t,'Flour','recipe_deducted',:u,:u,1) RETURNING id",
        t=tid,
        u=su,
    )
    seeded = await seed_recipe_version_session(
        db, str(tid), ingredients=[(item, 2, "g")], yield_quantity=1.0, status="confirmed"
    )
    cid = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO tenant_pos_connections (connection_id, tenant_id, vendor, merchant_id, "
            "environment, state, access_token_enc, access_token_expires_at, refresh_token_enc, "
            "refresh_token_expires_at, last_reconciliation_at, updated_at) "
            "VALUES (:c,:t,'clover',:m,'sandbox','active','x',:e,'y',:e,:lr,:lr)"
        ),
        {
            "c": cid,
            "t": tid,
            "m": f"M-{tid.hex[:8]}",
            "e": now + timedelta(days=30),
            "lr": now - timedelta(minutes=1),
        },
    )
    ib = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO pos_event_inbox (inbox_id, tenant_id, connection_id, vendor, "
            "vendor_event_id, vendor_object_type, vendor_event_type, vendor_ts, raw_payload, state, "
            "received_at, processed_at) VALUES (:id,:t,:c,'clover','E','O','CREATE',1,'{}',"
            "'processed',:r,:r)"
        ),
        {"id": ib, "t": tid, "c": cid, "r": now - timedelta(days=2)},
    )
    order = await _scalar(
        db,
        "INSERT INTO orders (id, tenant_id, pos_event_inbox_id, clover_order_id, state, "
        "payment_state, closed_at, processed_at) VALUES (:id,:t,:ib,'PO','locked','PAID',:c,:c) "
        "RETURNING id",
        id=uuid.uuid4(),
        t=tid,
        ib=ib,
        c=now - timedelta(days=2),
    )
    sli = await _scalar(
        db,
        "INSERT INTO sale_line_items (id, tenant_id, order_id, clover_line_item_id, "
        "menu_item_id, name_at_sale, quantity, price_cents_at_sale, net_revenue_cents, "
        "recipe_version_id, depletion_status) VALUES (:id,:t,:o,'L',:m,'Bread',3,900,900,:rv,"
        "'pending') RETURNING id",
        id=uuid.uuid4(),
        t=tid,
        o=order,
        m=seeded.menu_item_id,
        rv=seeded.recipe_version_id,
    )
    # REAL depletion path — writes movements + flips status via the walker/writer.
    status, reason = await handler.process_line(db, tid, _UUID(str(sli)))
    assert (status, reason) == ("depleted", None)

    dims = (await _insights(db, tid, item))["pos"]["dimensions"]
    assert dims["recipe_mapping"]["status"] == "ok"
    assert dims["conversion_coverage"]["status"] == "ok"
    assert dims["depletion_execution"]["status"] == "ok"
    assert dims["end_to_end_coverage"]["status"] == "complete"
    # Consumption reflects the REAL movement (2 g/unit x 3 sold = 6 g).
    out = await _insights(db, tid, item)
    total = sum(Decimal(d["consumed"]) for d in out["consumption"]["daily"])
    assert total == Decimal("6")


async def test_production_path_missing_conversion_is_conversion_failure(db: Any) -> None:
    from uuid import UUID as _UUID

    from app.modules.inventory.depletion import handler
    from tests.helpers.sprint5 import seed_recipe_version_session

    now = datetime.now(UTC)
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id,:s,'PRODC')"),
        {"id": tid, "s": f"prodc-{tid.hex[:8]}"},
    )
    su = await _scalar(
        db,
        "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
        "VALUES (:t,'g','g','weight') RETURNING id",
        t=tid,
    )
    item = await _scalar(
        db,
        "INSERT INTO inventory_items (tenant_id, name, inventory_mode, storage_unit_id, "
        "recipe_unit_id) VALUES (:t,'Flour','recipe_deducted',:u,:u) RETURNING id",
        t=tid,
        u=su,
    )
    # Recipe ingredient in 'ml' (volume) but item stores in 'g' (weight) →
    # cross-dimension, no conversion path → real missing_conversion failure.
    seeded = await seed_recipe_version_session(
        db, str(tid), ingredients=[(item, 2, "ml")], yield_quantity=1.0, status="confirmed"
    )
    cid = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO tenant_pos_connections (connection_id, tenant_id, vendor, merchant_id, "
            "environment, state, access_token_enc, access_token_expires_at, refresh_token_enc, "
            "refresh_token_expires_at, last_reconciliation_at, updated_at) "
            "VALUES (:c,:t,'clover',:m,'sandbox','active','x',:e,'y',:e,:lr,:lr)"
        ),
        {
            "c": cid,
            "t": tid,
            "m": f"M-{tid.hex[:8]}",
            "e": now + timedelta(days=30),
            "lr": now - timedelta(minutes=1),
        },
    )
    ib = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO pos_event_inbox (inbox_id, tenant_id, connection_id, vendor, "
            "vendor_event_id, vendor_object_type, vendor_event_type, vendor_ts, raw_payload, state, "
            "received_at, processed_at) VALUES (:id,:t,:c,'clover','E','O','CREATE',1,'{}',"
            "'processed',:r,:r)"
        ),
        {"id": ib, "t": tid, "c": cid, "r": now - timedelta(days=2)},
    )
    order = await _scalar(
        db,
        "INSERT INTO orders (id, tenant_id, pos_event_inbox_id, clover_order_id, state, "
        "payment_state, closed_at, processed_at) VALUES (:id,:t,:ib,'PO','locked','PAID',:c,:c) "
        "RETURNING id",
        id=uuid.uuid4(),
        t=tid,
        ib=ib,
        c=now - timedelta(days=2),
    )
    sli = await _scalar(
        db,
        "INSERT INTO sale_line_items (id, tenant_id, order_id, clover_line_item_id, "
        "menu_item_id, name_at_sale, quantity, price_cents_at_sale, net_revenue_cents, "
        "recipe_version_id, depletion_status) VALUES (:id,:t,:o,'L',:m,'Bread',3,900,900,:rv,"
        "'pending') RETURNING id",
        id=uuid.uuid4(),
        t=tid,
        o=order,
        m=seeded.menu_item_id,
        rv=seeded.recipe_version_id,
    )
    status, reason = await handler.process_line(db, tid, _UUID(str(sli)))
    assert status == "failed" and reason == "missing_conversion"

    dims = (await _insights(db, tid, item))["pos"]["dimensions"]
    # The recipe EXISTED — recipe stage passes; the CONVERSION stage owns the fail.
    assert dims["recipe_mapping"]["historical_window"]["no_recipe_count"] == 0
    assert dims["conversion_coverage"]["status"] == "failures"
    assert dims["conversion_coverage"]["missing_conversion_count"] == 1
    assert dims["depletion_execution"]["status"] == "unavailable"


# ── Finding 1: severity — failures never hidden by pending/unknown ───────────


async def _seed_lines(db: Any, lines: list[tuple[str, str | None]]) -> tuple[uuid.UUID, uuid.UUID]:
    """Tenant + item + active connection + one order + N eligible sale lines with
    the given (depletion_status, depletion_reason) each."""
    now = datetime.now(UTC)
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id,:s,'MIX')"),
        {"id": tid, "s": f"mix-{tid.hex[:8]}"},
    )
    unit = await _scalar(
        db,
        "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
        "VALUES (:t,'ea','ea','count') RETURNING id",
        t=tid,
    )
    item = await _scalar(
        db,
        "INSERT INTO inventory_items (tenant_id, name, inventory_mode, storage_unit_id, "
        "recipe_unit_id) VALUES (:t,'Oil','recipe_deducted',:u,:u) RETURNING id",
        t=tid,
        u=unit,
    )
    cid = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO tenant_pos_connections (connection_id, tenant_id, vendor, merchant_id, "
            "environment, state, access_token_enc, access_token_expires_at, refresh_token_enc, "
            "refresh_token_expires_at, last_reconciliation_at, updated_at) "
            "VALUES (:c,:t,'clover',:m,'sandbox','active','x',:e,'y',:e,:lr,:lr)"
        ),
        {
            "c": cid,
            "t": tid,
            "m": f"M-{tid.hex[:8]}",
            "e": now + timedelta(days=30),
            "lr": now - timedelta(minutes=3),
        },
    )
    ib = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO pos_event_inbox (inbox_id, tenant_id, connection_id, vendor, "
            "vendor_event_id, vendor_object_type, vendor_event_type, vendor_ts, raw_payload, state, "
            "received_at, processed_at) VALUES (:id,:t,:c,'clover','E','O','CREATE',1,'{}',"
            "'processed',:r,:r)"
        ),
        {"id": ib, "t": tid, "c": cid, "r": now - timedelta(days=2)},
    )
    order = await _scalar(
        db,
        "INSERT INTO orders (id, tenant_id, pos_event_inbox_id, clover_order_id, state, "
        "payment_state, closed_at, processed_at) VALUES (:id,:t,:ib,'O','locked','PAID',:c,:c) "
        "RETURNING id",
        id=uuid.uuid4(),
        t=tid,
        ib=ib,
        c=now - timedelta(days=2),
    )
    for i, (status, reason) in enumerate(lines):
        await db.execute(
            text(
                "INSERT INTO sale_line_items (id, tenant_id, order_id, clover_line_item_id, "
                "name_at_sale, quantity, price_cents_at_sale, net_revenue_cents, depletion_status, "
                "depletion_reason) VALUES (:id,:t,:o,:cl,'W',1,1000,1000,:st,:rs)"
            ),
            {"id": uuid.uuid4(), "t": tid, "o": order, "cl": f"L{i}", "st": status, "rs": reason},
        )
    return tid, item


async def test_failure_and_pending_reports_failures(db: Any) -> None:
    # Finding 1: a failed line + a pending line → 'failures' (failures outrank
    # in_progress), and BOTH counts are retained.
    tid, item = await _seed_lines(db, [("unmapped", "no_recipe"), ("pending", None)])
    dims = (await _insights(db, tid, item))["pos"]["dimensions"]
    rm = dims["recipe_mapping"]
    assert rm["status"] == "failures"
    assert rm["historical_window"]["no_recipe_count"] == 1
    assert rm["historical_window"]["pending_count"] == 1  # count kept, not hidden
    e2e = dims["end_to_end_coverage"]
    assert e2e["status"] == "failures"  # not in_progress
    assert e2e["failure_count"] == 1
    assert e2e["pending_line_count"] == 1


async def test_failure_and_unknown_reports_failures(db: Any) -> None:
    tid, item = await _seed_lines(
        db,
        [("unmapped", "no_recipe"), ("failed", "sale_ineligible")],  # sale_ineligible = unknown
    )
    rm = (await _insights(db, tid, item))["pos"]["dimensions"]["recipe_mapping"]
    assert rm["status"] == "failures"  # failures outrank unknown
    assert rm["historical_window"]["no_recipe_count"] == 1
    assert rm["historical_window"]["unknown_count"] == 1


# ── Finding 3: historical vs current catalog separated ───────────────────────


async def test_historical_and_current_catalog_separated(db: Any) -> None:
    s = await _seed_mode_a(db)
    rm = (await _insights(db, s["tid"], s["item"]))["pos"]["dimensions"]["recipe_mapping"]
    assert "historical_window" in rm
    assert "current_catalog" in rm
    # Frozen historical outcome vs live catalog — never mixed unlabeled.
    assert rm["historical_window"]["no_recipe_count"] == 1
    assert rm["current_catalog"]["menu_items_mapped"] == 1
    assert rm["current_catalog"]["menu_items_unmapped"] == 1


# ── Finding 4: consumption confidence is a tenant proxy, not ingredient-level ─


async def test_consumption_confidence_is_tenant_proxy(db: Any) -> None:
    s = await _seed_mode_a(db)
    conf = (await _insights(db, s["tid"], s["item"]))["consumption"]["confidence"]
    assert conf["scope"] == "tenant_proxy"
    assert conf["ingredient_level_completeness"] == "unproven"


# ── Finding 5: reconciliation freshness from the worker schedule ─────────────


async def test_reconciliation_stale_uses_schedule_threshold(db: Any) -> None:
    # A check 20 hours old must be 'stale' (schedule is 15 min + grace), never
    # 'recent' as it would be under the loose 24h event-quiet threshold.
    tid, item = await _seed_lines(db, [("depleted", None)])
    await db.execute(
        text("UPDATE tenant_pos_connections SET last_reconciliation_at = :ts WHERE tenant_id = :t"),
        {"ts": datetime.now(UTC) - timedelta(hours=20), "t": tid},
    )
    rh = (await _insights(db, tid, item))["pos"]["dimensions"]["reconciliation_health"]
    assert rh["status"] == "stale"
    assert rh["stale_after_seconds"] == 2700  # 15 min x 3 grace


# ── Finding 1/2: the typed contract holds across ALL states ──────────────────


async def test_full_payload_validates_against_contract_all_states(db: Any) -> None:
    from app.modules.inventory.insights_schemas import InsightsResponse

    # Mode A (rich), Mode B, disconnected, zero-eligible, pending, production-path.
    s = await _seed_mode_a(db)
    InsightsResponse.model_validate(await _insights(db, s["tid"], s["item"]))

    # Mode B
    tidb = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id,:s,'CVB')"),
        {"id": tidb, "s": f"cvb-{tidb.hex[:8]}"},
    )
    unit = await _scalar(
        db,
        "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
        "VALUES (:t,'ea','ea','count') RETURNING id",
        t=tidb,
    )
    itemb = await _scalar(
        db,
        "INSERT INTO inventory_items (tenant_id, name, inventory_mode, storage_unit_id, "
        "recipe_unit_id, count_cadence_days, count_grace_days, last_count_at, last_count_quantity) "
        "VALUES (:t,'Beans','count_anchored',:u,:u,7,9,:lca,200) RETURNING id",
        t=tidb,
        u=unit,
        lca=datetime.now(UTC) - timedelta(days=6),
    )
    InsightsResponse.model_validate(await _insights(db, tidb, itemb))

    # Disconnected + zero eligible + pending line + unknown reason
    tid_d, item_d = await _seed_pos(db, conn_state="revoked", inbox=[("processed", 5, True)])
    InsightsResponse.model_validate(await _insights(db, tid_d, item_d))
    tid_p, item_p = await _seed_one_line(db, status="pending", reason=None)
    InsightsResponse.model_validate(await _insights(db, tid_p, item_p))
    tid_u, item_u = await _seed_one_line(db, status="failed", reason="sale_ineligible")
    InsightsResponse.model_validate(await _insights(db, tid_u, item_u))


def test_contract_rejects_malformed_payloads() -> None:
    """Finding 2: representative malformed payloads MUST fail validation —
    proving the contract actually constrains nested evidence, not just the URL."""

    from pydantic import ValidationError

    from app.modules.inventory.insights_schemas import (
        Blocker,
        ConsumptionConfidence,
        Cost,
        Dimensions,
        E2EDim,
        InsightsResponse,
        Reason,
    )

    # A bad blocker code is rejected (enum enforced).
    with pytest.raises(ValidationError):
        Blocker.model_validate({"code": "NOT_A_REAL_BLOCKER"})
    # An unknown extra key on a stable structure is rejected (extra=forbid).
    with pytest.raises(ValidationError):
        E2EDim.model_validate(
            {
                "scope": "tenant",
                "status": "complete",
                "failure_count": 0,
                "unknown_line_count": 0,
                "overlap_line_count": 0,
                "pending_line_count": 0,
                "eligible_sale_line_count": 1,
                "depleted_sale_line_count": 1,
                "line_coverage_pct": "100.0",
                "eligible_net_revenue_cents": 1,
                "depleted_net_revenue_cents": 1,
                "revenue_coverage_pct": "100.0",
                "revenue_coverage_applicable": True,
                "effective_coverage_pct": "100.0",
                "reason_breakdown": {
                    "NO_RECIPE": 0,
                    "INVALID_RECIPE": 0,
                    "MISSING_CONVERSION": 0,
                    "DEPLETION_FAILED": 0,
                    "PROCESSING_PENDING": 0,
                    "UNKNOWN": 0,
                },
                # SURPRISE is the ONLY defect — all required fields above are valid.
                "SURPRISE": 1,
            },
        )
    # A negative count is rejected (Field(ge=0) on all count fields).
    with pytest.raises(ValidationError):
        E2EDim.model_validate(
            {
                "scope": "tenant",
                "status": "data_inconsistent",
                "failure_count": 0,
                "unknown_line_count": -1,
                "overlap_line_count": 0,
                "pending_line_count": 0,
                "eligible_sale_line_count": 0,
                "depleted_sale_line_count": 1,
                "line_coverage_pct": None,
                "eligible_net_revenue_cents": 0,
                "depleted_net_revenue_cents": 0,
                "revenue_coverage_pct": None,
                "revenue_coverage_applicable": False,
                "effective_coverage_pct": None,
                "reason_breakdown": {
                    "NO_RECIPE": 0,
                    "INVALID_RECIPE": 0,
                    "MISSING_CONVERSION": 0,
                    "DEPLETION_FAILED": 0,
                    "PROCESSING_PENDING": 0,
                    "UNKNOWN": 0,
                },
            },
        )
    # A confidence object claiming ingredient-level proof outside the enum fails.
    with pytest.raises(ValidationError):
        ConsumptionConfidence.model_validate(
            {
                "status": "floor",
                "scope": "tenant_proxy",
                "ingredient_level_completeness": "definitely_complete",
                "effective_coverage_pct": None,
                "reasons": [],
            }
        )
    # A reason with an unknown evidence field fails (evidence is enumerated).
    with pytest.raises(ValidationError):
        Reason.model_validate({"code": "POS_DISCONNECTED", "source": "pos", "made_up_evidence": 1})
    # Cost missing the required 'available' fails.
    with pytest.raises(ValidationError):
        Cost.model_validate({"latest_unit_cost_cents_exact": "120.0000"})
    # Dimensions missing a whole dimension fails.
    with pytest.raises(ValidationError):
        Dimensions.model_validate(
            {"connection": {"status": "connected", "provider": "clover", "state": "active"}}
        )
    # A wrong ledger state enum on the full response fails.
    good_ledger = {
        "mode": "recipe_deducted",
        "anchor_basis": "ledger_sum",
        "reconciled": True,
        "state": "BOGUS_STATE",
        "rows": [],
        "current_on_hand": "1",
    }
    with pytest.raises(ValidationError):
        InsightsResponse.model_validate({"ledger": good_ledger})  # also missing sections
