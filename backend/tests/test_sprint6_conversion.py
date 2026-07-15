"""Sprint 6 — purchase-unit conversion workflow (the Lauzon-invoice regression).

The live smoke proved invoices arrive in CS/SAC/EA while inventory runs in
L/kg/ea, and that nothing bridged them: items got linked, commit had nothing it
could legally receive, on_hand stayed 0. These tests pin the whole bridge:

  suggestion math (pack hints / actual weight / remembered)
  → operator confirmation (PUT line: received_unit + factor, stash originals)
  → remembered prefill for the next receipt
  → commit gate (RECEIPT_CONVERSION_REQUIRED until confirmed)
  → movement in STORAGE units + on_hand
  → double-commit idempotency
  → depletion math (48 L − 10 × 0.25 L = 45.5 L)
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import engine, get_db_session, make_bound_session
from app.core.security import Principal, get_principal
from app.main import create_app
from app.modules.inventory.services import (
    ReceiptConversionRequired,
    commit_receipt,
    on_hand,
)
from app.modules.receipts.conversion import round_unit_cost_cents, suggest_conversion

pytestmark = pytest.mark.integration

D = Decimal


# ── suggestion math: the exact Lauzon lines ───────────────────────────────────


def test_milk_case_suggests_48L() -> None:
    s = suggest_conversion(
        purchase_qty=D(3),
        purchase_unit="CS",
        storage_unit="L",
        pack_count=D(4),
        pack_size_qty=D(4),
        pack_size_unit="L",
    )
    assert s is not None
    assert s.quantity == D(48)
    assert s.factor == D(16)
    assert s.source == "extracted_suggestion"


def test_cream_case_suggests_12L() -> None:
    s = suggest_conversion(
        purchase_qty=D(1),
        purchase_unit="CS",
        storage_unit="L",
        pack_count=D(12),
        pack_size_qty=D(1),
        pack_size_unit="L",
    )
    assert s is not None and s.quantity == D(12) and s.factor == D(12)


def test_oat_case_suggests_11_352L() -> None:
    s = suggest_conversion(
        purchase_qty=D(2),
        purchase_unit="CS",
        storage_unit="L",
        pack_count=D(6),
        pack_size_qty=D(946),
        pack_size_unit="ML",
    )
    assert s is not None
    assert s.quantity == D("11.352")
    assert s.factor == D("5.676")


def test_oat_single_units_into_L_storage() -> None:
    s = suggest_conversion(
        purchase_qty=D(4),
        purchase_unit="EA",
        storage_unit="L",
        pack_size_qty=D(946),
        pack_size_unit="ML",
    )
    assert s is not None and s.quantity == D("3.784")


def test_goblet_case_suggests_1000_ea() -> None:
    s = suggest_conversion(
        purchase_qty=D(1),
        purchase_unit="CS",
        storage_unit="ea",
        pack_count=D(1000),
        pack_size_qty=D(1),
        pack_size_unit="CT",
    )
    assert s is not None and s.quantity == D(1000) and s.factor == D(1000)


def test_sugar_bags_into_kg_storage() -> None:
    s = suggest_conversion(
        purchase_qty=D(3),
        purchase_unit="EA",
        storage_unit="kg",
        pack_size_qty=D(2),
        pack_size_unit="KG",
    )
    assert s is not None and s.quantity == D(6) and s.factor == D(2)


def test_espresso_prefers_actual_weight_over_nominal() -> None:
    s = suggest_conversion(
        purchase_qty=D(2),
        purchase_unit="SAC",
        storage_unit="kg",
        pack_size_qty=D(5),
        pack_size_unit="KG",  # nominal 2 x 5 = 10 kg
        actual_weight_qty=D("10.18"),
        actual_weight_unit="KG",  # printed catch weight wins
    )
    assert s is not None
    assert s.quantity == D("10.18")
    assert s.factor == D("5.09")


def test_remembered_factor_beats_hints() -> None:
    s = suggest_conversion(
        purchase_qty=D(3),
        purchase_unit="CS",
        storage_unit="L",
        pack_count=D(4),
        pack_size_qty=D(4),
        pack_size_unit="L",
        remembered_factor=D(16),
    )
    assert s is not None and s.source == "remembered" and s.quantity == D(48)


def test_identity_when_purchase_unit_is_storage_unit() -> None:
    s = suggest_conversion(purchase_qty=D(6), purchase_unit="EA", storage_unit="ea")
    assert s is not None and s.quantity == D(6) and s.source == "identity"


def test_no_hints_no_guess() -> None:
    assert suggest_conversion(purchase_qty=D(3), purchase_unit="CS", storage_unit="L") is None


def test_unit_cost_per_storage_unit() -> None:
    assert round_unit_cost_cents(8244, D(48)) == 172  # 171.75 → 172


# ── HTTP: confirm round-trip + remembered prefill ─────────────────────────────


@pytest.fixture(scope="module")
def app_instance() -> Any:
    return create_app()


@pytest.fixture
async def conn(app_instance: Any) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as connection:
        await connection.begin()
        bound = make_bound_session(connection)
        app_instance.dependency_overrides[get_db_session] = lambda: bound
        try:
            yield connection
        finally:
            app_instance.dependency_overrides.clear()
            await connection.rollback()


@pytest.fixture
async def client(app_instance: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app_instance), base_url="http://test") as c:
        yield c


def _as(app_instance: Any, tenant_id: str, user_id: str, role: str = "staff") -> None:
    app_instance.dependency_overrides[get_principal] = lambda: Principal(
        user_id=user_id,
        workos_id=f"w_{user_id[:8]}",
        email="x@test.com",
        tenant_id=tenant_id,
        role=role,  # type: ignore[arg-type]
    )


async def _seed_milk(conn: AsyncConnection) -> dict[str, Any]:
    """Tenant + item 'LAIT 3.25%' (storage L) + draft with the milk line:
    3 CS, pack hints 4x4L, cost 2748¢/CS."""
    tid, uid = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'Lauzon T', :slug)"),
        {"id": tid, "slug": f"lz-{tid.hex[:8]}"},
    )
    await conn.execute(
        text("INSERT INTO users (id, workos_id, email) VALUES (:id, :w, :e)"),
        {"id": uid, "w": f"w_{uid.hex[:8]}", "e": f"{uid.hex[:8]}@test.com"},
    )
    unit_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO units_of_measure (id, tenant_id, name, abbreviation, unit_type) "
            "VALUES (:id, :tid, 'L', 'L', 'volume')"
        ),
        {"id": unit_id, "tid": tid},
    )
    item_id = uuid.uuid4()
    await conn.execute(
        text("""
            INSERT INTO inventory_items
                (id, tenant_id, name, inventory_mode, storage_unit_id, recipe_unit_id)
            VALUES (:id, :tid, 'LAIT 3.25%', 'recipe_deducted', :uid, :uid)
        """),
        {"id": item_id, "tid": tid, "uid": unit_id},
    )
    receipt_id = uuid.uuid4()
    await conn.execute(
        text("""
            INSERT INTO receipts (id, tenant_id, commit_state, source, extraction_status)
            VALUES (:id, :tid, 'draft', 'mobile_photo', 'complete')
        """),
        {"id": receipt_id, "tid": tid},
    )
    line_id = uuid.uuid4()
    await conn.execute(
        text("""
            INSERT INTO receipt_lines
                (id, tenant_id, receipt_id, extracted_name, received_quantity,
                 extracted_unit, unit_cost_cents, confidence, match_status, line_ordinal,
                 pack_count, pack_size_qty, pack_size_unit)
            VALUES (:id, :tid, :rid, 'LAIT 3.25% 4x4L', 3, 'CS', 2748, 0.97,
                    'unmatched', 0, 4, 4, 'L')
        """),
        {"id": line_id, "tid": tid, "rid": receipt_id},
    )
    return {
        "tenant_id": tid,
        "user_id": uid,
        "item_id": item_id,
        "receipt_id": receipt_id,
        "line_id": line_id,
    }


async def test_confirm_conversion_round_trip_and_remember(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed_milk(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))
    url = f"/api/v1/receipts/{s['receipt_id']}/lines/{s['line_id']}"

    # Link → detail suggests 48 L @ factor 16 from the 4x4L pack hints.
    r = await client.put(url, json={"inventory_item_id": str(s["item_id"])})
    assert r.status_code == 200, r.text
    assert r.json()["item_storage_unit"] == "L"
    detail = await client.get(f"/api/v1/receipts/{s['receipt_id']}")
    [line] = detail.json()["lines"]
    assert line["suggested_quantity"] == 48.0
    assert line["suggested_factor"] == 16.0
    assert line["suggestion_source"] == "extracted_suggestion"

    # Operator confirms: receive 48 L, 1 CS = 16 L, cost 172¢/L, remember it.
    r = await client.put(
        url,
        json={
            "received_quantity": 48,
            "received_unit": "L",
            "conversion_factor": 16,
            "unit_cost_cents": 172,
            "remember_conversion": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["received_unit"] == "L"
    assert body["received_quantity"] == 48.0
    assert body["conversion_source"] == "operator_confirmed"
    assert body["conversion_confirmed_at"] is not None
    # Invoice originals stashed, invoice text untouched.
    assert body["purchase_quantity"] == 3.0
    assert body["purchase_unit"] == "CS"
    assert body["extracted_unit"] == "CS"

    remembered = (
        await conn.execute(
            text(
                "SELECT factor, storage_unit FROM tenant_item_purchase_conversions "
                "WHERE tenant_id=:t AND inventory_item_id=:i AND purchase_unit='CS'"
            ),
            {"t": s["tenant_id"], "i": s["item_id"]},
        )
    ).fetchone()
    assert remembered is not None
    assert Decimal(str(remembered[0])) == D(16) and remembered[1] == "L"

    # A NEW line for the same item + CS prefills from the remembered factor.
    lid2 = uuid.uuid4()
    await conn.execute(
        text("""
            INSERT INTO receipt_lines
                (id, tenant_id, receipt_id, extracted_name, received_quantity,
                 extracted_unit, match_status, line_ordinal, inventory_item_id)
            VALUES (:id, :tid, :rid, 'LAIT 3.25% 4x4L', 2, 'CS', 'matched', 1, :iid)
        """),
        {"id": lid2, "tid": s["tenant_id"], "rid": s["receipt_id"], "iid": s["item_id"]},
    )
    detail = await client.get(f"/api/v1/receipts/{s['receipt_id']}")
    line2 = next(ln for ln in detail.json()["lines"] if ln["id"] == str(lid2))
    assert line2["suggestion_source"] == "remembered"
    assert line2["suggested_quantity"] == 32.0  # 2 CS x remembered 16


async def test_confirm_requires_linked_line(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed_milk(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))
    r = await client.put(
        f"/api/v1/receipts/{s['receipt_id']}/lines/{s['line_id']}",
        json={"received_quantity": 48, "received_unit": "L", "conversion_factor": 16},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "RECEIPT_LINE_NOT_LINKED"


# ── commit gate + movement + on_hand + idempotency + depletion ────────────────


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


async def _seed_commitable(db: Any) -> dict[str, Any]:
    """Milk scenario at the service layer: linked line, 3 CS, unconfirmed."""
    tid, item_id, unit_id, rid, lid = (uuid.uuid4() for _ in range(5))
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'S6C')"),
        {"id": tid, "s": f"s6c-{tid.hex[:8]}"},
    )
    await db.execute(
        text(
            "INSERT INTO units_of_measure (id, tenant_id, name, abbreviation, unit_type) "
            "VALUES (:id, :t, 'L', 'L', 'volume')"
        ),
        {"id": unit_id, "t": tid},
    )
    await db.execute(
        text(
            "INSERT INTO inventory_items (id, tenant_id, name, inventory_mode, "
            "storage_unit_id, recipe_unit_id) "
            "VALUES (:id, :t, 'LAIT 3.25%', 'recipe_deducted', :u, :u)"
        ),
        {"id": item_id, "t": tid, "u": unit_id},
    )
    await db.execute(
        text(
            "INSERT INTO receipts (id, tenant_id, commit_state, source) "
            "VALUES (:id, :t, 'draft', 'manual')"
        ),
        {"id": rid, "t": tid},
    )
    await db.execute(
        text("""
            INSERT INTO receipt_lines
                (id, tenant_id, receipt_id, extracted_name, received_quantity,
                 extracted_unit, unit_cost_cents, line_total_cents, match_status,
                 manually_corrected, inventory_item_id, line_ordinal)
            VALUES (:id, :t, :r, 'LAIT 3.25% 4x4L', 3, 'CS', 2748, 8244, 'matched',
                    true, :i, 0)
        """),
        {"id": lid, "t": tid, "r": rid, "i": item_id},
    )
    return {"tenant_id": tid, "item_id": item_id, "receipt_id": rid, "line_id": lid}


async def _confirm_line_sql(db: Any, s: dict[str, Any]) -> None:
    """The state update_line's confirm branch produces (SQL to stay service-pure)."""
    await db.execute(
        text("""
            UPDATE receipt_lines SET
                purchase_quantity = received_quantity, purchase_unit = extracted_unit,
                received_quantity = 48, received_unit = 'L', conversion_factor = 16,
                conversion_source = 'operator_confirmed', conversion_confirmed_at = now(),
                unit_cost_cents = 172
             WHERE id = :lid
        """),
        {"lid": s["line_id"]},
    )


