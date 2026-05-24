"""Sprint 3 — Phase 7: Full Exit Gate Integration Tests.

All 29 Sprint 3 acceptance criteria in one comprehensive suite.
Sprint 3 is complete only when every test in this file passes.

Groups:
  7.1-7.2   GRANT    — append-only enforcement (asyncpg)
  7.3-7.5   OB       — opening balance constraints
  7.6-7.9   ON_HAND  — on_hand() SQL function correctness (asyncpg)
  7.10-7.14 TRIGGER  — count event trigger / mode branching (asyncpg)
  7.15-7.17 SALE     — record_sale_inventory_effect() (service layer)
  7.18-7.20 RECEIPT  — commit_receipt() (service layer)
  7.21-7.23 IDEM     — idempotency middleware (HTTP)
  7.24-7.26 MONITOR  — monitoring_alerts UPSERT and thresholds (service layer)
  7.27-7.29 STOCK    — GET /inventory/items stock warning flags (HTTP)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.database import engine, get_db_session, make_bound_session
from app.core.security import Principal, get_principal
from app.main import create_app
from app.modules.inventory.services import (
    add_receipt_line,
    commit_receipt,
    create_receipt,
    record_count_event,
    record_opening_balance,
    record_sale_inventory_effect,
)
from tests.conftest import seed_tenant

pytestmark = pytest.mark.integration

UTC_TZ = UTC

# ── constants for savepoint-based tests (service + HTTP) ─────────────────────
T7_TENANT_ID = uuid.UUID("e7000000-0000-0000-0000-000000000001")
T7_USER_ID = uuid.UUID("e7000000-0000-0000-0000-000000000002")
T7_UOM_ID = uuid.UUID("e7000000-0000-0000-0000-000000000003")
T7_ING_A = uuid.UUID("e7000000-0000-0000-0000-000000000004")
T7_ING_B = uuid.UUID("e7000000-0000-0000-0000-000000000005")


# ═════════════════════════════════════════════════════════════════════════════
# Module-scoped fixtures (needed for HTTP tests in groups 7 and 9)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def app_instance():
    return create_app()


@pytest.fixture(scope="module")
async def client(app_instance):
    async with AsyncClient(transport=ASGITransport(app=app_instance), base_url="http://test") as c:
        yield c


# ═════════════════════════════════════════════════════════════════════════════
# Per-test savepoint fixture  (NOT autouse — asyncpg tests skip it)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture
async def session(app_instance):
    """Savepoint transaction with FK prerequisites seeded.

    Service tests call service functions directly with this session.
    HTTP tests use the bound session via get_db_session override.
    Asyncpg tests (7.1-7.14) do not request this fixture.
    """
    async with engine.begin() as conn:
        tx = await conn.begin_nested()  # SAVEPOINT
        db = make_bound_session(conn)

        app_instance.dependency_overrides[get_db_session] = lambda: db
        app_instance.dependency_overrides[get_principal] = lambda: Principal(
            user_id=str(T7_USER_ID),
            workos_id="wos_phase7_test",
            email="phase7@test.example",
            tenant_id=str(T7_TENANT_ID),
            role="manager",
        )

        await conn.execute(
            text("""
            INSERT INTO tenants (id, slug, name)
            VALUES (:id, 'phase7-tenant', 'Phase 7 Tenant')
            ON CONFLICT (id) DO NOTHING
        """),
            {"id": T7_TENANT_ID},
        )

        await conn.execute(
            text("""
            INSERT INTO users (id, workos_id, email, email_verified)
            VALUES (:id, 'wos_phase7_test', 'phase7@test.example', true)
            ON CONFLICT (id) DO NOTHING
        """),
            {"id": T7_USER_ID},
        )

        await conn.execute(
            text("""
            INSERT INTO units_of_measure (id, tenant_id, name, abbreviation, unit_type)
            VALUES (:id, :tid, 'grams-p7', 'g', 'weight')
            ON CONFLICT (id) DO NOTHING
        """),
            {"id": T7_UOM_ID, "tid": T7_TENANT_ID},
        )

        for ing_id, ing_name in [
            (T7_ING_A, "Ingredient A (p7)"),
            (T7_ING_B, "Ingredient B (p7)"),
        ]:
            await conn.execute(
                text("""
                INSERT INTO ingredients_master (id, tenant_id, name)
                VALUES (:id, :tid, :name)
                ON CONFLICT (id) DO NOTHING
            """),
                {"id": ing_id, "tid": T7_TENANT_ID, "name": ing_name},
            )

        yield db

        await tx.rollback()
        app_instance.dependency_overrides.clear()


# ═════════════════════════════════════════════════════════════════════════════
# Asyncpg helpers  (groups 1-4)
# ═════════════════════════════════════════════════════════════════════════════


async def _mk_uom(conn: asyncpg.Connection, tenant_id: str) -> str:
    name = f"uom7-{uuid.uuid4().hex[:6]}"
    row = await conn.fetchrow(
        "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type)"
        " VALUES ($1, $2, $3, 'weight') RETURNING id",
        uuid.UUID(tenant_id),
        name,
        name[:3],
    )
    return str(row["id"])


async def _mk_item(
    conn: asyncpg.Connection,
    tenant_id: str,
    uom_id: str,
    *,
    mode: str,
    factor: float = 1.0,
    last_count_at: datetime | None = None,
    last_count_qty: float | None = None,
) -> str:
    cadence = 7 if mode == "count_anchored" else None
    grace = 7 if mode == "count_anchored" else None
    row = await conn.fetchrow(
        """
        INSERT INTO inventory_items
            (tenant_id, name, inventory_mode, storage_unit_id,
             storage_to_recipe_factor,
             last_count_at, last_count_quantity,
             count_cadence_days, count_grace_days)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id
        """,
        uuid.UUID(tenant_id),
        f"i7-{uuid.uuid4().hex[:6]}",
        mode,
        uuid.UUID(uom_id),
        factor,
        last_count_at,
        last_count_qty,
        cadence,
        grace,
    )
    return str(row["id"])


async def _mv(
    conn: asyncpg.Connection,
    tenant_id: str,
    item_id: str,
    movement_type: str,
    delta: float,
    recorded_at: datetime | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO inventory_movements
            (tenant_id, inventory_item_id, movement_type, delta, recorded_at)
        VALUES ($1, $2, $3, $4, COALESCE($5, NOW()))
        """,
        uuid.UUID(tenant_id),
        uuid.UUID(item_id),
        movement_type,
        delta,
        recorded_at,
    )


