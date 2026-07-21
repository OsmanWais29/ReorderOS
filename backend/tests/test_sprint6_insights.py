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
        "VALUES (:cid,:t,'clover','M1','sandbox','active','x',:exp,'y',:exp,:lr,:lr) "
        "RETURNING connection_id",
        cid=uuid.uuid4(),
        t=tid,
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
    assert dims["ingestion"]["latest_sales_data_received_at"] is not None
    assert dims["mapping"]["menu_items_mapped"] == 1
    assert dims["mapping"]["menu_items_unmapped"] == 1
    # eligible = 2 lines (both PAID/locked/not-refunded); 1 depleted → 50.0.
    # eligible rev 13800, mapped 10000 → 72.5. effective = min = 50.0.
    assert dims["mapping"]["eligible_sale_line_count"] == 2
    assert dims["mapping"]["mapped_sale_line_count"] == 1
    assert dims["mapping"]["sale_line_mapping_pct"] == "50.0"
    assert dims["mapping"]["revenue_mapping_pct"] == "72.5"
    assert dims["mapping"]["effective_mapping_pct"] == "50.0"
    assert dims["mapping"]["status"] == "partial"
    assert dims["mapping"]["reason_breakdown"]["NO_RECIPE"] == 1
    assert dims["completeness"]["status"] == "unproven"
    assert dims["forecast_eligibility"]["status"] == "blocked"
    codes = {b["code"] for b in dims["forecast_eligibility"]["blockers"]}
    assert "MAPPING_INCOMPLETE" in codes
    assert "COMPLETENESS_UNPROVEN" in codes


async def test_affected_menu_items_lists_unmapped_with_repair(db: Any) -> None:
    s = await _seed_mode_a(db)
    out = await _insights(db, s["tid"], s["item"])
    affected = out["pos"]["dimensions"]["mapping"]["affected_menu_items"]
    # The unmapped 'no_recipe' line surfaces with a fix_recipe destination.
    entry = next(a for a in affected if a["reason_code"] == "NO_RECIPE")
    assert entry["menu_item"] == "Unmapped Special"
    assert entry["revenue_cents"] == 3800
    assert entry["repair"] == "fix_recipe"


async def test_consumption_floor_labeled_below_full_coverage(db: Any) -> None:
    s = await _seed_mode_a(db)
    out = await _insights(db, s["tid"], s["item"])
    cov = out["consumption"]["coverage"]
    assert cov["is_floor"] is True
    assert cov["effective_mapping_pct"] == "50.0"
    assert cov["note_code"] == "OBSERVED_USAGE_IS_MINIMUM_UNMAPPED_SALES"


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
    assert "UNMAPPED_SOLD_ITEMS" in codes  # coverage < 100
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


async def test_pos_pending_backlog(db: Any) -> None:
    # A pending event older than the 900s backlog threshold.
    tid, item = await _seed_pos(db, inbox=[("pending", 0.02, False)])
    dims = (await _insights(db, tid, item))["pos"]["dimensions"]
    assert dims["processing"]["status"] == "backlogged"
    assert dims["processing"]["pending_event_count"] == 1


async def test_pos_failed_and_permanently_failed_events(db: Any) -> None:
    tid, item = await _seed_pos(db, inbox=[("failed", 0.1, True), ("dead_letter", 0.2, True)])
    dims = (await _insights(db, tid, item))["pos"]["dimensions"]
    assert dims["processing"]["failed_event_count"] == 1
    assert dims["processing"]["permanently_failed_event_count"] == 1
    assert dims["processing"]["status"] == "backlogged"


async def test_pos_disconnected_dimensions(db: Any) -> None:
    tid, item = await _seed_pos(db, conn_state="revoked", inbox=[("processed", 5, True)])
    dims = (await _insights(db, tid, item))["pos"]["dimensions"]
    assert dims["connection"]["status"] == "disconnected"
    assert dims["ingestion"]["status"] == "unavailable"
    assert dims["processing"]["status"] == "unavailable"
    codes = {b["code"] for b in dims["forecast_eligibility"]["blockers"]}
    assert "POS_DISCONNECTED" in codes


async def test_full_coverage_still_blocked_by_completeness(db: Any) -> None:
    # 100% eligible-line mapping, zero failures — forecast STILL blocked because
    # completeness is unprovable in A1 (watermark null).
    s = await _seed_mode_a(db)
    # Mark the previously-unmapped line as depleted → 100% coverage.
    await db.execute(
        text(
            "UPDATE sale_line_items SET depletion_status='depleted', depletion_reason=NULL "
            "WHERE tenant_id=:t AND depletion_status='unmapped'"
        ),
        {"t": s["tid"]},
    )
    dims = (await _insights(db, s["tid"], s["item"]))["pos"]["dimensions"]
    assert dims["mapping"]["effective_mapping_pct"] == "100.0"
    assert dims["mapping"]["status"] == "complete"
    codes = {b["code"] for b in dims["forecast_eligibility"]["blockers"]}
    # Mapping/conversion/depletion no longer block at 100% + zero failures…
    assert "MAPPING_INCOMPLETE" not in codes
    assert "CONVERSION_FAILURES" not in codes
    assert "DEPLETION_FAILURES" not in codes
    # …but completeness is unprovable in A1, so it always blocks.
    assert "COMPLETENESS_UNPROVEN" in codes
    assert dims["forecast_eligibility"]["status"] == "blocked"


async def test_conversion_failure_blocks_even_at_full_mapping(db: Any) -> None:
    s = await _seed_mode_a(db)
    # Full mapping but one line failed on missing_conversion.
    await db.execute(
        text(
            "UPDATE sale_line_items SET depletion_status='failed', "
            "depletion_reason='missing_conversion' WHERE tenant_id=:t "
            "AND depletion_status='unmapped'"
        ),
        {"t": s["tid"]},
    )
    dims = (await _insights(db, s["tid"], s["item"]))["pos"]["dimensions"]
    assert dims["conversion"]["status"] == "failures"
    assert dims["conversion"]["missing_conversion_count"] == 1
    codes = {b["code"] for b in dims["forecast_eligibility"]["blockers"]}
    assert "CONVERSION_FAILURES" in codes
    assert "COMPLETENESS_UNPROVEN" in codes


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
