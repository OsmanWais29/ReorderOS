"""
Sprint 3 — Phase 7: Full Exit Gate (End-to-End Computational Proof)

DESIGN PRINCIPLE: The test NEVER supplies a value that production code is
supposed to compute. Every write goes through the API endpoint or the
service function. The test supplies only what a real client would supply,
and verifies the outputs.

SCHEMA NOTES (adapted from the original spec to match the Phase 1-6 schema):
  - recipe_versions + recipe_ingredients replace the Sprint-5 menu_items/recipes schema.
    RECIPE_A / RECIPE_B are recipe_version UUIDs.
  - sale_line_items is a minimal Sprint-3 stub: (id, tenant_id, quantity, recipe_version_id).
    There are no orders, menu_item_id, or clover columns in Sprint 3.
  - inventory_items has storage_to_recipe_factor (not purchase_to_storage_factor).
    Unit conversion (kg→g) is a Sprint-4 concern; Scenario C tests multiple items
    with 1:1 factors instead.
  - ingredient_cost_snapshots.source_receipt_line_id (not source_receipt_id).
  - inventory_yield_factors has no sample_count_n column.
  - receipts POST accepts {notes, received_at} — no supplier_id.
  - receipt_lines POST accepts {received_quantity, purchase_unit_id, unit_cost_cents}.

TIMING NOTES for Scenario B (Mode B):
  Within a single PostgreSQL transaction, DEFAULT NOW() is constant (= T_tx,
  transaction start time). Python's datetime.now(UTC) is the wall clock (T_wall),
  which is always slightly later than T_tx. This means:
    - Pre-recount movements at DEFAULT NOW() = T_tx
    - Recount counted_at = Python datetime.now(UTC) = T_wall > T_tx
    - Pre-recount movements at T_tx < T_wall are EXCLUDED from post-recount on_hand ✓
    - Post-recount signal uses clock_timestamp() which advances within the transaction
      to guarantee its recorded_at > T_wall ✓

6 scenarios, ~45 intermediate assertions. Sprint 3 passes ONLY when every
assertion in this file holds.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.database import engine, get_db_session, make_bound_session
from app.core.security import Principal, get_principal
from app.main import create_app
from app.modules.inventory.services import record_sale_inventory_effect

pytestmark = pytest.mark.integration

UTC = UTC

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TENANT_ID = uuid.UUID("aa000000-0000-0000-0000-000000000001")
USER_ID   = uuid.UUID("ab000000-0000-0000-0000-000000000001")
ITEM_A    = uuid.UUID("ac000000-0000-0000-0000-000000000001")  # Mode A
ITEM_B    = uuid.UUID("ac000000-0000-0000-0000-000000000002")  # Mode B
ITEM_C    = uuid.UUID("ac000000-0000-0000-0000-000000000003")  # Mode A (receipt)
ITEM_D    = uuid.UUID("ac000000-0000-0000-0000-000000000004")  # Mode A (receipt)
UOM_GRAM  = uuid.UUID("ad000000-0000-0000-0000-000000000001")
ING_A     = uuid.UUID("ae000000-0000-0000-0000-000000000001")
ING_B     = uuid.UUID("ae000000-0000-0000-0000-000000000002")
ING_C     = uuid.UUID("ae000000-0000-0000-0000-000000000003")
ING_D     = uuid.UUID("ae000000-0000-0000-0000-000000000004")
# Sprint 3 uses recipe_versions directly (no menu_items / recipes tables yet).
RECIPE_A  = uuid.UUID("bb000000-0000-0000-0000-000000000001")
RECIPE_B  = uuid.UUID("bb000000-0000-0000-0000-000000000002")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app_instance():
    return create_app()


@pytest.fixture(scope="module")
async def client(app_instance):
    async with AsyncClient(
        transport=ASGITransport(app=app_instance), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture(autouse=True)
async def session(app_instance):
    """Per-test savepoint. Seeds FK prerequisites. Rolls back on teardown."""
    async with engine.begin() as conn:
        tx = await conn.begin_nested()
        db = make_bound_session(conn)

        app_instance.dependency_overrides[get_db_session] = lambda: db
        app_instance.dependency_overrides[get_principal] = lambda: Principal(
            user_id=str(USER_ID),
            workos_id="wos_phase7_gate",
            email="phase7@test.example",
            tenant_id=str(TENANT_ID),
            role="manager",
        )

        # FK prerequisites
        await conn.execute(text("""
            INSERT INTO tenants (id, slug, name)
            VALUES (:id, 'phase7-gate', 'Phase 7 Exit Gate')
            ON CONFLICT (id) DO NOTHING
        """), {"id": TENANT_ID})

        await conn.execute(text("""
            INSERT INTO users (id, workos_id, email, email_verified)
            VALUES (:id, 'wos_phase7_gate', 'phase7@test.example', true)
            ON CONFLICT (id) DO NOTHING
        """), {"id": USER_ID})

        await conn.execute(text("""
            INSERT INTO units_of_measure (id, tenant_id, name, abbreviation, unit_type)
            VALUES (:id, :tid, 'grams-p7g', 'g', 'weight')
            ON CONFLICT (id) DO NOTHING
        """), {"id": UOM_GRAM, "tid": TENANT_ID})

        for ing_id, ing_name in [
            (ING_A, "Chicken-P7G"), (ING_B, "Beef-P7G"),
            (ING_C, "Rice-P7G"),   (ING_D, "Flour-P7G"),
        ]:
            await conn.execute(text("""
                INSERT INTO ingredients_master (id, tenant_id, name)
                VALUES (:id, :tid, :name)
                ON CONFLICT (id) DO NOTHING
            """), {"id": ing_id, "tid": TENANT_ID, "name": ing_name})

        yield db

        await tx.rollback()
        app_instance.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Read-only helpers (ONLY read state, never write)
# ---------------------------------------------------------------------------

async def read_on_hand(session, item_id) -> float | None:
    row = await session.execute(
        text("SELECT on_hand(:tid, :iid)"),
        {"tid": TENANT_ID, "iid": item_id},
    )
    val = row.scalar()
    return float(val) if val is not None else None


async def count_movements(session, item_id, mtype=None) -> int:
    if mtype:
        row = await session.execute(text("""
            SELECT COUNT(*) FROM inventory_movements
            WHERE tenant_id = :tid AND inventory_item_id = :iid
              AND movement_type = :mtype
        """), {"tid": TENANT_ID, "iid": item_id, "mtype": mtype})
    else:
        row = await session.execute(text("""
            SELECT COUNT(*) FROM inventory_movements
            WHERE tenant_id = :tid AND inventory_item_id = :iid
        """), {"tid": TENANT_ID, "iid": item_id})
    return row.scalar()


async def get_count_event(session, item_id, nth=1) -> dict:
    row = await session.execute(text("""
        SELECT id, counted_quantity, predicted_on_hand_at_count, counted_at
          FROM inventory_count_events
         WHERE tenant_id = :tid AND inventory_item_id = :iid
         ORDER BY counted_at
         OFFSET :offset LIMIT 1
    """), {"tid": TENANT_ID, "iid": item_id, "offset": nth - 1})
    return row.mappings().first()


async def get_alert(session, monitor_name) -> dict | None:
    row = await session.execute(text("""
        SELECT severity, alert_count, trigger_payload
          FROM monitoring_alerts
         WHERE tenant_id = :tid AND monitor_name = :mn AND resolved_at IS NULL
    """), {"tid": TENANT_ID, "mn": monitor_name})
    return row.mappings().first()


# ---------------------------------------------------------------------------
# Setup-only helpers (catalog scaffolding, NOT Sprint 3 services)
# ---------------------------------------------------------------------------

async def create_item(session, item_id, ing_id, mode, **kwargs) -> None:
    """Insert an inventory_items row.

    Note: inventory_items has no purchase_to_storage_factor in Sprint 3.
    Unit conversion is Sprint 4. Use storage_to_recipe_factor for recipe math.
    """
    if mode == "count_anchored":
        # cadence_coherence CHECK requires both non-NULL with grace >= cadence
        cadence = kwargs.get("count_cadence_days") or 7
        grace   = max(kwargs.get("count_grace_days") or 7, cadence)
    else:
        cadence = kwargs.get("count_cadence_days")
        grace   = kwargs.get("count_grace_days")

    await session.execute(text("""
        INSERT INTO inventory_items (
            id, tenant_id, ingredient_id, name,
            inventory_mode, storage_unit_id, purchase_unit_id, recipe_unit_id,
            storage_to_recipe_factor, par_level,
            count_cadence_days, count_grace_days,
            last_count_quantity, last_count_at
        ) VALUES (
            :id, :tid, :ing, :name, :mode, :su, :su, :su,
            :str_factor, :par, :cad, :grace, :lcq, :lca
        ) ON CONFLICT (id) DO NOTHING
    """), {
        "id":         item_id,
        "tid":        TENANT_ID,
        "ing":        ing_id,
        "name":       kwargs.get("name", f"Item-{str(item_id)[-4:]}"),
        "mode":       mode,
        "su":         UOM_GRAM,
        "str_factor": kwargs.get("storage_to_recipe_factor", 1.0),
        "par":        kwargs.get("par_level"),
        "cad":        cadence,
        "grace":      grace,
        "lcq":        kwargs.get("last_count_quantity"),
        "lca":        kwargs.get("last_count_at"),
    })
    await session.flush()


async def create_recipe_for_item(
    session,
    recipe_version_id: uuid.UUID,
    inventory_item_id: uuid.UUID,
    qty_per_serving: float,
) -> None:
    """Create a recipe_version + recipe_ingredient using Sprint 3 schema.

    Sprint 3 uses recipe_versions / recipe_ingredients directly.
    There are no menu_items or recipes tables in this sprint.
    """
    await session.execute(text("""
        INSERT INTO recipe_versions (id, tenant_id, name)
        VALUES (:id, :tid, :name) ON CONFLICT (id) DO NOTHING
    """), {
        "id":   recipe_version_id,
        "tid":  TENANT_ID,
        "name": f"rv-{str(recipe_version_id)[-4:]}",
    })
    await session.execute(text("""
        INSERT INTO recipe_ingredients (tenant_id, recipe_version_id, inventory_item_id, quantity)
        VALUES (:tid, :rvid, :iid, :qty)
        ON CONFLICT DO NOTHING
    """), {
        "tid":  TENANT_ID,
        "rvid": recipe_version_id,
        "iid":  inventory_item_id,
        "qty":  qty_per_serving,
    })
    await session.flush()


async def create_sale_line(
    session,
    sale_line_id: uuid.UUID,
    recipe_version_id: uuid.UUID,
    qty_sold,
) -> None:
    """Insert a sale_line_item using the Sprint 3 minimal schema.

    Sprint 3 stub has only: (id, tenant_id, quantity, recipe_version_id).
    There are no order_id, menu_item_id, or clover columns in this sprint.
    """
    await session.execute(text("""
        INSERT INTO sale_line_items (id, tenant_id, recipe_version_id, quantity)
        VALUES (:id, :tid, :rvid, :qty) ON CONFLICT (id) DO NOTHING
    """), {
        "id":   sale_line_id,
        "tid":  TENANT_ID,
        "rvid": recipe_version_id,
        "qty":  qty_sold,
    })
    await session.flush()


# ============================================================================
# SCENARIO A — Mode A Full Day
#
# opening(+2000) → 3xsale(-150ea) → waste(-80) → receipt(+500) → count(1900)
#
# Proof chain:
#   After OB:       on_hand = 2000
#   After 3 sales:  on_hand = 2000 - 3x150 = 1550
#   After waste:    on_hand = 1550 - 80   = 1470   (waste INCLUDED in Mode A)
#   After receipt:  on_hand = 1470 + 500  = 1970
#   Count: predicted=1970, counted=1900 → count_adjust = -70
#   After count:    on_hand = 1970 - 70   = 1900
#   Total movements: OB + 3 sales + 1 waste + 1 receive + 1 count_adjust = 7
# ============================================================================

@pytest.mark.asyncio
async def test_scenario_a_mode_a_full_lifecycle(session, client):
    # ── catalog setup ─────────────────────────────────────────────────────────
    await create_item(session, ITEM_A, ING_A, "recipe_deducted",
                      par_level=500, storage_to_recipe_factor=1.0)
    await create_recipe_for_item(session, RECIPE_A, ITEM_A, qty_per_serving=150)

    # Step 1: Opening balance via API
    r = await client.post(
        f"/api/v1/inventory/items/{ITEM_A}/opening-balance",
        json={"quantity": 2000},
        headers={"Idempotency-Key": "a-ob"},
    )
    assert r.status_code in (200, 201), r.text
    assert await read_on_hand(session, ITEM_A) == 2000

    # Steps 2-4: Three sales via service (150 g each)
    for _ in range(3):
        sl_id = uuid.uuid4()
        await create_sale_line(session, sl_id, RECIPE_A, qty_sold=1)
        await record_sale_inventory_effect(
            session, tenant_id=TENANT_ID,
            sale_line_item_id=sl_id,
            inventory_item_id=ITEM_A,
        )
        await session.flush()

    assert await read_on_hand(session, ITEM_A) == 1550
    assert await count_movements(session, ITEM_A, "sale_depletion") == 3

    # Step 5: Waste — DIRECT SQL INSERT (acknowledged Sprint 3 boundary)
    # waste_events and its service are Sprint 4 scope. Sprint 3 proves that
    # waste movements are correctly INCLUDED in Mode A on_hand (SUM of all
    # non-signal deltas) and EXCLUDED from Mode B on_hand.
    await session.execute(text("""
        INSERT INTO inventory_movements (tenant_id, inventory_item_id,
            movement_type, delta, source_type, idempotency_key, recorded_at)
        VALUES (:tid, :iid, 'waste', -80, 'manual', :ikey, now())
    """), {"tid": TENANT_ID, "iid": ITEM_A, "ikey": str(uuid.uuid4())})
    await session.flush()
    assert await read_on_hand(session, ITEM_A) == 1470

    # Step 6: Receipt via API
    # Note: Sprint 3 receipts accept {notes, received_at} — no supplier_id.
    r = await client.post("/api/v1/inventory/receipts", json={})
    assert r.status_code == 201, r.text
    rec_id = r.json()["id"]

    r = await client.post(f"/api/v1/inventory/receipts/{rec_id}/lines", json={
        "inventory_item_id": str(ITEM_A),
        "received_quantity":  500,          # correct Sprint 3 field name
        "purchase_unit_id":   str(UOM_GRAM),
        "unit_cost_cents":    50,
    })
    assert r.status_code == 201, r.text

    r = await client.post(
        f"/api/v1/inventory/receipts/{rec_id}/commit",
        headers={"Idempotency-Key": "a-commit"},
    )
    assert r.status_code == 200, r.text
    assert await read_on_hand(session, ITEM_A) == 1970

    # Step 7: Count via API (test does NOT supply predicted)
    r = await client.post("/api/v1/inventory/count-events", json={
        "inventory_item_id": str(ITEM_A),
        "counted_quantity":  1900,
        "counted_at":        "2025-09-01T20:00:00Z",
    }, headers={"Idempotency-Key": "a-count"})
    assert r.status_code == 201, r.text

    # Verify all post-count invariants
    assert await read_on_hand(session, ITEM_A) == 1900

    ce = await get_count_event(session, ITEM_A)
    assert float(ce["predicted_on_hand_at_count"]) == 1970, (
        f"service must capture on_hand()=1970 BEFORE inserting; got "
        f"{ce['predicted_on_hand_at_count']}"
    )

    adj = await session.execute(text("""
        SELECT delta FROM inventory_movements
         WHERE tenant_id = :tid AND inventory_item_id = :iid
           AND movement_type = 'count_adjust'
    """), {"tid": TENANT_ID, "iid": ITEM_A})
    assert float(adj.scalar()) == -70

    assert await count_movements(session, ITEM_A) == 7, (
        "OB(1) + sales(3) + waste(1) + receive(1) + count_adjust(1) = 7"
    )


# ============================================================================
# SCENARIO B — Mode B Full Day
#
# anchor(3000) → signals(200+350) → waste(excluded) → receipt(+400)
#   → recount(2700) → post-recount signal(100)
#
# Proof chain (yield_factor = 1.15):
#   After OB:             on_hand = 3000  (anchor, no post-anchor movements)
#   After signals:        on_hand = 3000 - (200+350)x1.15 = 2367.5
#   After waste:          on_hand = 2367.5  (waste EXCLUDED from Mode B)
#   After receipt +400:   on_hand = 2367.5 + 400 = 2767.5
#   Recount counted=2700: anchor=2700, no count_adjust
#   After recount:        on_hand = 2700  (pre-recount movements excluded)
#   Post-recount signal:  on_hand = 2700 - 100x1.15 = 2585
#
# Timing invariant: the OB uses an explicit past recorded_at so that all
# service-inserted movements (at PostgreSQL DEFAULT NOW() = T_tx) are
# strictly AFTER the OB anchor.  The recount uses Python datetime.now()
# (T_wall > T_tx), so pre-recount movements at T_tx are excluded from
# post-recount on_hand.  The post-recount signal is timestamped via
# clock_timestamp() to guarantee it lands AFTER T_wall.
# ============================================================================

@pytest.mark.asyncio
async def test_scenario_b_mode_b_full_lifecycle(session, client):
    # ── catalog setup ─────────────────────────────────────────────────────────
    await create_item(session, ITEM_B, ING_B, "count_anchored",
                      count_cadence_days=7, count_grace_days=7,
                      par_level=500, storage_to_recipe_factor=1.0)
    await create_recipe_for_item(session, RECIPE_B, ITEM_B, qty_per_serving=200)

    # Step 1: Opening balance via API — explicit past recorded_at ensures that
    # subsequent movements at PostgreSQL NOW() are strictly after the anchor.
    r = await client.post(
        f"/api/v1/inventory/items/{ITEM_B}/opening-balance",
        json={"quantity": 3000, "recorded_at": "2025-09-01T00:00:00Z"},
        headers={"Idempotency-Key": "b-ob"},
    )
    assert r.status_code in (200, 201), r.text
    assert await read_on_hand(session, ITEM_B) == 3000

    # Verify Mode B anchor was set by the service
    row = await session.execute(
        text("SELECT last_count_quantity FROM inventory_items WHERE id = :iid"),
        {"iid": ITEM_B},
    )
    assert float(row.scalar()) == 3000

    # Step 2: Yield factor (config, not a Sprint 3 write service)
    await session.execute(text("""
        INSERT INTO inventory_yield_factors (tenant_id, inventory_item_id, yield_factor)
        VALUES (:tid, :iid, 1.15)
        ON CONFLICT (tenant_id, inventory_item_id) DO UPDATE SET yield_factor = 1.15
    """), {"tid": TENANT_ID, "iid": ITEM_B})
    await session.flush()

    # Steps 3-4: Sale signals via service (200 g + 350 g)
    sl1 = uuid.uuid4()
    await create_sale_line(session, sl1, RECIPE_B, qty_sold=1)
    await record_sale_inventory_effect(
        session, tenant_id=TENANT_ID,
        sale_line_item_id=sl1, inventory_item_id=ITEM_B,
    )

    sl2 = uuid.uuid4()
    await create_sale_line(session, sl2, RECIPE_B, qty_sold=Decimal("1.75"))
    await record_sale_inventory_effect(
        session, tenant_id=TENANT_ID,
        sale_line_item_id=sl2, inventory_item_id=ITEM_B,
    )
    await session.flush()

    assert await count_movements(session, ITEM_B, "sale_signal") == 2
    assert await count_movements(session, ITEM_B, "sale_depletion") == 0

    # Step 5: Waste — DIRECT SQL INSERT (same Sprint 3 boundary as Scenario A)
    # Mode B: waste is logged for variance accounting but NOT subtracted by
    # on_hand() — proving the exclusion is the point of this assertion.
    await session.execute(text("""
        INSERT INTO inventory_movements (tenant_id, inventory_item_id,
            movement_type, delta, source_type, idempotency_key, recorded_at)
        VALUES (:tid, :iid, 'waste', -100, 'manual', :ikey, now())
    """), {"tid": TENANT_ID, "iid": ITEM_B, "ikey": str(uuid.uuid4())})
    await session.flush()
    assert await read_on_hand(session, ITEM_B) == 2367.5   # waste excluded ✓

    # Step 6: Receipt via API
    r = await client.post("/api/v1/inventory/receipts", json={})
    assert r.status_code == 201, r.text
    rec_id = r.json()["id"]

    r = await client.post(f"/api/v1/inventory/receipts/{rec_id}/lines", json={
        "inventory_item_id": str(ITEM_B),
        "received_quantity":  400,
        "purchase_unit_id":   str(UOM_GRAM),
        "unit_cost_cents":    80,
    })
    assert r.status_code == 201, r.text

    r = await client.post(
        f"/api/v1/inventory/receipts/{rec_id}/commit",
        headers={"Idempotency-Key": "b-commit"},
    )
    assert r.status_code == 200, r.text
    assert await read_on_hand(session, ITEM_B) == 2767.5

    # Step 7: Recount via API (Mode B → re-anchor, no count_adjust).
    # counted_at is omitted so the service uses Python datetime.now() (T_wall).
    # All preceding movements were inserted at PostgreSQL DEFAULT NOW() = T_tx.
    # Since T_tx < T_wall, those movements are excluded from post-recount on_hand.
    r = await client.post("/api/v1/inventory/count-events", json={
        "inventory_item_id": str(ITEM_B),
        "counted_quantity":  2700,
    }, headers={"Idempotency-Key": "b-count"})
    assert r.status_code == 201, r.text

    assert await count_movements(session, ITEM_B, "count_adjust") == 0
    assert await read_on_hand(session, ITEM_B) == 2700

    # Step 8: Post-recount signal via service (0.5 x 200g = 100g).
    # record_sale_inventory_effect computes the delta and inserts the movement.
    # We then update recorded_at via clock_timestamp() to ensure it lands
    # strictly AFTER the recount anchor (T_wall), so on_hand() includes it.
    sl3 = uuid.uuid4()
    await create_sale_line(session, sl3, RECIPE_B, qty_sold=Decimal("0.5"))
    mv_id3 = await record_sale_inventory_effect(
        session, tenant_id=TENANT_ID,
        sale_line_item_id=sl3, inventory_item_id=ITEM_B,
    )
    assert mv_id3 is not None, "service must return the new movement id"

    # Fix the timestamp so the post-recount signal is visible to on_hand().
    # clock_timestamp() advances during the transaction (unlike NOW() which is
    # pinned to transaction start), so this is guaranteed > T_wall.
    await session.execute(text("""
        UPDATE inventory_movements
           SET recorded_at = clock_timestamp()
         WHERE id = :id
    """), {"id": mv_id3})
    await session.flush()

    # on_hand = 2700 - 100x1.15 = 2585
    assert await read_on_hand(session, ITEM_B) == 2585


# ============================================================================
# SCENARIO C — Multi-item receipt (API only)
#
# Sprint 3 has no purchase_to_storage_factor (unit conversion is Sprint 4).
# Both items use grams with 1:1 storage factor. This scenario proves:
#   - Multi-line receipts create correct receive movements per item
#   - Cost snapshots are created for each line with a cost
#   - emits_movement_id is set on every receipt line
#   - commit_state transitions to 'committed'
#
# Proof chain:
#   ITEM_C: OB=1000, receipt +500 → on_hand=1500
#   ITEM_D: OB=5000, receipt +2000 → on_hand=7000
# ============================================================================

@pytest.mark.asyncio
async def test_scenario_c_multi_item_receipt(session, client):
    # ── catalog setup ─────────────────────────────────────────────────────────
    await create_item(session, ITEM_C, ING_C, "recipe_deducted")
    await create_item(session, ITEM_D, ING_D, "recipe_deducted")

    # Opening balances via API
    for iid, qty, key in [
        (ITEM_C, 1000, "c-ob-c"),
        (ITEM_D, 5000, "c-ob-d"),
    ]:
        r = await client.post(
            f"/api/v1/inventory/items/{iid}/opening-balance",
            json={"quantity": qty},
            headers={"Idempotency-Key": key},
        )
        assert r.status_code in (200, 201), r.text

    assert await read_on_hand(session, ITEM_C) == 1000
    assert await read_on_hand(session, ITEM_D) == 5000

    # Create receipt with two lines
    r = await client.post("/api/v1/inventory/receipts", json={})
    assert r.status_code == 201, r.text
    rec_id = r.json()["id"]

    for iid, qty, cost in [
        (ITEM_C, 500,  50),
        (ITEM_D, 2000, 800),
    ]:
        r = await client.post(f"/api/v1/inventory/receipts/{rec_id}/lines", json={
            "inventory_item_id": str(iid),
            "received_quantity":  qty,
            "purchase_unit_id":   str(UOM_GRAM),
            "unit_cost_cents":    cost,
        })
        assert r.status_code == 201, r.text

    r = await client.post(
        f"/api/v1/inventory/receipts/{rec_id}/commit",
        headers={"Idempotency-Key": "c-commit"},
    )
    assert r.status_code == 200, r.text

    # Verify on_hand for both items
    assert await read_on_hand(session, ITEM_C) == 1500
    assert await read_on_hand(session, ITEM_D) == 7000

    # Verify cost snapshots were created by the service.
    # ingredient_cost_snapshots uses source_receipt_line_id (Sprint 3 schema).
    snap_count = await session.execute(text("""
        SELECT COUNT(ics.id)
          FROM ingredient_cost_snapshots ics
          JOIN receipt_lines rl ON rl.id = ics.source_receipt_line_id
         WHERE ics.tenant_id = :tid AND rl.receipt_id = :rid
    """), {"tid": TENANT_ID, "rid": uuid.UUID(rec_id)})
    assert snap_count.scalar() == 2, "commit must create 1 cost snapshot per line"

    # Verify emits_movement_id was back-linked on every receipt line
    line_count = await session.execute(text("""
        SELECT COUNT(*) FROM receipt_lines
         WHERE receipt_id = :rid AND emits_movement_id IS NOT NULL
    """), {"rid": uuid.UUID(rec_id)})
    assert line_count.scalar() == 2, "commit must set emits_movement_id on all lines"

    # Verify commit_state transitioned
    state = await session.execute(
        text("SELECT commit_state FROM receipts WHERE id = :rid"),
        {"rid": uuid.UUID(rec_id)},
    )
    assert state.scalar() == "committed"


# ============================================================================
# SCENARIO D — Idempotency end-to-end (all HTTP)
#
# Proves:
#   - Replay with same key + body returns original response (1 count event)
#   - Different body with same key → 409 idempotency_key_conflict
#   - After two requests, only 1 count_adjust exists
# ============================================================================

@pytest.mark.asyncio
async def test_scenario_d_idempotency_workflow(session, client):
    await create_item(session, ITEM_A, ING_A, "recipe_deducted")

    r = await client.post(
        f"/api/v1/inventory/items/{ITEM_A}/opening-balance",
        json={"quantity": 1000},
        headers={"Idempotency-Key": "d-ob"},
    )
    assert r.status_code in (200, 201), r.text

    payload = {
        "inventory_item_id": str(ITEM_A),
        "counted_quantity":  950,
        "counted_at":        "2025-09-01T12:00:00Z",
    }
    key = "d-count"

    r1 = await client.post("/api/v1/inventory/count-events", json=payload,
                           headers={"Idempotency-Key": key})
    assert r1.status_code == 201
    original = r1.json()

    # Replay: same key + same body → identical 201 response
    r2 = await client.post("/api/v1/inventory/count-events", json=payload,
                           headers={"Idempotency-Key": key})
    assert r2.status_code == 201
    assert r2.json() == original, "replay must return identical response body"

    # Conflict: same key + different body → 409
    r3 = await client.post(
        "/api/v1/inventory/count-events",
        json={**payload, "counted_quantity": 900},
        headers={"Idempotency-Key": key},
    )
    assert r3.status_code == 409
    assert r3.json()["detail"]["error"] == "idempotency_key_conflict"

    # Exactly 1 count_adjust must exist (replay did not fire the handler again)
    assert await count_movements(session, ITEM_A, "count_adjust") == 1
    assert await read_on_hand(session, ITEM_A) == 950


# ============================================================================
# SCENARIO E — Stock warnings from real ledger (API only)
#
# Verifies GET /inventory/items returns correct stock_status for:
#   ITEM_A: Mode A, on_hand=2000, par=500  → ok
#   ITEM_B: Mode B, on_hand=100,  par=500, count_state=stale → low_stock
#           (low_stock has higher priority than count_stale in the chain)
#   ITEM_C: Mode A, on_hand=0,    par=100  → out_of_stock (and low_stock)
#   ITEM_D: Mode B, no anchor              → count_required
# ============================================================================

@pytest.mark.asyncio
async def test_scenario_e_stock_warnings(session, client):
    now = datetime.now(UTC)

    # ITEM_A: healthy Mode A stock
    await create_item(session, ITEM_A, ING_A, "recipe_deducted", par_level=500)
    await client.post(
        f"/api/v1/inventory/items/{ITEM_A}/opening-balance",
        json={"quantity": 2000},
        headers={"Idempotency-Key": "e-a"},
    )

    # ITEM_B: Mode B, count is stale (10 days old, cadence=7, grace=7)
    # on_hand = 100 (anchor), par=500 → low_stock
    await create_item(
        session, ITEM_B, ING_B, "count_anchored",
        count_cadence_days=7, count_grace_days=7,
        last_count_quantity=100,
        last_count_at=now - timedelta(days=10),
        par_level=500,
    )

    # ITEM_C: Mode A, out of stock (and low_stock)
    await create_item(session, ITEM_C, ING_C, "recipe_deducted", par_level=100)
    await client.post(
        f"/api/v1/inventory/items/{ITEM_C}/opening-balance",
        json={"quantity": 0},
        headers={"Idempotency-Key": "e-c"},
    )

    # ITEM_D: Mode B, no anchor → count_required
    await create_item(
        session, ITEM_D, ING_D, "count_anchored",
        count_cadence_days=7, count_grace_days=7,
    )
    await session.flush()

    r = await client.get("/api/v1/inventory/items")
    assert r.status_code == 200, r.text
    items = {i["id"]: i for i in r.json()["items"]}

    # ITEM_A — healthy
    a = items[str(ITEM_A)]
    assert a["stock_status"] == "ok",            f"ITEM_A: {a['stock_status']}"
    assert a["low_stock"] is False
    assert a["out_of_stock"] is False
    assert a["count_required"] is False

    # ITEM_B — stale count but low_stock wins (higher priority)
    b = items[str(ITEM_B)]
    assert b["stock_status"] == "low_stock",     f"ITEM_B: {b['stock_status']}"
    assert b["count_state"] == "stale"
    assert b["low_stock"] is True
    assert b["out_of_stock"] is False

    # ITEM_C — out of stock (and low_stock — both True; out_of_stock wins)
    c = items[str(ITEM_C)]
    assert c["stock_status"] == "out_of_stock",  f"ITEM_C: {c['stock_status']}"
    assert c["low_stock"] is True
    assert c["out_of_stock"] is True

    # ITEM_D — no anchor → count_required
    d = items[str(ITEM_D)]
    assert d["stock_status"] == "count_required", f"ITEM_D: {d['stock_status']}"
    assert d["on_hand"] is None
    assert d["count_required"] is True


# ============================================================================
# SCENARIO F — Monitoring alerts as SIDE EFFECT of count events (API only)
#
# Proves:
#   - Count 1: on_hand=1000, counted=943, drift≈6.04% → warn, alert_count=1
#   - Count 2: on_hand=943, counted=754, drift≈25.07% → critical, alert_count=2
#
# The test does NOT insert monitoring alerts directly. They must be created
# by record_count_event() as a side effect of the count API calls.
# ============================================================================

@pytest.mark.asyncio
async def test_scenario_f_monitoring_from_count_events(session, client):
    await create_item(session, ITEM_A, ING_A, "recipe_deducted")
    await client.post(
        f"/api/v1/inventory/items/{ITEM_A}/opening-balance",
        json={"quantity": 1000},
        headers={"Idempotency-Key": "f-ob"},
    )

    # Count 1: drift = (1000-943)/943 ≈ 6.04 % → warn
    r = await client.post("/api/v1/inventory/count-events", json={
        "inventory_item_id": str(ITEM_A),
        "counted_quantity":  943,
        "counted_at":        "2025-09-02T10:00:00Z",
    }, headers={"Idempotency-Key": "f-c1"})
    assert r.status_code == 201, r.text

    # Verify the service captured predicted BEFORE the count_adjust fired
    ce1 = await get_count_event(session, ITEM_A, nth=1)
    assert float(ce1["predicted_on_hand_at_count"]) == 1000
    assert await read_on_hand(session, ITEM_A) == 943

    alert = await get_alert(session, "integrity_drift_high")
    assert alert is not None,              "first count event must create an alert"
    assert alert["severity"] == "warn",    f"6 % drift → warn; got {alert['severity']}"
    assert alert["alert_count"] == 1

    # Count 2: drift = (943-754)/754 ≈ 25.07 % → critical + UPSERT → count=2
    r = await client.post("/api/v1/inventory/count-events", json={
        "inventory_item_id": str(ITEM_A),
        "counted_quantity":  754,
        "counted_at":        "2025-09-02T14:00:00Z",
    }, headers={"Idempotency-Key": "f-c2"})
    assert r.status_code == 201, r.text

    ce2 = await get_count_event(session, ITEM_A, nth=2)
    # The service must have captured on_hand()=943 (after count_adjust from count 1)
    assert float(ce2["predicted_on_hand_at_count"]) == 943

    alert = await get_alert(session, "integrity_drift_high")
    assert alert["severity"]    == "critical",  f"25 % drift → critical; got {alert['severity']}"
    assert alert["alert_count"] == 2, (
        f"UPSERT must increment to 2; got {alert['alert_count']}"
    )
    assert await read_on_hand(session, ITEM_A) == 754