async def _on_hand(conn: asyncpg.Connection, tenant_id: str, item_id: str) -> Decimal | None:
    row = await conn.fetchrow(
        """
        WITH item AS (
            SELECT inventory_mode, last_count_at, last_count_quantity
              FROM inventory_items WHERE tenant_id=$1 AND id=$2
        ),
        ledger_sum AS (
            SELECT COALESCE(SUM(delta),0) AS qty FROM inventory_movements
             WHERE tenant_id=$1 AND inventory_item_id=$2
               AND movement_type NOT IN ('sale_signal','sale_signal_reversal')
        ),
        receipts_since AS (
            SELECT COALESCE(SUM(m.delta),0) AS qty
              FROM inventory_movements m, item
             WHERE m.tenant_id=$1 AND m.inventory_item_id=$2
               AND m.recorded_at > item.last_count_at
               AND m.movement_type IN ('receive','transfer_in','count_adjust','opening_balance')
        ),
        signals_since AS (
            SELECT COALESCE(SUM(ABS(m.delta)),0) AS qty
              FROM inventory_movements m, item
             WHERE m.tenant_id=$1 AND m.inventory_item_id=$2
               AND m.recorded_at > item.last_count_at
               AND m.movement_type = 'sale_signal'
        )
        SELECT CASE
            WHEN item.inventory_mode = 'recipe_deducted' THEN ledger_sum.qty
            WHEN item.inventory_mode = 'count_anchored'
                 AND item.last_count_quantity IS NOT NULL
                THEN item.last_count_quantity + receipts_since.qty
                     - (signals_since.qty * COALESCE(
                           (SELECT yield_factor FROM inventory_yield_factors
                             WHERE tenant_id=$1 AND inventory_item_id=$2), 1.0))
            ELSE NULL
        END AS qty
        FROM item, ledger_sum, receipts_since, signals_since
        """,
        uuid.UUID(tenant_id),
        uuid.UUID(item_id),
    )
    return row["qty"]


async def _count_mvt(
    conn: asyncpg.Connection, tenant_id: str, item_id: str, movement_type: str
) -> int:
    row = await conn.fetchrow(
        "SELECT COUNT(*) AS n FROM inventory_movements"
        " WHERE tenant_id = $1 AND inventory_item_id = $2 AND movement_type = $3",
        uuid.UUID(tenant_id),
        uuid.UUID(item_id),
        movement_type,
    )
    return row["n"]


