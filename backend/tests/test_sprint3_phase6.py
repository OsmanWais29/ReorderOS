"""Sprint 3 - Phase 6: GET /inventory/items Verification

Covers every spec scenario for stock_status, count_state, and related flags:
  6.1  — Mode A ok
  6.2  — Mode A low_stock
  6.3  — Mode A out_of_stock (both low_stock and out_of_stock are True)
  6.4  — Mode B no anchor (all nulls, count_required)
  6.5  — Mode B fresh count
  6.6  — Mode B stale count
  6.7  — Mode B expired count
  6.8  — Priority: low_stock beats stale
  6.9  — Mode B with NULL count_cadence_days → count_state=null (defensive)
  6.10 — Missing yield_factor defaults to 1.0

Design notes:
  - count_state is computed LIVE from (last_count_at, count_cadence_days,
    count_grace_days, now()), not read from inventory_items.confidence_state.
  - stock_status priority: count_required > out_of_stock > low_stock >
    count_expired > count_stale > ok
  - low_stock and out_of_stock are independent booleans; an item can be both.
  - Tests never use negative receive movements. Stock levels are controlled
    by setting last_count_quantity directly for Mode B items.
  - on_hand is returned as a JSON number (float) not a string.

Bugs fixed vs the original test spec
  - USER_ID / UOM_GRAM / ING_A/B/C used non-hex prefix chars (u, m, g) —
    uuid.UUID() raises ValueError at import time.  Replaced with hex UUIDs.
  - ITEM_B collided with TENANT_ID (both "b000…1").  ITEM_B changed to "b2…".
  - Fixture named `transaction` renamed to `session` so test bodies can
    request it as a named parameter.
  - get_db_session(bind=conn) → make_bound_session(conn) (FastAPI-safe split).
  - get_principal mock returned a plain dict; routes use attribute access.
    Mock now returns a real Principal dataclass.
  - URL path /inventory/items → /api/v1/inventory/items.
  - session.execute("…") bare strings → wrapped with text().
  - gen_random_uuid()::text inside raw SQL → uuid4() from Python.
  - display_name → name (our Sprint 3 schema column).
  - FK prerequisite rows (tenant, user, uom, ingredients) now seeded in the
    fixture so all tests have the necessary foreign-key anchors.
  - Test 6.9 adapted: cadence_coherence CHECK only enforces NULL cadence/grace
    for Mode A items (spec §7.5).  Mode B items with NULL cadence_days are
    accepted by the DB; the API returns count_state=null (defensive path).

All tests run inside a savepoint transaction that rolls back in teardown.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.database import engine, get_db_session, make_bound_session
from app.core.security import Principal, get_principal
from app.main import create_app

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants — all UUIDs use hex-valid prefix characters only
# ---------------------------------------------------------------------------
TENANT_ID = uuid.UUID("aa000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("ab000000-0000-0000-0000-000000000001")  # was 'u' (invalid)
ITEM_A = uuid.UUID("ac000000-0000-0000-0000-000000000001")  # Mode A
ITEM_B = uuid.UUID("ac000000-0000-0000-0000-000000000002")  # Mode B (was "b000…1" = TENANT_ID)
ITEM_C = uuid.UUID("ac000000-0000-0000-0000-000000000003")  # Mode B no anchor
UOM_GRAM = uuid.UUID("ad000000-0000-0000-0000-000000000001")  # was 'm' (invalid)
ING_A = uuid.UUID("ae000000-0000-0000-0000-000000000001")  # was 'g' (invalid)
ING_B = uuid.UUID("ae000000-0000-0000-0000-000000000002")
ING_C = uuid.UUID("ae000000-0000-0000-0000-000000000003")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app_instance():
    return create_app()


@pytest.fixture(scope="module")
async def client(app_instance):
    async with AsyncClient(transport=ASGITransport(app=app_instance), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def session(app_instance):
    """
    Per-test transaction isolation.

    Seeds all FK prerequisite rows (tenant, user, uom, ingredients) within the
    transaction so every test starts with a clean, consistent baseline.
    Rolls back on teardown — no data persists between tests.

    Uses join_transaction_mode="create_savepoint" (via make_bound_session) so
    session.commit() inside tests wraps flushes in nested SAVEPOINTs without
    touching the outer BEGIN. Teardown calls conn.rollback() to discard all data.
    """
    async with engine.connect() as conn:
        trans = await conn.begin()
        db = make_bound_session(conn)

        app_instance.dependency_overrides[get_db_session] = lambda: db
        app_instance.dependency_overrides[get_principal] = lambda: Principal(
            user_id=str(USER_ID),
            workos_id="wos_phase6_test",
            email="phase6@test.example",
            tenant_id=str(TENANT_ID),
            role="manager",
        )

        # ── FK prerequisites ───────────────────────────────────────────────
        await conn.execute(
            text("""
            INSERT INTO tenants (id, slug, name)
            VALUES (:id, 'phase6-tenant', 'Phase 6 Tenant')
            ON CONFLICT (id) DO NOTHING
        """),
            {"id": TENANT_ID},
        )

        await conn.execute(
            text("""
            INSERT INTO users (id, workos_id, email, email_verified)
            VALUES (:id, 'wos_phase6_test', 'phase6@test.example', true)
            ON CONFLICT (id) DO NOTHING
        """),
            {"id": USER_ID},
        )

        await conn.execute(
            text("""
            INSERT INTO units_of_measure (id, tenant_id, name, abbreviation, unit_type)
            VALUES (:id, :tid, 'grams-p6', 'g', 'weight')
            ON CONFLICT (id) DO NOTHING
        """),
            {"id": UOM_GRAM, "tid": TENANT_ID},
        )

        for ing_id, ing_name in [
            (ING_A, "Ingredient A (p6)"),
            (ING_B, "Ingredient B (p6)"),
            (ING_C, "Ingredient C (p6)"),
        ]:
            await conn.execute(
                text("""
                INSERT INTO ingredients_master (id, tenant_id, name)
                VALUES (:id, :tid, :name)
                ON CONFLICT (id) DO NOTHING
            """),
                {"id": ing_id, "tid": TENANT_ID, "name": ing_name},
            )

        try:
            yield db
        finally:
            await db.close()
            await trans.rollback()  # undo all test changes
            app_instance.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INGREDIENT_MAP = {ITEM_A: ING_A, ITEM_B: ING_B, ITEM_C: ING_C}


async def seed_item(session, item_id, mode, **kwargs):
    """Create or update an inventory item using our Sprint 3 schema."""
    await session.execute(
        text("""
        INSERT INTO inventory_items (
            id, tenant_id, ingredient_id, name,
            inventory_mode, storage_unit_id, purchase_unit_id,
            recipe_unit_id, storage_to_recipe_factor,
            par_level, count_cadence_days, count_grace_days,
            last_count_quantity, last_count_at
        ) VALUES (
            :id, :tid, :ing, :name, :mode, :su, :pu, :ru, 1.0,
            :par, :cad, :grace, :lcq, :lca
        ) ON CONFLICT (id) DO UPDATE SET
            par_level           = EXCLUDED.par_level,
            count_cadence_days  = EXCLUDED.count_cadence_days,
            count_grace_days    = EXCLUDED.count_grace_days,
            last_count_quantity = EXCLUDED.last_count_quantity,
            last_count_at       = EXCLUDED.last_count_at
    """),
        {
            "id": item_id,
            "tid": TENANT_ID,
            "ing": _INGREDIENT_MAP.get(item_id, ING_A),
            "name": f"Item {str(item_id)[-4:]}",
            "mode": mode,
            "su": UOM_GRAM,
            "pu": UOM_GRAM,
            "ru": UOM_GRAM,
            "par": kwargs.get("par_level"),
            "cad": kwargs.get("count_cadence_days"),
            "grace": kwargs.get("count_grace_days"),
            "lcq": kwargs.get("last_count_quantity"),
            "lca": kwargs.get("last_count_at"),
        },
    )
    await session.flush()


async def add_movement(session, item_id, movement_type, delta, recorded_at):
    """Insert an inventory movement with a unique idempotency key."""
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
        INSERT INTO inventory_movements (
            tenant_id, inventory_item_id, movement_type, delta,
            source_type, idempotency_key, recorded_at
        ) VALUES (
            :tid, :iid, :mtype, :delta, :stype, :ikey, :ts
        )
    """),
        {
            "tid": TENANT_ID,
            "iid": item_id,
            "mtype": movement_type,
            "delta": delta,
            "stype": source_map.get(movement_type, "manual"),
            "ikey": str(uuid.uuid4()),  # unique per call; no gen_random_uuid() in raw SQL
            "ts": recorded_at,
        },
    )
    await session.flush()


