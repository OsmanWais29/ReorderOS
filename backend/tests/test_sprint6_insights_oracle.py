"""Independent computational certification for Stock Insights (PR-A1).

The oracle here re-derives expected balances DIRECTLY from raw database rows in
Python Decimal, WITHOUT importing or calling on_hand(), current_balance(),
build_item_insights(), or any production projection helper. It is a genuine
second implementation of the balance specification (row iteration + explicit
branching, not the production CTEs). Agreement between two independent
implementations, plus the metamorphic properties below, is the arithmetic proof
that OpenAPI shape tests cannot give.

NOT proven here (by design): source-data completeness (whether every Clover order
was received) — that stays NOT_YET_CERTIFIED until a provider cutoff exists.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import engine, make_bound_session
from app.modules.inventory.insights import build_item_insights

# on_hand is the AUTHORITATIVE production function — we compare the oracle against
# it, but the oracle never calls it.
from app.modules.inventory.services import on_hand

pytestmark = pytest.mark.integration

_MODE_A_NON_SIGNAL = {
    "opening_balance",
    "receive",
    "sale_depletion",
    "sale_depletion_reversal",
    "count_adjust",
    "waste",
    "transfer_in",
    "transfer_out",
    "adjustment",
}
_MODE_B_RECEIPTS = {"receive", "transfer_in", "count_adjust", "opening_balance"}
_SIGNALS = {"sale_signal", "sale_signal_reversal"}


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


# ── the independent oracle (reads raw rows only) ─────────────────────────────


async def oracle_on_hand(db: Any, tid: uuid.UUID, iid: uuid.UUID) -> Decimal | None:
    item = (
        (
            await db.execute(
                text(
                    "SELECT inventory_mode, last_count_at, last_count_quantity "
                    "FROM inventory_items WHERE tenant_id = :t AND id = :i"
                ),
                {"t": tid, "i": iid},
            )
        )
        .mappings()
        .fetchone()
    )
    if item is None:
        return None
    rows = (
        (
            await db.execute(
                text(
                    "SELECT movement_type, delta, recorded_at FROM inventory_movements "
                    "WHERE tenant_id = :t AND inventory_item_id = :i"
                ),
                {"t": tid, "i": iid},
            )
        )
        .mappings()
        .all()
    )

    if item["inventory_mode"] == "recipe_deducted":
        total = Decimal("0")
        for r in rows:
            if r["movement_type"] in _SIGNALS:
                continue
            total += Decimal(str(r["delta"]))
        return total

    # count_anchored
    if item["last_count_quantity"] is None:
        return None
    yf = (
        await db.execute(
            text(
                "SELECT yield_factor FROM inventory_yield_factors "
                "WHERE tenant_id = :t AND inventory_item_id = :i"
            ),
            {"t": tid, "i": iid},
        )
    ).scalar_one_or_none()
    yfac = Decimal(str(yf)) if yf is not None else Decimal("1.0")
    anchor_at = item["last_count_at"]
    receipts = Decimal("0")
    signals = Decimal("0")
    for r in rows:
        if anchor_at is not None and r["recorded_at"] <= anchor_at:
            continue
        if r["movement_type"] in _MODE_B_RECEIPTS:
            receipts += Decimal(str(r["delta"]))
        elif r["movement_type"] in _SIGNALS:
            signals += Decimal(str(r["delta"]))
    return Decimal(str(item["last_count_quantity"])) + receipts - signals * yfac


# ── seed helpers ─────────────────────────────────────────────────────────────


async def _tenant(db: Any) -> uuid.UUID:
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id,:s,'ORC')"),
        {"id": tid, "s": f"orc-{tid.hex[:8]}"},
    )
    return tid


async def _item(
    db: Any,
    tid: uuid.UUID,
    *,
    mode: str,
    count_at: datetime | None = None,
    count_qty: Decimal | None = None,
) -> Any:
    unit = (
        await db.execute(
            text(
                "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
                "VALUES (:t,:n,:n,'weight') RETURNING id"
            ),
            {"t": tid, "n": f"g{uuid.uuid4().hex[:4]}"},
        )
    ).scalar_one()
    cad = 7 if mode == "count_anchored" else None
    gr = 9 if mode == "count_anchored" else None
    return (
        await db.execute(
            text("""
                INSERT INTO inventory_items
                    (tenant_id, name, inventory_mode, storage_unit_id, recipe_unit_id,
                     count_cadence_days, count_grace_days, last_count_at, last_count_quantity)
                VALUES (:t,:n,:m,:u,:u,:cad,:gr,:lca,:lcq) RETURNING id
            """),
            {
                "t": tid,
                "n": f"i-{uuid.uuid4().hex[:6]}",
                "m": mode,
                "u": unit,
                "cad": cad,
                "gr": gr,
                "lca": count_at,
                "lcq": count_qty,
            },
        )
    ).scalar_one()


async def _mv(
    db: Any,
    tid: uuid.UUID,
    iid: uuid.UUID,
    *,
    mtype: str,
    delta: str,
    when: datetime,
    key: str | None = None,
    yfa: str | None = None,
) -> None:
    await db.execute(
        text("""
            INSERT INTO inventory_movements
                (tenant_id, inventory_item_id, movement_type, delta, recorded_at, created_at,
                 idempotency_key, yield_factor_applied)
            VALUES (:t,:i,:mt,:d,:w,:w,:k,:yfa)
            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
        """),
        {
            "t": tid,
            "i": iid,
            "mt": mtype,
            "d": delta,
            "w": when,
            "k": key or f"orc:{uuid.uuid4()}",
            "yfa": yfa,
        },
    )


async def _assert_chain(db: Any, tid: uuid.UUID, iid: uuid.UUID) -> Decimal | None:
    """oracle == on_hand() == build_item_insights().item.on_hand (Decimal-exact)."""
    exp = await oracle_on_hand(db, tid, iid)
    auth = await on_hand(db, tenant_id=tid, inventory_item_id=iid)
    assert auth == exp, f"on_hand {auth} != oracle {exp}"
    out = await build_item_insights(
        db,
        tenant_id=tid,
        item_id=iid,
        window_key="30d",
        as_of=datetime.now(UTC),
        timezone_name="UTC",
        timezone_source="fallback",
        can_view_aggregated_cost=True,
        target_cover_days=7,
        target_source="default",
    )
    got = out["item"]["on_hand"]
    if exp is None:
        assert got is None
    else:
        assert Decimal(got) == exp, f"insights on_hand {got} != oracle {exp}"
    return exp


# ── randomized sequences (fixed seed) ────────────────────────────────────────


async def test_oracle_matches_authoritative_random_mode_a(db: Any) -> None:
    rng = random.Random(20260720)
    now = datetime.now(UTC)
    for _ in range(12):
        tid = await _tenant(db)
        item = await _item(db, tid, mode="recipe_deducted")
        await _mv(
            db, tid, item, mtype="opening_balance", delta="100", when=now - timedelta(days=40)
        )
        types = [
            "receive",
            "sale_depletion",
            "sale_depletion_reversal",
            "count_adjust",
            "transfer_in",
            "transfer_out",
            "waste",
            "adjustment",
        ]
        for d in range(20):
            mt = rng.choice(types)
            mag = Decimal(str(rng.randint(1, 500))) / Decimal("10")
            delta = str(-mag if mt in ("sale_depletion", "transfer_out", "waste") else mag)
            await _mv(db, tid, item, mtype=mt, delta=delta, when=now - timedelta(days=39 - d))
        await _assert_chain(db, tid, item)


async def test_oracle_matches_authoritative_random_mode_b(db: Any) -> None:
    rng = random.Random(99)
    now = datetime.now(UTC)
    for _ in range(10):
        tid = await _tenant(db)
        count_at = now - timedelta(days=15)
        item = await _item(
            db,
            tid,
            mode="count_anchored",
            count_at=count_at,
            count_qty=Decimal("500"),
        )
        # yield factor for some items
        if rng.random() < 0.5:
            await db.execute(
                text(
                    "INSERT INTO inventory_yield_factors (tenant_id, inventory_item_id, "
                    "yield_factor) VALUES (:t,:i,:y)"
                ),
                {"t": tid, "i": item, "y": "0.9"},
            )
        # some movements BEFORE the count (must be ignored) and after
        await _mv(db, tid, item, mtype="receive", delta="50", when=count_at - timedelta(days=2))
        for d in range(12):
            mt = rng.choice(["receive", "sale_signal", "sale_signal_reversal", "count_adjust"])
            mag = Decimal(str(rng.randint(1, 300))) / Decimal("10")
            delta = str(-mag if mt == "sale_signal_reversal" else mag)
            yfa = "1.0" if mt in _SIGNALS else None
            await _mv(
                db, tid, item, mtype=mt, delta=delta, when=count_at + timedelta(days=1 + d), yfa=yfa
            )
        await _assert_chain(db, tid, item)


async def test_mode_b_uncounted_is_none(db: Any) -> None:
    tid = await _tenant(db)
    item = await _item(db, tid, mode="count_anchored", count_at=None, count_qty=None)
    await _mv(db, tid, item, mtype="receive", delta="10", when=datetime.now(UTC))
    assert await _assert_chain(db, tid, item) is None


# ── metamorphic properties ───────────────────────────────────────────────────


async def test_sale_plus_equal_reversal_is_no_op(db: Any) -> None:
    now = datetime.now(UTC)
    tid = await _tenant(db)
    item = await _item(db, tid, mode="recipe_deducted")
    await _mv(db, tid, item, mtype="opening_balance", delta="100", when=now - timedelta(days=5))
    before = await _assert_chain(db, tid, item)
    await _mv(db, tid, item, mtype="sale_depletion", delta="-7.5", when=now - timedelta(days=3))
    await _mv(
        db, tid, item, mtype="sale_depletion_reversal", delta="7.5", when=now - timedelta(days=2)
    )
    after = await _assert_chain(db, tid, item)
    assert after == before  # equal reversal leaves the balance unchanged


async def test_duplicate_idempotency_key_is_no_op(db: Any) -> None:
    now = datetime.now(UTC)
    tid = await _tenant(db)
    item = await _item(db, tid, mode="recipe_deducted")
    await _mv(db, tid, item, mtype="receive", delta="40", when=now, key="dup")
    a = await _assert_chain(db, tid, item)
    await _mv(db, tid, item, mtype="receive", delta="40", when=now, key="dup")  # dropped
    b = await _assert_chain(db, tid, item)
    assert a == b == Decimal("40")


async def test_splitting_a_movement_preserves_total(db: Any) -> None:
    now = datetime.now(UTC)
    tid1, tid2 = await _tenant(db), await _tenant(db)
    i1 = await _item(db, tid1, mode="recipe_deducted")
    i2 = await _item(db, tid2, mode="recipe_deducted")
    await _mv(db, tid1, i1, mtype="opening_balance", delta="50", when=now - timedelta(days=3))
    await _mv(db, tid1, i1, mtype="sale_depletion", delta="-10", when=now - timedelta(days=1))
    await _mv(db, tid2, i2, mtype="opening_balance", delta="50", when=now - timedelta(days=3))
    await _mv(db, tid2, i2, mtype="sale_depletion", delta="-6", when=now - timedelta(days=1))
    await _mv(db, tid2, i2, mtype="sale_depletion", delta="-4", when=now - timedelta(days=1))
    assert await _assert_chain(db, tid1, i1) == await _assert_chain(db, tid2, i2)


async def test_row_order_independent(db: Any) -> None:
    now = datetime.now(UTC)
    # No opening_balance here — its must-be-first trigger would forbid reversed
    # INSERT order; receive is the initial positive so insertion order is free.
    deltas = [("receive", "20", 5), ("receive", "30", 4), ("sale_depletion", "-8", 2)]
    tid1, tid2 = await _tenant(db), await _tenant(db)
    i1 = await _item(db, tid1, mode="recipe_deducted")
    i2 = await _item(db, tid2, mode="recipe_deducted")
    for mt, d, days in deltas:
        await _mv(db, tid1, i1, mtype=mt, delta=d, when=now - timedelta(days=days))
    for mt, d, days in reversed(deltas):  # different INSERT order, same movements
        await _mv(db, tid2, i2, mtype=mt, delta=d, when=now - timedelta(days=days))
    assert await _assert_chain(db, tid1, i1) == await _assert_chain(db, tid2, i2) == Decimal("42")


async def test_foreign_tenant_rows_do_not_affect_result(db: Any) -> None:
    now = datetime.now(UTC)
    tid = await _tenant(db)
    item = await _item(db, tid, mode="recipe_deducted")
    await _mv(db, tid, item, mtype="opening_balance", delta="15", when=now - timedelta(days=2))
    other = await _tenant(db)
    oitem = await _item(db, other, mode="recipe_deducted")
    await _mv(db, other, oitem, mtype="opening_balance", delta="9999", when=now - timedelta(days=2))
    assert await _assert_chain(db, tid, item) == Decimal("15")


# ── ledger categories sum to the authoritative window change ─────────────────


async def test_ledger_categories_sum_to_window_change(db: Any) -> None:
    now = datetime.now(UTC)
    tid = await _tenant(db)
    item = await _item(db, tid, mode="recipe_deducted")
    await _mv(db, tid, item, mtype="opening_balance", delta="12", when=now - timedelta(days=30))
    await _mv(db, tid, item, mtype="receive", delta="48", when=now - timedelta(days=8))
    await _mv(db, tid, item, mtype="sale_depletion", delta="-6", when=now - timedelta(days=5))
    await _mv(db, tid, item, mtype="count_adjust", delta="-1", when=now - timedelta(days=2))
    out = await build_item_insights(
        db,
        tenant_id=tid,
        item_id=item,
        window_key="14d",
        as_of=now,
        timezone_name="UTC",
        timezone_source="fallback",
        can_view_aggregated_cost=True,
        target_cover_days=7,
        target_source="default",
    )
    lg = out["ledger"]
    assert lg["reconciled"] is True
    assert lg["reconciliation_delta"] == "0"
    rows_sum = sum(Decimal(r["quantity"]) for r in lg["rows"])
    window_change = Decimal(lg["current_on_hand"]) - Decimal(lg["balance_at_window_start"])
    assert rows_sum == window_change  # every displayed category sums back


# ── coverage funnel partitions every eligible line exactly once ──────────────


async def _seed_lines(db: Any, lines: list[tuple[str, str | None]]) -> tuple[uuid.UUID, uuid.UUID]:
    now = datetime.now(UTC)
    tid = await _tenant(db)
    item = await _item(db, tid, mode="recipe_deducted")
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
    order = (
        await db.execute(
            text(
                "INSERT INTO orders (id, tenant_id, pos_event_inbox_id, clover_order_id, state, "
                "payment_state, closed_at, processed_at) VALUES "
                "(:id,:t,:ib,'O','locked','PAID',:c,:c) RETURNING id"
            ),
            {"id": uuid.uuid4(), "t": tid, "ib": ib, "c": now - timedelta(days=2)},
        )
    ).scalar_one()
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


async def test_funnel_partitions_eligible_lines_and_bounds(db: Any) -> None:
    tid, item = await _seed_lines(
        db,
        [
            ("depleted", None),
            ("depleted", None),
            ("unmapped", "no_recipe"),
            ("unmapped", "recipe_draft"),
            ("failed", "invalid_recipe"),
            ("failed", "missing_conversion"),
            ("failed", "computation_error"),
            ("pending", None),
            ("failed", "sale_ineligible"),  # → unknown
        ],
    )
    dims = (
        await build_item_insights(
            db,
            tenant_id=tid,
            item_id=item,
            window_key="14d",
            as_of=datetime.now(UTC),
            timezone_name="UTC",
            timezone_source="fallback",
            can_view_aggregated_cost=True,
            target_cover_days=7,
            target_source="default",
        )
    )["pos"]["dimensions"]

    rm = dims["recipe_mapping"]["historical_window"]
    eligible = rm["eligible_sale_line_count"]
    assert eligible == 9
    # recipe stage partitions eligible exactly once
    assert (
        rm["with_recipe_count"]
        + rm["no_recipe_count"]
        + rm["invalid_recipe_count"]
        + rm["pending_count"]
        + rm["unknown_count"]
        == eligible
    )
    # conversion stage partitions with_recipe
    cc = dims["conversion_coverage"]
    assert (
        cc["converted_count"] + cc["missing_conversion_count"] + cc["unknown_count"]
        == cc["with_recipe_count"]
    )
    # depletion stage partitions convertible
    de = dims["depletion_execution"]
    assert (
        de["depleted_count"] + de["depletion_failure_count"] + de["unknown_count"]
        == de["convertible_count"]
    )
    # no negative counts anywhere; every pct in [0,100] or None
    e2e = dims["end_to_end_coverage"]
    for pct in (
        rm["coverage_pct"],
        cc["coverage_pct"],
        de["coverage_pct"],
        e2e["line_coverage_pct"],
        e2e["revenue_coverage_pct"],
        e2e["effective_coverage_pct"],
    ):
        if pct is not None:
            assert Decimal("0") <= Decimal(pct) <= Decimal("100"), pct
    for v in (
        rm["with_recipe_count"],
        rm["no_recipe_count"],
        rm["pending_count"],
        rm["unknown_count"],
        cc["converted_count"],
        de["depleted_count"],
    ):
        assert v >= 0


# ── contributors reconcile to observed consumption ───────────────────────────


async def test_contributors_sum_to_observed_consumption(db: Any) -> None:
    now = datetime.now(UTC)
    tid = await _tenant(db)
    item = await _item(db, tid, mode="recipe_deducted")
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
        {"id": ib, "t": tid, "c": cid, "r": now - timedelta(days=3)},
    )
    order = (
        await db.execute(
            text(
                "INSERT INTO orders (id, tenant_id, pos_event_inbox_id, clover_order_id, state, "
                "payment_state, closed_at, processed_at) VALUES "
                "(:id,:t,:ib,'O','locked','PAID',:c,:c) RETURNING id"
            ),
            {"id": uuid.uuid4(), "t": tid, "ib": ib, "c": now - timedelta(days=3)},
        )
    ).scalar_one()
    # two menu items, each with a depleting sale line + movement
    total = Decimal("0")
    for n, (mag, qty) in enumerate({"A": (Decimal("6"), 24), "B": (Decimal("4"), 8)}.values()):
        mi = (
            await db.execute(
                text(
                    "INSERT INTO menu_items (tenant_id, pos_item_id, name, active) "
                    "VALUES (:t,:p,:nm,true) RETURNING id"
                ),
                {"t": tid, "p": f"PI{n}", "nm": f"Dish{n}"},
            )
        ).scalar_one()
        rec = (
            await db.execute(
                text(
                    "INSERT INTO recipes (tenant_id, menu_item_id, status) "
                    "VALUES (:t,:m,'confirmed') RETURNING id"
                ),
                {"t": tid, "m": mi},
            )
        ).scalar_one()
        rv = (
            await db.execute(
                text(
                    "INSERT INTO recipe_versions (tenant_id, recipe_id, version_number, "
                    "yield_quantity, name) VALUES (:t,:r,1,1,'v') RETURNING id"
                ),
                {"t": tid, "r": rec},
            )
        ).scalar_one()
        sli = (
            await db.execute(
                text(
                    "INSERT INTO sale_line_items (id, tenant_id, order_id, clover_line_item_id, "
                    "menu_item_id, recipe_version_id, name_at_sale, quantity, price_cents_at_sale, "
                    "net_revenue_cents, depletion_status) VALUES "
                    "(:id,:t,:o,:cl,:m,:rv,'d',:q,100,100,'depleted') RETURNING id"
                ),
                {
                    "id": uuid.uuid4(),
                    "t": tid,
                    "o": order,
                    "cl": f"CL{n}",
                    "m": mi,
                    "rv": rv,
                    "q": qty,
                },
            )
        ).scalar_one()
        await db.execute(
            text(
                "INSERT INTO inventory_movements (tenant_id, inventory_item_id, movement_type, "
                "delta, recorded_at, created_at, source_type, source_id, idempotency_key) "
                "VALUES (:t,:i,'sale_depletion',:d,:w,:w,'sale_line_item',:s,:k)"
            ),
            {
                "t": tid,
                "i": item,
                "d": str(-mag),
                "w": now - timedelta(days=2),
                "s": sli,
                "k": f"c:{sli}",
            },
        )
        total += mag

    out = await build_item_insights(
        db,
        tenant_id=tid,
        item_id=item,
        window_key="14d",
        as_of=now,
        timezone_name="UTC",
        timezone_source="fallback",
        can_view_aggregated_cost=True,
        target_cover_days=7,
        target_source="default",
    )
    contrib_sum = sum(Decimal(c["total_consumed"]) for c in out["contributors"])
    daily_sum = sum(Decimal(d["consumed"]) for d in out["consumption"]["daily"])
    assert contrib_sum == total == Decimal("10")
    assert daily_sum == total  # observed consumption reconciles to contributors


# ── oracle vs the GET /inventory/items list endpoint (Mode A) ────────────────
# The Stock-list Mode-B refund fix lives in PR #12; here we certify Mode A list
# equality (list is correct for Mode A on this base). After PR #12 merges and
# PR #11 rebases, the Mode-B list==on_hand equality (already proven by
# test_sprint6_modeb_list_balance in PR #12) is re-verified on the final SHA.


async def test_oracle_equals_list_endpoint_mode_a() -> None:
    from httpx import ASGITransport, AsyncClient

    from app.core.database import get_db_session
    from app.core.security import Principal, get_principal
    from app.main import create_app

    app = create_app()
    tid, uid = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    async with engine.connect() as conn:
        await conn.begin()
        bound = make_bound_session(conn)
        app.dependency_overrides[get_db_session] = lambda: bound
        app.dependency_overrides[get_principal] = lambda: Principal(
            user_id=str(uid),
            workos_id=f"w_{uid.hex[:8]}",
            email="x@test.com",
            tenant_id=str(tid),
            role="manager",
        )
        try:
            await conn.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id,:s,'ORCL')"),
                {"id": tid, "s": f"orcl-{tid.hex[:8]}"},
            )
            item = await _item(bound, tid, mode="recipe_deducted")
            await _mv(
                bound, tid, item, mtype="opening_balance", delta="12", when=now - timedelta(days=30)
            )
            await _mv(bound, tid, item, mtype="receive", delta="48", when=now - timedelta(days=8))
            await _mv(
                bound, tid, item, mtype="sale_depletion", delta="-6", when=now - timedelta(days=5)
            )
            await _mv(
                bound,
                tid,
                item,
                mtype="sale_depletion_reversal",
                delta="1.5",
                when=now - timedelta(days=4),
            )
            await _mv(
                bound, tid, item, mtype="count_adjust", delta="-1", when=now - timedelta(days=2)
            )

            oracle = await oracle_on_hand(bound, tid, item)
            assert oracle == Decimal("54.5")  # 12+48-6+1.5-1
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                r = await c.get("/api/v1/inventory/items")
                assert r.status_code == 200, r.text
                listed = next(x for x in r.json()["items"] if x["id"] == str(item))
                assert Decimal(str(listed["on_hand"])) == oracle
        finally:
            app.dependency_overrides.clear()
            await conn.rollback()