async def test_commit_blocked_until_conversion_confirmed(db: Any) -> None:
    s = await _seed_commitable(db)
    with pytest.raises(ReceiptConversionRequired):
        await commit_receipt(
            db,
            tenant_id=s["tenant_id"],
            receipt_id=s["receipt_id"],
            confirm=True,
            reviewed_affirmation=True,
        )
    n = (
        await db.execute(
            text("SELECT count(*) FROM inventory_movements WHERE tenant_id=:t"),
            {"t": s["tenant_id"]},
        )
    ).scalar_one()
    assert n == 0  # blocked cleanly, zero partial movements


async def test_commit_after_confirm_moves_48L_and_costs_per_L(db: Any) -> None:
    s = await _seed_commitable(db)
    await _confirm_line_sql(db, s)
    result = await commit_receipt(
        db,
        tenant_id=s["tenant_id"],
        receipt_id=s["receipt_id"],
        confirm=True,
        reviewed_affirmation=True,
    )
    assert result["status"] == "committed"

    mv = (
        await db.execute(
            text(
                "SELECT movement_type, delta FROM inventory_movements "
                "WHERE tenant_id=:t AND inventory_item_id=:i"
            ),
            {"t": s["tenant_id"], "i": s["item_id"]},
        )
    ).fetchall()
    assert len(mv) == 1
    assert mv[0][0] == "receive"
    assert Decimal(str(mv[0][1])) == D(48)  # STORAGE units, not 3 CS

    # Cost snapshot: per-storage-unit cents, not per-case.
    snap = (
        await db.execute(
            text(
                "SELECT unit_cost_cents, unit_cost_cents_exact FROM ingredient_cost_snapshots "
                "WHERE tenant_id=:t AND inventory_item_id=:i"
            ),
            {"t": s["tenant_id"], "i": s["item_id"]},
        )
    ).fetchall()
    assert len(snap) == 1
    assert snap[0][0] == 172  # rounded legacy column
    assert Decimal(str(snap[0][1])) == D("171.75")  # exact: 8244 / 48 L

    oh = await on_hand(db, tenant_id=s["tenant_id"], inventory_item_id=s["item_id"])
    assert oh is not None and Decimal(str(oh)) == D(48)

    # Double-commit: idempotent, no double-add.
    again = await commit_receipt(
        db,
        tenant_id=s["tenant_id"],
        receipt_id=s["receipt_id"],
        confirm=True,
        reviewed_affirmation=True,
    )
    assert again["status"] == "already_committed"
    n = (
        await db.execute(
            text("SELECT count(*) FROM inventory_movements WHERE tenant_id=:t"),
            {"t": s["tenant_id"]},
        )
    ).scalar_one()
    assert n == 1
    oh2 = await on_hand(db, tenant_id=s["tenant_id"], inventory_item_id=s["item_id"])
    assert oh2 is not None and Decimal(str(oh2)) == D(48)