async def get_item(client, item_id):
    """Fetch a single item from the GET /api/v1/inventory/items response."""
    r = await client.get("/api/v1/inventory/items")  # correct prefix
    assert r.status_code == 200, r.text
    for i in r.json()["items"]:
        if i["id"] == str(item_id):
            return i
    raise AssertionError(f"Item {item_id} not found in GET /api/v1/inventory/items")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_6_1_mode_a_ok(session, client):
    """Mode A, on_hand=500, par=100 → stock_status=ok, all flags nominal."""
    await seed_item(session, ITEM_A, "recipe_deducted", par_level=100)
    await add_movement(session, ITEM_A, "opening_balance", 500, datetime(2025, 9, 1, tzinfo=UTC))
    item = await get_item(client, ITEM_A)
    assert item["on_hand"] == 500
    assert item["stock_status"] == "ok"
    assert item["count_required"] is False
    assert item["count_state"] is None
    assert item["low_stock"] is False
    assert item["out_of_stock"] is False


@pytest.mark.asyncio
async def test_6_2_mode_a_low_stock(session, client):
    """Mode A, on_hand=50 ≤ par=100 → stock_status=low_stock."""
    await seed_item(session, ITEM_A, "recipe_deducted", par_level=100)
    await add_movement(session, ITEM_A, "opening_balance", 50, datetime(2025, 9, 1, tzinfo=UTC))
    item = await get_item(client, ITEM_A)
    assert item["on_hand"] == 50
    assert item["stock_status"] == "low_stock"
    assert item["low_stock"] is True
    assert item["out_of_stock"] is False