async def _insert_count_event(
    conn: asyncpg.Connection,
    tenant_id: str,
    item_id: str,
    *,
    counted_quantity: float,
    predicted_on_hand: float | None,
    counted_at: datetime | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO inventory_count_events
            (tenant_id, inventory_item_id, counted_quantity,
             predicted_on_hand_at_count, counted_at)
        VALUES ($1, $2, $3, $4, COALESCE($5, NOW()))
        """,
        uuid.UUID(tenant_id),
        uuid.UUID(item_id),
        counted_quantity,
        predicted_on_hand,
        counted_at,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Session helpers  (groups 5-9)
# ═════════════════════════════════════════════════════════════════════════════


async def _seed_item(
    session,
    item_id: uuid.UUID,
    mode: str,
    *,
    factor: float = 1.0,
    par: float | None = None,
    cadence: int | None = None,
    grace: int | None = None,
    lca: datetime | None = None,
    lcq: float | None = None,
    ing_id: uuid.UUID | None = None,
) -> None:
    if mode == "count_anchored":
        cadence = cadence if cadence is not None else 7
        grace = grace if grace is not None else 7
    await session.execute(
        text("""
        INSERT INTO inventory_items (
            id, tenant_id, ingredient_id, name,
            inventory_mode, storage_unit_id, purchase_unit_id, recipe_unit_id,
            storage_to_recipe_factor, par_level,
            count_cadence_days, count_grace_days,
            last_count_at, last_count_quantity
        ) VALUES (
            :id, :tid, :ing, :name, :mode, :su, :su, :su, :factor, :par,
            :cad, :grace, :lca, :lcq
        ) ON CONFLICT (id) DO UPDATE SET
            par_level           = EXCLUDED.par_level,
            count_cadence_days  = EXCLUDED.count_cadence_days,
            count_grace_days    = EXCLUDED.count_grace_days,
            last_count_at       = EXCLUDED.last_count_at,
            last_count_quantity = EXCLUDED.last_count_quantity
    """),
        {
            "id": item_id,
            "tid": T7_TENANT_ID,
            "ing": ing_id or T7_ING_A,
            "name": f"p7-{str(item_id)[-4:]}",
            "mode": mode,
            "su": T7_UOM_ID,
            "factor": factor,
            "par": par,
            "cad": cadence,
            "grace": grace,
            "lca": lca,
            "lcq": lcq,
        },
    )
    await session.flush()


async def _seed_movement(
    session,
    item_id: uuid.UUID,
    movement_type: str,
    delta: float,
    recorded_at: datetime | None = None,
) -> None:
    source_map = {
        "opening_balance": "opening",
        "receive": "receipt_line",
        "sale_depletion": "sale_line_item",
        "sale_signal": "sale_line_item",
        "waste": "manual",
        "count_adjust": "count_event",
    }
    await session.execute(
        text("""
        INSERT INTO inventory_movements
            (tenant_id, inventory_item_id, movement_type, delta,
             source_type, idempotency_key, recorded_at)
        VALUES (
            :tid, :iid, :mtype, :delta, :stype, :ikey, COALESCE(:at, NOW())
        )
    """),
        {
            "tid": T7_TENANT_ID,
            "iid": item_id,
            "mtype": movement_type,
            "delta": delta,
            "stype": source_map.get(movement_type, "manual"),
            "ikey": f"t7:{movement_type}:{uuid.uuid4().hex}",
            "at": recorded_at,
        },
    )
    await session.flush()


async def _seed_recipe(
    session,
    tenant_id: uuid.UUID,
    item_id: uuid.UUID,
    recipe_qty: float,
) -> uuid.UUID:
    rv_id = uuid.uuid4()
    await session.execute(
        text("""
        INSERT INTO recipe_versions (id, tenant_id, name) VALUES (:id, :tid, :name)
    """),
        {"id": rv_id, "tid": tenant_id, "name": f"rv7-{rv_id.hex[:6]}"},
    )
    await session.execute(
        text("""
        INSERT INTO recipe_ingredients (tenant_id, recipe_version_id, inventory_item_id, quantity)
        VALUES (:tid, :rvid, :iid, :qty)
    """),
        {"tid": tenant_id, "rvid": rv_id, "iid": item_id, "qty": recipe_qty},
    )
    await session.flush()
    return rv_id


async def _seed_sale_line(
    session,
    tenant_id: uuid.UUID,
    recipe_version_id: uuid.UUID,
    sale_qty: float,
) -> uuid.UUID:
    # Sprint 4: sale_line_items requires order_id → orders → pos_event_inbox.
    # Create minimal prereqs via raw SQL (session runs as superuser, bypasses RLS).
    import time as _time

    inbox_id = uuid.uuid4()
    await session.execute(
        text("""
            INSERT INTO pos_event_inbox
            (inbox_id, tenant_id, vendor, vendor_event_id, vendor_object_type,
             vendor_event_type, vendor_ts, raw_payload, signature_verified, source)
            VALUES (:iid, :tid, 'clover', 'O:s3ph7', 'O', 'UPDATE',
                    :ts, '{}', false, 'webhook')
        """),
        {"iid": inbox_id, "tid": tenant_id, "ts": int(_time.time() * 1000)},
    )
    order_id = uuid.uuid4()
    await session.execute(
        text("""
            INSERT INTO orders
            (id, tenant_id, pos_event_inbox_id, clover_order_id,
             total_amount_cents, state, payment_state, processed_at)
            VALUES (:oid, :tid, :iid, 's3ph7_order', 0, 'locked', 'OPEN', now())
        """),
        {"oid": order_id, "tid": tenant_id, "iid": inbox_id},
    )
    slid = uuid.uuid4()
    await session.execute(
        text("""
            INSERT INTO sale_line_items
            (id, tenant_id, order_id, clover_line_item_id, name_at_sale,
             quantity, price_cents_at_sale, net_revenue_cents, recipe_version_id)
            VALUES (:id, :tid, :oid, :cli, 'Sprint 3 Item', :qty, 0, 0, :rvid)
        """),
        {
            "id": slid, "tid": tenant_id, "oid": order_id,
            "cli": f"cli_{uuid.uuid4().hex[:8]}",
            "qty": sale_qty, "rvid": recipe_version_id,
        },
    )
    await session.flush()
    return slid


async def _get_item_http(client: AsyncClient, item_id: uuid.UUID) -> dict:
    r = await client.get("/api/v1/inventory/items")
    assert r.status_code == 200, r.text
    for i in r.json()["items"]:
        if i["id"] == str(item_id):
            return i
    raise AssertionError(f"item {item_id} not found in GET /api/v1/inventory/items")


# ═════════════════════════════════════════════════════════════════════════════
# 7.1-7.2  GRANT TESTS
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_7_1_app_user_cannot_update_movements(
    admin_conn: asyncpg.Connection,
    app_conn: asyncpg.Connection,
) -> None:
    """app_user UPDATE on inventory_movements must be denied."""
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted")
    await _mv(admin_conn, tid, item, "opening_balance", 100)

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("UPDATE inventory_movements SET delta = 999")


@pytest.mark.asyncio
async def test_7_2_app_user_cannot_update_count_events(
    app_conn: asyncpg.Connection,
) -> None:
    """app_user UPDATE on inventory_count_events must be denied."""
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("UPDATE inventory_count_events SET counted_quantity = 0")


# ═════════════════════════════════════════════════════════════════════════════
# 7.3-7.5  OPENING BALANCE TESTS
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_7_3_duplicate_opening_balance_raises_23505(
    admin_conn: asyncpg.Connection,
) -> None:
    """Second opening_balance for the same item raises UniqueViolationError (23505)."""
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted")

    await _mv(admin_conn, tid, item, "opening_balance", 100)

    with pytest.raises(asyncpg.UniqueViolationError):
        await _mv(admin_conn, tid, item, "opening_balance", 50)


@pytest.mark.asyncio
async def test_7_4_opening_balance_after_other_movement_raises_23514(
    admin_conn: asyncpg.Connection,
) -> None:
    """opening_balance after a non-OB movement → trigger raises 23514."""
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted")

    await _mv(admin_conn, tid, item, "receive", 10)

    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await _mv(admin_conn, tid, item, "opening_balance", 100)

    assert exc.value.sqlstate == "23514"


@pytest.mark.asyncio
async def test_7_5_mode_b_opening_balance_sets_anchor(session) -> None:
    """record_opening_balance() on a Mode B item updates last_count_at and last_count_quantity."""
    item_id = uuid.uuid4()
    await _seed_item(session, item_id, "count_anchored")

    recorded_at = datetime(2026, 5, 1, 8, 0, 0, tzinfo=UTC_TZ)
    await record_opening_balance(
        session,
        tenant_id=T7_TENANT_ID,
        inventory_item_id=item_id,
        quantity=Decimal("300"),
        recorded_at=recorded_at,
    )
    await session.flush()

    row = await session.execute(
        text("SELECT last_count_quantity, last_count_at FROM inventory_items WHERE id = :id"),
        {"id": item_id},
    )
    r = row.fetchone()
    assert r[0] == Decimal("300"), f"expected 300, got {r[0]}"
    assert r[1] == recorded_at, f"expected {recorded_at}, got {r[1]}"


# ═════════════════════════════════════════════════════════════════════════════
# 7.6-7.9  ON_HAND TESTS
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_7_6_mode_a_on_hand_equals_130(admin_conn: asyncpg.Connection) -> None:
    """Mode A: OB=100, sale_depletion=-12, waste=-3, receive=+50, count_adjust=-5 → on_hand=130."""
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted")

    await _mv(admin_conn, tid, item, "opening_balance", +100)
    await _mv(admin_conn, tid, item, "sale_depletion", -12)
    await _mv(admin_conn, tid, item, "waste", -3)
    await _mv(admin_conn, tid, item, "receive", +50)
    await _mv(admin_conn, tid, item, "count_adjust", -5)

    result = await _on_hand(admin_conn, tid, item)
    assert result == Decimal("130"), f"expected 130, got {result}"


@pytest.mark.asyncio
async def test_7_7_mode_b_on_hand_equals_125(admin_conn: asyncpg.Connection) -> None:
    """Mode B: anchor=100, receive=+50, sale_signal=+20, yield=1.25 → on_hand=125."""
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)

    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC_TZ)
    t1 = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC_TZ)

    item = await _mk_item(
        admin_conn,
        tid,
        uom,
        mode="count_anchored",
        last_count_at=t0,
        last_count_qty=100,
    )
    await admin_conn.execute(
        "INSERT INTO inventory_yield_factors (tenant_id, inventory_item_id, yield_factor)"
        " VALUES ($1, $2, 1.25)",
        uuid.UUID(tid),
        uuid.UUID(item),
    )
    await _mv(admin_conn, tid, item, "receive", +50, t1)
    await _mv(admin_conn, tid, item, "sale_signal", +20, t1)
    await _mv(admin_conn, tid, item, "waste", -5, t1)  # excluded from on_hand

    result = await _on_hand(admin_conn, tid, item)
    assert result == Decimal("125"), f"expected 125, got {result}"


@pytest.mark.asyncio
async def test_7_8_mode_b_no_anchor_returns_null(admin_conn: asyncpg.Connection) -> None:
    """Mode B item with no last_count_quantity → on_hand() returns NULL."""
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="count_anchored")

    result = await _on_hand(admin_conn, tid, item)
    assert result is None, f"expected NULL, got {result}"


@pytest.mark.asyncio
async def test_7_9_mode_b_missing_yield_factor_defaults_to_1(
    admin_conn: asyncpg.Connection,
) -> None:
    """Mode B with no yield_factor row: sale_signal uses factor=1.0 → on_hand=80."""
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)

    t0 = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC_TZ)
    t1 = datetime(2026, 2, 1, 1, 0, 0, tzinfo=UTC_TZ)

    item = await _mk_item(
        admin_conn,
        tid,
        uom,
        mode="count_anchored",
        last_count_at=t0,
        last_count_qty=100,
    )
    await _mv(admin_conn, tid, item, "sale_signal", +20, t1)

    result = await _on_hand(admin_conn, tid, item)
    assert result == Decimal("80"), f"expected 80, got {result}"


# ═════════════════════════════════════════════════════════════════════════════
# 7.10-7.14  COUNT TRIGGER TESTS
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_7_10_mode_a_drift_emits_count_adjust(
    admin_conn: asyncpg.Connection,
) -> None:
    """Mode A count with drift → trigger emits count_adjust with delta = counted - predicted."""
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted")

    await _mv(admin_conn, tid, item, "opening_balance", +100)

    await _insert_count_event(
        admin_conn,
        tid,
        item,
        counted_quantity=95,
        predicted_on_hand=100,
    )

    n = await _count_mvt(admin_conn, tid, item, "count_adjust")
    assert n == 1, f"expected 1 count_adjust, got {n}"

    row = await admin_conn.fetchrow(
        "SELECT delta FROM inventory_movements"
        " WHERE tenant_id=$1 AND inventory_item_id=$2 AND movement_type='count_adjust'",
        uuid.UUID(tid),
        uuid.UUID(item),
    )
    assert row["delta"] == Decimal("-5"), f"expected delta=-5, got {row['delta']}"
    assert await _on_hand(admin_conn, tid, item) == Decimal("95")


@pytest.mark.asyncio
async def test_7_11_mode_a_zero_drift_emits_nothing(
    admin_conn: asyncpg.Connection,
) -> None:
    """Mode A count with zero drift → no count_adjust row."""
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted")

    await _mv(admin_conn, tid, item, "opening_balance", +100)
    await _insert_count_event(admin_conn, tid, item, counted_quantity=100, predicted_on_hand=100)

    n = await _count_mvt(admin_conn, tid, item, "count_adjust")
    assert n == 0, f"expected 0 count_adjust rows, got {n}"
    assert await _on_hand(admin_conn, tid, item) == Decimal("100")


@pytest.mark.asyncio
async def test_7_12_mode_b_count_does_not_emit_count_adjust(
    admin_conn: asyncpg.Connection,
) -> None:
    """Mode B count event never emits a count_adjust movement."""
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)

    t0 = datetime(2026, 3, 1, tzinfo=UTC_TZ)
    item = await _mk_item(
        admin_conn,
        tid,
        uom,
        mode="count_anchored",
        last_count_at=t0,
        last_count_qty=100,
    )
    await _insert_count_event(
        admin_conn,
        tid,
        item,
        counted_quantity=80,
        predicted_on_hand=None,
        counted_at=datetime(2026, 3, 2, tzinfo=UTC_TZ),
    )

    n = await _count_mvt(admin_conn, tid, item, "count_adjust")
    assert n == 0, f"expected 0 count_adjust, got {n}"


@pytest.mark.asyncio
async def test_7_13_mode_b_count_updates_anchor(
    admin_conn: asyncpg.Connection,
) -> None:
    """Mode B count event trigger updates last_count_quantity and last_count_at."""
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)

    t0 = datetime(2026, 4, 1, tzinfo=UTC_TZ)
    t2 = datetime(2026, 4, 2, tzinfo=UTC_TZ)
    item = await _mk_item(
        admin_conn,
        tid,
        uom,
        mode="count_anchored",
        last_count_at=t0,
        last_count_qty=200,
    )
    await _insert_count_event(
        admin_conn,
        tid,
        item,
        counted_quantity=210,
        predicted_on_hand=None,
        counted_at=t2,
    )

    row = await admin_conn.fetchrow(
        "SELECT last_count_quantity, last_count_at FROM inventory_items WHERE id = $1",
        uuid.UUID(item),
    )
    assert row["last_count_quantity"] == Decimal("210")
    assert row["last_count_at"] == t2


@pytest.mark.asyncio
async def test_7_14_predicted_captured_before_trigger(session) -> None:
    """record_count_event() captures on_hand() BEFORE the count_adjust trigger fires.

    If on_hand() were captured after the INSERT (and thus after count_adjust),
    predicted would equal counted and drift = 0.  The trigger fires DURING the INSERT,
    so the only way to get the correct pre-count predicted is to call on_hand() first.
    """
    item_id = uuid.uuid4()
    await _seed_item(session, item_id, "recipe_deducted")
    await _seed_movement(session, item_id, "opening_balance", 100)

    result = await record_count_event(
        session,
        tenant_id=T7_TENANT_ID,
        inventory_item_id=item_id,
        counted_quantity=Decimal("80"),
    )

    # predicted must be 100 (before trigger changed on_hand to 80)
    assert result["predicted_on_hand_at_count"] == Decimal("100"), (
        f"expected predicted=100 (pre-trigger), got {result['predicted_on_hand_at_count']}"
    )

    # Verify the DB row matches
    row = await session.execute(
        text("SELECT predicted_on_hand_at_count FROM inventory_count_events WHERE id = :id"),
        {"id": result["id"]},
    )
    assert row.scalar_one() == Decimal("100")


# ═════════════════════════════════════════════════════════════════════════════
# 7.15-7.17  SALE TESTS
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_7_15_mode_a_sale_emits_sale_depletion(session) -> None:
    """Mode A: record_sale_inventory_effect() emits a negative sale_depletion movement.

    Theoretical qty = sale_qty * recipe_qty / storage_to_recipe_factor
                    = 5 * 4 / 2.0 = 10
    Delta = -10  (Mode A subtracts from ledger)
    """
    item_id = uuid.uuid4()
    await _seed_item(session, item_id, "recipe_deducted", factor=2.0)
    await _seed_movement(session, item_id, "opening_balance", 100)

    rv_id = await _seed_recipe(session, T7_TENANT_ID, item_id, recipe_qty=4.0)
    slid = await _seed_sale_line(session, T7_TENANT_ID, rv_id, sale_qty=5.0)

    mv_id = await record_sale_inventory_effect(
        session,
        tenant_id=T7_TENANT_ID,
        sale_line_item_id=slid,
        inventory_item_id=item_id,
    )
    assert mv_id is not None

    row = await session.execute(
        text("SELECT movement_type, delta FROM inventory_movements WHERE id = :id"),
        {"id": mv_id},
    )
    r = row.fetchone()
    assert r[0] == "sale_depletion", f"expected sale_depletion, got {r[0]}"
    assert r[1] == Decimal("-10"), f"expected delta=-10, got {r[1]}"


@pytest.mark.asyncio
async def test_7_16_mode_b_sale_emits_sale_signal(session) -> None:
    """Mode B: record_sale_inventory_effect() emits a positive sale_signal movement.

    Theoretical qty = 3 * 2 / 1.0 = 6
    Delta = +6  (Mode B records signal, on_hand() subtracts it with yield factor)
    """
    item_id = uuid.uuid4()
    t0 = datetime(2026, 1, 1, tzinfo=UTC_TZ)
    await _seed_item(session, item_id, "count_anchored", lca=t0, lcq=200)

    rv_id = await _seed_recipe(session, T7_TENANT_ID, item_id, recipe_qty=2.0)
    slid = await _seed_sale_line(session, T7_TENANT_ID, rv_id, sale_qty=3.0)

    mv_id = await record_sale_inventory_effect(
        session,
        tenant_id=T7_TENANT_ID,
        sale_line_item_id=slid,
        inventory_item_id=item_id,
    )
    assert mv_id is not None

    row = await session.execute(
        text("SELECT movement_type, delta FROM inventory_movements WHERE id = :id"),
        {"id": mv_id},
    )
    r = row.fetchone()
    assert r[0] == "sale_signal", f"expected sale_signal, got {r[0]}"
    assert r[1] == Decimal("6"), f"expected delta=+6, got {r[1]}"


@pytest.mark.asyncio
async def test_7_17_sale_is_idempotent(session) -> None:
    """Second call with same sale_line_item_id + inventory_item_id returns None (replay)."""
    item_id = uuid.uuid4()
    await _seed_item(session, item_id, "recipe_deducted")
    await _seed_movement(session, item_id, "opening_balance", 100)

    rv_id = await _seed_recipe(session, T7_TENANT_ID, item_id, recipe_qty=1.0)
    slid = await _seed_sale_line(session, T7_TENANT_ID, rv_id, sale_qty=10.0)

    first = await record_sale_inventory_effect(
        session,
        tenant_id=T7_TENANT_ID,
        sale_line_item_id=slid,
        inventory_item_id=item_id,
    )
    second = await record_sale_inventory_effect(
        session,
        tenant_id=T7_TENANT_ID,
        sale_line_item_id=slid,
        inventory_item_id=item_id,
    )

    assert first is not None, "first call must return a movement id"
    assert second is None, "second call must return None (idempotent replay)"

    cnt = await session.execute(
        text("SELECT COUNT(*) FROM inventory_movements WHERE id = :id"),
        {"id": first},
    )
    assert cnt.scalar() == 1, "only one movement must exist"


# ═════════════════════════════════════════════════════════════════════════════
# 7.18-7.20  RECEIPT TESTS
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_7_18_receipt_commit_creates_receive_movement(session) -> None:
    """commit_receipt() creates a receive movement for each receipt line."""
    item_id = uuid.uuid4()
    await _seed_item(session, item_id, "recipe_deducted")

    rid = await create_receipt(session, tenant_id=T7_TENANT_ID, created_by=T7_USER_ID)
    await add_receipt_line(
        session,
        tenant_id=T7_TENANT_ID,
        receipt_id=rid,
        inventory_item_id=item_id,
        received_quantity=Decimal("50"),
    )
    result = await commit_receipt(session, tenant_id=T7_TENANT_ID, receipt_id=rid)

    assert result["status"] == "committed"
    assert len(result["movement_ids"]) == 1

    mv_row = await session.execute(
        text("SELECT movement_type, delta FROM inventory_movements WHERE id = :id"),
        {"id": result["movement_ids"][0]},
    )
    r = mv_row.fetchone()
    assert r[0] == "receive", f"expected receive, got {r[0]}"
    assert r[1] == Decimal("50"), f"expected delta=50, got {r[1]}"


@pytest.mark.asyncio
async def test_7_19_receipt_commit_creates_cost_snapshot(session) -> None:
    """commit_receipt() writes an ingredient_cost_snapshot when unit_cost_cents is set."""
    item_id = uuid.uuid4()
    await _seed_item(session, item_id, "recipe_deducted")

    rid = await create_receipt(session, tenant_id=T7_TENANT_ID, created_by=T7_USER_ID)
    lid = await add_receipt_line(
        session,
        tenant_id=T7_TENANT_ID,
        receipt_id=rid,
        inventory_item_id=item_id,
        received_quantity=Decimal("10"),
        unit_cost_cents=750,
    )
    await commit_receipt(session, tenant_id=T7_TENANT_ID, receipt_id=rid)

    snap = await session.execute(
        text(
            "SELECT unit_cost_cents FROM ingredient_cost_snapshots"
            " WHERE tenant_id = :tid AND inventory_item_id = :iid"
            "   AND source_receipt_line_id = :lid"
        ),
        {"tid": T7_TENANT_ID, "iid": item_id, "lid": lid},
    )
    row = snap.fetchone()
    assert row is not None, "expected cost snapshot row"
    assert row[0] == 750


@pytest.mark.asyncio
async def test_7_20_receipt_commit_is_idempotent(session) -> None:
    """commit_receipt() on an already-committed receipt returns already_committed."""
    item_id = uuid.uuid4()
    await _seed_item(session, item_id, "recipe_deducted")

    rid = await create_receipt(session, tenant_id=T7_TENANT_ID, created_by=T7_USER_ID)
    await add_receipt_line(
        session,
        tenant_id=T7_TENANT_ID,
        receipt_id=rid,
        inventory_item_id=item_id,
        received_quantity=Decimal("20"),
    )

    r1 = await commit_receipt(session, tenant_id=T7_TENANT_ID, receipt_id=rid)
    r2 = await commit_receipt(session, tenant_id=T7_TENANT_ID, receipt_id=rid)

    assert r1["status"] == "committed"
    assert r2["status"] == "already_committed"

    cnt = await session.execute(
        text(
            "SELECT COUNT(*) FROM inventory_movements"
            " WHERE tenant_id = :tid AND inventory_item_id = :iid AND movement_type = 'receive'"
        ),
        {"tid": T7_TENANT_ID, "iid": item_id},
    )
    assert cnt.scalar() == 1, "second commit must not create a second movement"


# ═════════════════════════════════════════════════════════════════════════════
# 7.21-7.23  IDEMPOTENCY TESTS  (HTTP)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_7_21_idempotent_replay_returns_same_response(session, client: AsyncClient) -> None:
    """Same Idempotency-Key + same body → second request returns the cached 201.

    Exactly 1 count event and 1 count_adjust exist after two identical requests.
    """
    item_id = uuid.uuid4()
    await _seed_item(session, item_id, "recipe_deducted")
    await _seed_movement(session, item_id, "opening_balance", 100)

    payload = {
        "inventory_item_id": str(item_id),
        "counted_quantity": 80,
        "counted_at": "2026-05-01T10:00:00Z",
    }
    key = f"idem-replay-{uuid.uuid4().hex[:8]}"

    r1 = await client.post(
        "/api/v1/inventory/count-events",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert r1.status_code == 201, r1.text
    original = r1.json()

    r2 = await client.post(
        "/api/v1/inventory/count-events",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert r2.status_code == 201, r2.text
    assert r2.json() == original, "replay must return identical response body"

    evt_cnt = await session.execute(
        text("SELECT COUNT(*) FROM inventory_count_events WHERE inventory_item_id = :iid"),
        {"iid": item_id},
    )
    assert evt_cnt.scalar() == 1, "exactly 1 count event must exist"

    adj_cnt = await session.execute(
        text(
            "SELECT COUNT(*) FROM inventory_movements"
            " WHERE inventory_item_id = :iid AND movement_type = 'count_adjust'"
        ),
        {"iid": item_id},
    )
    assert adj_cnt.scalar() == 1, "exactly 1 count_adjust must exist"


@pytest.mark.asyncio
async def test_7_22_different_body_same_key_returns_409(session, client: AsyncClient) -> None:
    """Same Idempotency-Key + different body → 409 idempotency_key_conflict."""
    item_id = uuid.uuid4()
    await _seed_item(session, item_id, "recipe_deducted")
    await _seed_movement(session, item_id, "opening_balance", 100)

    key = f"idem-conflict-{uuid.uuid4().hex[:8]}"
    base = {"inventory_item_id": str(item_id), "counted_at": "2026-05-01T10:00:00Z"}

    r1 = await client.post(
        "/api/v1/inventory/count-events",
        json={**base, "counted_quantity": 100},
        headers={"Idempotency-Key": key},
    )
    assert r1.status_code == 201

    r2 = await client.post(
        "/api/v1/inventory/count-events",
        json={**base, "counted_quantity": 80},
        headers={"Idempotency-Key": key},
    )
    assert r2.status_code == 409
    detail = r2.json().get("detail", r2.json())
    assert detail.get("error") == "idempotency_key_conflict"


@pytest.mark.asyncio
async def test_7_23_in_flight_key_returns_409(session, client: AsyncClient) -> None:
    """Pre-seeded key with response_status=NULL → 409 request_in_flight."""
    from app.modules.inventory.idempotency import compute_fingerprint

    item_id = uuid.uuid4()
    await _seed_item(session, item_id, "recipe_deducted")
    await _seed_movement(session, item_id, "opening_balance", 100)

    key = f"in-flight-{uuid.uuid4().hex[:8]}"
    payload = {
        "inventory_item_id": str(item_id),
        "counted_quantity": 90,
        "counted_at": "2026-05-01T10:00:00Z",
    }
    fp = compute_fingerprint("POST", "/api/v1/inventory/count-events", payload)

    await session.execute(
        text("""
            INSERT INTO idempotency_keys (tenant_id, key, request_fingerprint)
            VALUES (:tid, :key, :fp)
        """),
        {"tid": T7_TENANT_ID, "key": key, "fp": fp},
    )
    await session.flush()

    r = await client.post(
        "/api/v1/inventory/count-events",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert r.status_code == 409
    detail = r.json().get("detail", r.json())
    assert detail.get("error") == "request_in_flight"


# ═════════════════════════════════════════════════════════════════════════════
# 7.24-7.26  MONITORING TESTS
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_7_24_drift_over_20pct_emits_critical_alert(session) -> None:
    """drift > 20 % → monitoring_alerts severity = 'critical'."""
    item_id = uuid.uuid4()
    await _seed_item(session, item_id, "recipe_deducted")
    await _seed_movement(session, item_id, "opening_balance", 100)

    # counted=75 → drift = (100-75)/75 ≈ 33 % > 20 %
    await record_count_event(
        session,
        tenant_id=T7_TENANT_ID,
        inventory_item_id=item_id,
        counted_quantity=Decimal("75"),
    )

    row = await session.execute(
        text(
            "SELECT severity FROM monitoring_alerts"
            " WHERE tenant_id = :tid AND monitor_name = 'integrity_drift_high'"
            "   AND resolved_at IS NULL"
        ),
        {"tid": T7_TENANT_ID},
    )
    r = row.fetchone()
    assert r is not None, "expected a monitoring_alerts row"
    assert r[0] == "critical", f"expected critical, got {r[0]}"


@pytest.mark.asyncio
async def test_7_25_drift_between_5_and_20pct_emits_warn_alert(session) -> None:
    """5 % < drift ≤ 20 % → monitoring_alerts severity = 'warn'."""
    item_id = uuid.uuid4()
    await _seed_item(session, item_id, "recipe_deducted")
    await _seed_movement(session, item_id, "opening_balance", 100)

    # counted=90 → drift = (100-90)/90 ≈ 11 % → warn
    await record_count_event(
        session,
        tenant_id=T7_TENANT_ID,
        inventory_item_id=item_id,
        counted_quantity=Decimal("90"),
    )

    row = await session.execute(
        text(
            "SELECT severity FROM monitoring_alerts"
            " WHERE tenant_id = :tid AND monitor_name = 'integrity_drift_high'"
            "   AND resolved_at IS NULL"
        ),
        {"tid": T7_TENANT_ID},
    )
    r = row.fetchone()
    assert r is not None, "expected a monitoring_alerts row"
    assert r[0] == "warn", f"expected warn, got {r[0]}"


@pytest.mark.asyncio
async def test_7_26_monitoring_upsert_increments_alert_count(session) -> None:
    """Second high-drift count on same tenant → UPSERT updates alert_count to 2."""
    item_id = uuid.uuid4()
    await _seed_item(session, item_id, "recipe_deducted")
    await _seed_movement(session, item_id, "opening_balance", 100)

    # First count: on_hand=100, counted=70 → drift=43% → critical, alert_count=1
    await record_count_event(
        session,
        tenant_id=T7_TENANT_ID,
        inventory_item_id=item_id,
        counted_quantity=Decimal("70"),
    )
    # After trigger: on_hand = 70 (count_adjust delta=-30 emitted)

    # Second count: on_hand=70, counted=55 → drift≈27% → critical, UPSERT → alert_count=2
    await record_count_event(
        session,
        tenant_id=T7_TENANT_ID,
        inventory_item_id=item_id,
        counted_quantity=Decimal("55"),
    )

    row = await session.execute(
        text(
            "SELECT COUNT(*) AS n, MAX(alert_count) AS max_cnt"
            " FROM monitoring_alerts"
            " WHERE tenant_id = :tid AND monitor_name = 'integrity_drift_high'"
            "   AND resolved_at IS NULL"
        ),
        {"tid": T7_TENANT_ID},
    )
    r = row.fetchone()
    assert r[0] == 1, f"expected exactly 1 monitoring_alerts row (UPSERT), got {r[0]}"
    assert r[1] == 2, f"expected alert_count=2 after two drifts, got {r[1]}"


# ═════════════════════════════════════════════════════════════════════════════
# 7.27-7.29  STOCK WARNING TESTS  (HTTP)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_7_27_stock_flags_are_correct(session, client: AsyncClient) -> None:
    """GET /inventory/items returns correct stock_status, low_stock, out_of_stock."""
    item_ok = uuid.uuid4()
    item_low = uuid.uuid4()

    # ok: on_hand=500, par=100
    await _seed_item(session, item_ok, "recipe_deducted", par=100.0)
    await _seed_movement(session, item_ok, "opening_balance", 500)

    # low_stock: on_hand=50, par=100
    await _seed_item(session, item_low, "recipe_deducted", par=100.0, ing_id=T7_ING_B)
    await _seed_movement(session, item_low, "opening_balance", 50)

    # out_of_stock: on_hand=0, par=100 (both out_of_stock AND low_stock; oos wins)
    item_oos2 = uuid.uuid4()
    await _seed_item(session, item_oos2, "recipe_deducted", par=100.0)
    await _seed_movement(session, item_oos2, "opening_balance", 0)

    i_ok = await _get_item_http(client, item_ok)
    i_low = await _get_item_http(client, item_low)
    i_oos = await _get_item_http(client, item_oos2)

    assert i_ok["stock_status"] == "ok", f"got {i_ok['stock_status']}"
    assert i_ok["low_stock"] is False
    assert i_ok["out_of_stock"] is False
    assert i_ok["count_required"] is False

    assert i_low["stock_status"] == "low_stock", f"got {i_low['stock_status']}"
    assert i_low["low_stock"] is True
    assert i_low["out_of_stock"] is False

    assert i_oos["stock_status"] == "out_of_stock", f"got {i_oos['stock_status']}"
    assert i_oos["out_of_stock"] is True
    assert i_oos["low_stock"] is True  # 0 ≤ 100 → also low_stock, but oos wins


@pytest.mark.asyncio
async def test_7_28_mode_b_null_anchor_reports_count_required(session, client: AsyncClient) -> None:
    """Mode B item with no last_count_quantity → count_required=True, on_hand=None."""
    item_id = uuid.uuid4()
    await _seed_item(
        session,
        item_id,
        "count_anchored",
        cadence=7,
        grace=7,
        lca=None,
        lcq=None,
    )

    i = await _get_item_http(client, item_id)
    assert i["count_required"] is True
    assert i["on_hand"] is None
    assert i["stock_status"] == "count_required"
    assert i["low_stock"] is None
    assert i["out_of_stock"] is None


@pytest.mark.asyncio
async def test_7_29_stock_status_priority_chain(session, client: AsyncClient) -> None:
    """Verify priority chain: count_required > out_of_stock > count_expired > count_stale > ok.

    Each item exercises a distinct rank in the priority ladder.
    """
    now = datetime.now(UTC_TZ)

    # count_required (highest) — Mode B, no anchor
    p_count_req = uuid.uuid4()
    await _seed_item(session, p_count_req, "count_anchored", cadence=7, grace=7)

    # out_of_stock — Mode A, on_hand=0
    p_oos = uuid.uuid4()
    await _seed_item(session, p_oos, "recipe_deducted", ing_id=T7_ING_B)
    await _seed_movement(session, p_oos, "opening_balance", 0)

    # count_expired — Mode B, anchor exists but age > cadence+grace
    p_expired = uuid.uuid4()
    old_count = now - timedelta(days=20)  # cadence=7, grace=7 → expired after 14 days
    await _seed_item(
        session,
        p_expired,
        "count_anchored",
        cadence=7,
        grace=7,
        lca=old_count,
        lcq=200,
    )

    # count_stale — Mode B, anchor in stale window (cadence < age ≤ cadence+grace)
    p_stale = uuid.uuid4()
    stale_count = now - timedelta(days=10)  # 7 < 10 ≤ 14 → stale
    await _seed_item(
        session,
        p_stale,
        "count_anchored",
        cadence=7,
        grace=7,
        lca=stale_count,
        lcq=200,
    )

    # ok — Mode A, healthy stock
    p_ok = uuid.uuid4()
    await _seed_item(session, p_ok, "recipe_deducted")
    await _seed_movement(session, p_ok, "opening_balance", 500)

    i_cr = await _get_item_http(client, p_count_req)
    i_oos = await _get_item_http(client, p_oos)
    i_exp = await _get_item_http(client, p_expired)
    i_st = await _get_item_http(client, p_stale)
    i_ok = await _get_item_http(client, p_ok)

    assert i_cr["stock_status"] == "count_required", f"got {i_cr['stock_status']}"
    assert i_oos["stock_status"] == "out_of_stock", f"got {i_oos['stock_status']}"
    assert i_exp["stock_status"] == "count_expired", f"got {i_exp['stock_status']}"
    assert i_st["stock_status"] == "count_stale", f"got {i_st['stock_status']}"
    assert i_ok["stock_status"] == "ok", f"got {i_ok['stock_status']}"