async def test_depletion_math_48L_minus_10_sales_of_quarter_L(db: Any) -> None:
    """The founder's acceptance math: 10 sales x 0.25 L → 48 − 2.5 = 45.5 L."""
    s = await _seed_commitable(db)
    await _confirm_line_sql(db, s)
    await commit_receipt(
        db,
        tenant_id=s["tenant_id"],
        receipt_id=s["receipt_id"],
        confirm=True,
        reviewed_affirmation=True,
    )
    for i in range(10):
        await db.execute(
            text("""
                INSERT INTO inventory_movements
                    (id, tenant_id, inventory_item_id, movement_type, delta,
                     source_type, idempotency_key)
                VALUES (:id, :t, :i, 'sale_depletion', -0.25, 'pos_order', :k)
            """),
            {
                "id": uuid.uuid4(),
                "t": s["tenant_id"],
                "i": s["item_id"],
                "k": f"conv-test-sale-{s['line_id']}-{i}",
            },
        )
    oh = await on_hand(db, tenant_id=s["tenant_id"], inventory_item_id=s["item_id"])
    assert oh is not None and Decimal(str(oh)) == D("45.5")


# ── cost precision: the full Lauzon examples table (blocker 1) ────────────────


async def _seed_costed_line(
    db: Any,
    tid: uuid.UUID,
    rid: uuid.UUID,
    ordinal: int,
    *,
    name: str,
    storage: str,
    unit_type: str,
    line_total: int,
    purchase_qty: str,
    purchase_unit: str,
    recv_qty: str,
    factor: str,
) -> uuid.UUID:
    unit_id = (
        await db.execute(
            text(
                "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
                "VALUES (:t, :n, :n, :ut) ON CONFLICT (tenant_id, name) DO UPDATE "
                "SET abbreviation=EXCLUDED.abbreviation RETURNING id"
            ),
            {"t": tid, "n": storage, "ut": unit_type},
        )
    ).scalar_one()
    item_id = (
        await db.execute(
            text(
                "INSERT INTO inventory_items (tenant_id, name, inventory_mode, "
                "storage_unit_id, recipe_unit_id) VALUES (:t, :n, 'recipe_deducted', :u, :u) "
                "RETURNING id"
            ),
            {"t": tid, "n": name, "u": unit_id},
        )
    ).scalar_one()
    await db.execute(
        text("""
            INSERT INTO receipt_lines
                (tenant_id, receipt_id, extracted_name, received_quantity, received_unit,
                 extracted_unit, purchase_quantity, purchase_unit, conversion_factor,
                 conversion_source, conversion_confirmed_at, line_total_cents,
                 match_status, manually_corrected, inventory_item_id, line_ordinal)
            VALUES (:t, :r, :n, CAST(:rq AS numeric), :ru, :pu, CAST(:pq AS numeric), :pu,
                    CAST(:f AS numeric), 'operator_confirmed', now(), :lt, 'matched',
                    true, :i, :o)
        """),
        {
            "t": tid,
            "r": rid,
            "n": name,
            "rq": recv_qty,
            "ru": storage,
            "pu": purchase_unit,
            "pq": purchase_qty,
            "f": factor,
            "lt": line_total,
            "i": item_id,
            "o": ordinal,
        },
    )
    return item_id