@pytest.mark.asyncio
async def test_6_3_mode_a_out_of_stock(session, client):
    """
    Mode A, on_hand=0, par=100.
    Both low_stock (0 ≤ 100) and out_of_stock (0 ≤ 0) are True.
    stock_status = out_of_stock (higher priority than low_stock in the chain).
    """
    await seed_item(session, ITEM_A, "recipe_deducted", par_level=100)
    await add_movement(session, ITEM_A, "opening_balance", 0, datetime(2025, 9, 1, tzinfo=UTC))
    item = await get_item(client, ITEM_A)
    assert item["on_hand"] == 0
    assert item["stock_status"] == "out_of_stock"
    assert item["low_stock"] is True  # independent: 0 ≤ 100
    assert item["out_of_stock"] is True  # independent: 0 ≤ 0


@pytest.mark.asyncio
async def test_6_4_mode_b_no_anchor(session, client):
    """
    Mode B, no last_count_quantity → on_hand=null, count_required=true.
    All stock flags are null (unknown without a reliable reading).
    """
    await seed_item(
        session,
        ITEM_B,
        "count_anchored",
        count_cadence_days=7,
        count_grace_days=9,
        last_count_quantity=None,
        last_count_at=None,
    )
    item = await get_item(client, ITEM_B)
    assert item["on_hand"] is None
    assert item["stock_status"] == "count_required"
    assert item["count_required"] is True
    assert item["count_state"] is None
    assert item["low_stock"] is None
    assert item["out_of_stock"] is None


@pytest.mark.asyncio
async def test_6_5_mode_b_fresh(session, client):
    """
    Mode B, count 2 days ago (cadence=7) → count_state=fresh.
    on_hand = 100 (anchor) + 20 (receive after count) = 120.
    """
    now = datetime.now(UTC)
    await seed_item(
        session,
        ITEM_B,
        "count_anchored",
        count_cadence_days=7,
        count_grace_days=9,
        last_count_quantity=100,
        last_count_at=now - timedelta(days=2),
        par_level=50,
    )
    await add_movement(session, ITEM_B, "receive", 20, now - timedelta(days=1))
    item = await get_item(client, ITEM_B)
    assert item["on_hand"] == 120
    assert item["stock_status"] == "ok"
    assert item["count_state"] == "fresh"
    assert item["low_stock"] is False  # 120 > 50
    assert item["out_of_stock"] is False


@pytest.mark.asyncio
async def test_6_6_mode_b_stale(session, client):
    """
    Mode B, count 10 days ago (cadence=7, grace=7) → stale (7 < 10 ≤ 14).
    on_hand = 70 (anchor, no movements since).
    grace=7 satisfies v5 constraint: grace >= cadence.
    """
    now = datetime.now(UTC)
    await seed_item(
        session,
        ITEM_B,
        "count_anchored",
        count_cadence_days=7,
        count_grace_days=7,
        last_count_quantity=70,
        last_count_at=now - timedelta(days=10),
        par_level=50,
    )
    item = await get_item(client, ITEM_B)
    assert item["on_hand"] == 70
    assert item["count_state"] == "stale"
    assert item["stock_status"] == "count_stale"
    assert item["low_stock"] is False  # 70 > 50
    assert item["out_of_stock"] is False