async def test_cost_snapshots_exact_lauzon_examples(db: Any) -> None:
    """Blocker-1 acceptance: exact NUMERIC unit costs from printed line totals.
    milk 8244/48=171.75 ¢/L · espresso 18171/10.18=1784.9706 ¢/kg ·
    oat 6588/11352=0.5803 ¢/ml · goblets 9230/1000=9.23 ¢/ea."""
    tid, rid = uuid.uuid4(), uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'CostP')"),
        {"id": tid, "s": f"cp-{tid.hex[:8]}"},
    )
    await db.execute(
        text(
            "INSERT INTO receipts (id, tenant_id, commit_state, source) "
            "VALUES (:id, :t, 'draft', 'manual')"
        ),
        {"id": rid, "t": tid},
    )
    cases = [
        # (name, storage, unit_type, line_total, pq, pu, rq, factor, expected_exact, expected_int)
        ("LAIT 3.25%", "L", "volume", 8244, "3", "CS", "48", "16", "171.75", 172),
        ("ESPRESSO", "kg", "weight", 18171, "2", "SAC", "10.18", "5.09", "1784.9705", 1785),
        ("AVOINE", "ml", "volume", 6588, "2", "CS", "11352", "5676", "0.5803", 1),
        ("GOBELET", "ea", "count", 9230, "1", "CS", "1000", "1000", "9.23", 9),
    ]
    items = {}
    for i, (name, storage, ut, lt, pq, pu, rq, f, _, _e) in enumerate(cases):
        items[name] = await _seed_costed_line(
            db,
            tid,
            rid,
            i,
            name=name,
            storage=storage,
            unit_type=ut,
            line_total=lt,
            purchase_qty=pq,
            purchase_unit=pu,
            recv_qty=rq,
            factor=f,
        )

    result = await commit_receipt(
        db, tenant_id=tid, receipt_id=rid, confirm=True, reviewed_affirmation=True
    )
    assert result["status"] == "committed"

    for name, _storage, _ut, lt, _pq, _pu, rq, _f, expected_exact, expected_int in cases:
        row = (
            await db.execute(
                text(
                    "SELECT unit_cost_cents, unit_cost_cents_exact "
                    "FROM ingredient_cost_snapshots WHERE tenant_id=:t "
                    "AND inventory_item_id=:i"
                ),
                {"t": tid, "i": items[name]},
            )
        ).fetchone()
        assert row is not None, name
        assert row[0] == expected_int, f"{name} int"
        got = Decimal(str(row[1]))
        assert abs(got - D(expected_exact)) <= D("0.0001"), f"{name} exact: {got}"
        # storage never destroyed the precision display would round away
        mv = (
            await db.execute(
                text(
                    "SELECT delta FROM inventory_movements WHERE tenant_id=:t "
                    "AND inventory_item_id=:i AND movement_type='receive'"
                ),
                {"t": tid, "i": items[name]},
            )
        ).scalar_one()
        assert Decimal(str(mv)) == D(rq), f"{name} movement"