@pytest.mark.asyncio
async def test_6_7_mode_b_expired(session, client):
    """
    Mode B, count 20 days ago (cadence=7, grace=7) → expired (20 > 14).
    on_hand = 100 + 30 receive = 130.
    grace=7 satisfies v5 constraint: grace >= cadence.
    """
    now = datetime.now(UTC)
    await seed_item(
        session,
        ITEM_B,
        "count_anchored",
        count_cadence_days=7,
        count_grace_days=7,
        last_count_quantity=100,
        last_count_at=now - timedelta(days=20),
        par_level=50,
    )
    await add_movement(session, ITEM_B, "receive", 30, now - timedelta(days=1))
    item = await get_item(client, ITEM_B)
    assert item["on_hand"] == 130
    assert item["count_state"] == "expired"
    assert item["stock_status"] == "count_expired"
    assert item["low_stock"] is False  # 130 > 50


@pytest.mark.asyncio
async def test_6_8_mode_b_low_stock_beats_stale(session, client):
    """
    Priority test: low_stock (on_hand=30 ≤ par=50) beats stale in the chain.
    stock_status = low_stock, not count_stale.
    grace=7 satisfies v5 constraint: grace >= cadence.
    """
    now = datetime.now(UTC)
    await seed_item(
        session,
        ITEM_B,
        "count_anchored",
        count_cadence_days=7,
        count_grace_days=7,
        last_count_quantity=30,
        last_count_at=now - timedelta(days=10),
        par_level=50,
    )
    item = await get_item(client, ITEM_B)
    assert item["on_hand"] == 30
    assert item["count_state"] == "stale"
    assert item["low_stock"] is True
    assert item["stock_status"] == "low_stock"  # priority: low_stock > stale


@pytest.mark.asyncio
async def test_6_9_cadence_coherence_rejects_mode_b_null_cadence(session, client):
    """
    v5 cadence_coherence CHECK rejects Mode B items with NULL cadence/grace.

    A Mode B item without count_cadence_days is operationally broken:
      - count_state can never be computed
      - the confidence state machine doesn't advance
      - the nightly job silently skips the item

    The DB enforces this at write time so such zombies never enter the ledger.
    The defensive API path (count_state=null + warning) is a safety net for
    data that pre-dates this migration, not a substitute for the constraint.

    The v5 constraint requires for count_anchored:
      count_cadence_days IS NOT NULL
      count_grace_days   IS NOT NULL
      count_grace_days   >= count_cadence_days
    """
    now = datetime.now(UTC)
    with pytest.raises(Exception) as exc_info:
        await seed_item(
            session,
            ITEM_B,
            "count_anchored",
            count_cadence_days=None,  # violates IS NOT NULL
            count_grace_days=None,
            last_count_quantity=100,
            last_count_at=now - timedelta(days=3),
            par_level=50,
        )
    error_msg = str(exc_info.value).lower()
    assert "cadence_coherence" in error_msg or "check" in error_msg, (
        f"Expected cadence_coherence CHECK violation, got: {exc_info.value}"
    )


@pytest.mark.asyncio
async def test_6_10_missing_yield_factor_defaults_to_one(session, client):
    """
    Mode B item with no inventory_yield_factors row.
    on_hand = 100 - (20 x COALESCE(NULL, 1.0)) = 80.
    """
    anchor_at = datetime(2025, 6, 1, 8, 0, tzinfo=UTC)
    signal_at = datetime(2025, 6, 2, 12, 0, tzinfo=UTC)
    await seed_item(
        session,
        ITEM_B,
        "count_anchored",
        count_cadence_days=7,
        count_grace_days=9,
        last_count_quantity=100,
        last_count_at=anchor_at,
        par_level=50,
    )
    # Ensure no yield factor row exists for this item in this tenant
    await session.execute(
        text("""
        DELETE FROM inventory_yield_factors
        WHERE tenant_id = :tid AND inventory_item_id = :iid
    """),
        {"tid": TENANT_ID, "iid": ITEM_B},
    )
    await add_movement(session, ITEM_B, "sale_signal", 20, signal_at)
    await session.flush()
    item = await get_item(client, ITEM_B)
    assert item["on_hand"] == 80  # 100 - (20 x 1.0) via COALESCE default