# ── conversion consistency (blocker 2) ────────────────────────────────────────


async def test_inconsistent_conversion_rejected_422(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    """The live-smoke escape: qty 2 CS, factor 5676, received_quantity 2 —
    the three numbers must agree or the confirm is rejected wholesale."""
    s = await _seed_milk(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))
    url = f"/api/v1/receipts/{s['receipt_id']}/lines/{s['line_id']}"
    r = await client.put(url, json={"inventory_item_id": str(s["item_id"])})
    assert r.status_code == 200

    r = await client.put(
        url,
        json={"received_quantity": 2, "received_unit": "L", "conversion_factor": 5676},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "RECEIPT_CONVERSION_INCONSISTENT"
    # nothing changed on the line
    detail = await client.get(f"/api/v1/receipts/{s['receipt_id']}")
    [line] = detail.json()["lines"]
    assert line["received_unit"] is None
    assert line["conversion_confirmed_at"] is None

    # tolerance: quantized factors still pass (10.18 vs 2 x 5.09 exact)
    r = await client.put(
        url,
        json={"received_quantity": 48, "received_unit": "L", "conversion_factor": 16},
    )
    assert r.status_code == 200


# ── dimension mismatch warning (blocker 3) ────────────────────────────────────


async def test_count_invoice_vs_weight_item_warns(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed_milk(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))
    # a weight-stored item (like the mis-picked oz_weight goblets)
    unit_id, item_id, lid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO units_of_measure (id, tenant_id, name, abbreviation, unit_type) "
            "VALUES (:id, :t, 'oz_weight', 'oz', 'weight')"
        ),
        {"id": unit_id, "t": s["tenant_id"]},
    )
    await conn.execute(
        text(
            "INSERT INTO inventory_items (id, tenant_id, name, inventory_mode, "
            "storage_unit_id, recipe_unit_id) "
            "VALUES (:id, :t, 'GOBELET CARTON', 'recipe_deducted', :u, :u)"
        ),
        {"id": item_id, "t": s["tenant_id"], "u": unit_id},
    )
    await conn.execute(
        text("""
            INSERT INTO receipt_lines
                (id, tenant_id, receipt_id, extracted_name, received_quantity,
                 extracted_unit, match_status, line_ordinal, inventory_item_id,
                 pack_count, pack_size_qty, pack_size_unit)
            VALUES (:id, :t, :r, 'GOBELET 12OZ 1000CT', 1, 'CS', 'matched', 5, :i,
                    1000, 1, 'CT')
        """),
        {"id": lid, "t": s["tenant_id"], "r": s["receipt_id"], "i": item_id},
    )
    detail = await client.get(f"/api/v1/receipts/{s['receipt_id']}")
    lines = {ln["id"]: ln for ln in detail.json()["lines"]}
    assert lines[str(lid)]["unit_mismatch_warning"] is True  # CT clue vs oz_weight
    # milk (L clue, unlinked) carries no warning
    assert lines[str(s["line_id"])]["unit_mismatch_warning"] is False
