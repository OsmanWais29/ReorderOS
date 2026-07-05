"""Sprint 3 - Phase 5: Idempotency Middleware Verification

Covers:
  5.1 — Replay with same key + body returns original response (1 event + 1 adjust)
  5.2 — Same key + different body → 409 Conflict
  5.3 — In-flight detection (response_status IS NULL) → 409 request_in_flight
  5.4 — Cross-endpoint idempotency (opening-balance)
  5.5 — Canonical JSON normalisation (key ordering)

Design notes:
  - The POST /inventory/count-events endpoint accepts {inventory_item_id,
    counted_quantity, counted_at}. It does NOT accept predicted_on_hand_at_count
    from the client — the service calls on_hand() internally and writes the
    result into the count event row (Sprint 3 Phase 4C contract).
  - Therefore every test that creates a count event MUST seed an opening
    balance first so on_hand() returns a known value.
  - The compute_fingerprint import is isolated inside Test 5.3 only, with a
    pytest.skip fallback if the module path doesn't match the implementation.
  - Each test runs inside a savepoint that is rolled back in teardown.

Bugs fixed vs. original test spec
  - USER_ID / UOM_GRAM / INGREDIENT_A used non-hex prefix chars (u, m, g)
    which make uuid.UUID() raise ValueError at import time.  Replaced with
    valid hex-prefixed UUIDs that are visually distinct.
  - Column name display_name → name (our Sprint 3 schema).
  - Fixture named `transaction` renamed to `session` so test functions can
    request it by the expected parameter name.
  - session.execute("SELECT …") → session.execute(text("SELECT …"))
    (SQLAlchemy 2.x rejects bare string statements).
  - get_principal mock returned a plain dict; routes use attribute access
    (principal.tenant_id).  Mock now returns a real Principal dataclass.
  - URL paths restored to include the /api/v1/ prefix used by create_app().
  - compute_fingerprint path argument in test 5.3 updated to /api/v1/…
    so the pre-inserted fingerprint matches what the route handler computes
    from request.url.path.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.database import engine, get_db_session, make_bound_session
from app.core.security import Principal, get_principal
from app.main import create_app

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants — all UUIDs use hex-valid prefix characters
# ---------------------------------------------------------------------------
TENANT_ID = uuid.UUID("b0000000-0000-0000-0000-000000000001")  # 'b' is hex
USER_ID = uuid.UUID("b1000000-0000-0000-0000-000000000001")  # was 'u' (invalid)
ITEM_A = uuid.UUID("a0000000-0000-0000-0000-000000000001")  # 'a' is hex
UOM_GRAM = uuid.UUID("c0000000-0000-0000-0000-000000000001")  # was 'm' (invalid)
INGREDIENT_A = uuid.UUID("d0000000-0000-0000-0000-000000000001")  # was 'g' (invalid)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app_instance():
    """The FastAPI application instance (module-scoped: built once per module)."""
    return create_app()


@pytest.fixture(scope="module")
async def client(app_instance):
    async with AsyncClient(transport=ASGITransport(app=app_instance), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def session(app_instance):
    """
    Per-test transaction isolation.

    1. Opens a connection and starts an explicit outer BEGIN.
    2. make_bound_session uses join_transaction_mode="create_savepoint" so
       every session.commit() inside a test wraps its flush in a nested
       SAVEPOINT then releases it — the outer BEGIN stays intact.
    3. Overrides get_db_session and get_principal for all routes in this test.
    4. Yields the session so test bodies can run raw queries.
    5. Rolls back the outer BEGIN in teardown — all test data vanishes.
    """
    async with engine.connect() as conn:
        trans = await conn.begin()
        db = make_bound_session(conn)

        app_instance.dependency_overrides[get_db_session] = lambda: db
        app_instance.dependency_overrides[get_principal] = lambda: Principal(
            user_id=str(USER_ID),
            workos_id="wos_phase5_test",
            email="phase5@test.example",
            tenant_id=str(TENANT_ID),
            role="manager",
        )

        # Clean up any leftover data from previous runs (handles the case where
        # a prior run's savepoint failed to roll back).
        for tbl in (
            "ingredient_cost_snapshots",
            "receipt_lines",
            "inventory_count_events",
            "inventory_movements",
            "monitoring_alerts",
            "idempotency_keys",
            "receipts",
            "inventory_yield_factors",
            "recipe_ingredients",
            "inventory_items",
            "recipe_versions",
            "sale_line_items",
            "orders",
            "pos_event_inbox",
        ):
            await conn.execute(
                text(f"DELETE FROM {tbl} WHERE tenant_id = :tid"),
                {"tid": TENANT_ID},
            )

        # Seed prerequisite FK rows so item inserts succeed.
        await conn.execute(
            text("""
            INSERT INTO tenants (id, slug, name)
            VALUES (:id, 'phase5-tenant', 'Phase 5 Test Tenant')
            ON CONFLICT (id) DO NOTHING
        """),
            {"id": TENANT_ID},
        )

        await conn.execute(
            text("""
            INSERT INTO users (id, workos_id, email, email_verified)
            VALUES (:id, 'wos_phase5_test', 'phase5@test.example', true)
            ON CONFLICT (id) DO NOTHING
        """),
            {"id": USER_ID},
        )

        await conn.execute(
            text("""
            INSERT INTO units_of_measure (id, tenant_id, name, abbreviation, unit_type)
            VALUES (:id, :tid, 'grams-p5', 'g', 'weight')
            ON CONFLICT (id) DO NOTHING
        """),
            {"id": UOM_GRAM, "tid": TENANT_ID},
        )

        await conn.execute(
            text("""
            INSERT INTO ingredients_master (id, tenant_id, name)
            VALUES (:id, :tid, 'Test Ingredient Phase 5')
            ON CONFLICT (id) DO NOTHING
        """),
            {"id": INGREDIENT_A, "tid": TENANT_ID},
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


async def seed_item_with_stock(session, item_id, mode, opening_qty):
    """Create an inventory item AND seed an opening balance so on_hand() is known."""
    await session.execute(
        text("""
        INSERT INTO inventory_items (
            id, tenant_id, ingredient_id, name,
            inventory_mode, storage_unit_id, purchase_unit_id,
            recipe_unit_id, storage_to_recipe_factor
        ) VALUES (
            :id, :tid, :ing, :name, :mode, :su, :pu, :ru, 1.0
        ) ON CONFLICT (id) DO NOTHING
    """),
        {
            "id": item_id,
            "tid": TENANT_ID,
            "ing": INGREDIENT_A,
            "name": f"Item {str(item_id)[:8]}",
            "mode": mode,
            "su": UOM_GRAM,
            "pu": UOM_GRAM,
            "ru": UOM_GRAM,
        },
    )
    if opening_qty is not None:
        await session.execute(
            text("""
            INSERT INTO inventory_movements (
                tenant_id, inventory_item_id, movement_type, delta,
                source_type, idempotency_key, recorded_at
            ) VALUES (
                :tid, :iid, 'opening_balance', :qty,
                'opening', :ikey, '2025-09-01T08:00:00Z'
            ) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
        """),
            {
                "tid": TENANT_ID,
                "iid": item_id,
                "qty": opening_qty,
                "ikey": f"ob:phase5:{item_id}",
            },
        )
    await session.flush()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_5_1_replay_returns_original_and_only_one_adjust(session, client: AsyncClient):
    """
    Replay with same key + same body → returns stored response.
    Exactly 1 count event and 1 count_adjust movement exist.

    Trace:
      on_hand(ITEM_A) = 100 (from opening balance)
      Service writes predicted_on_hand_at_count = 100
      counted_quantity = 95
      Trigger: drift = 95 - 100 = -5 → emits count_adjust with delta = -5
      on_hand after: 100 + (-5) = 95

      Second request: middleware finds same key + same fingerprint + response cached
      → returns stored response without executing handler
      → still 1 count event + 1 count_adjust
    """
    await seed_item_with_stock(session, ITEM_A, "recipe_deducted", 100)

    payload = {
        "inventory_item_id": str(ITEM_A),
        "counted_quantity": 95,
        "counted_at": "2025-09-01T10:00:00Z",
    }
    key = "replay-key-1"

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
    assert r2.json() == original, "replay did not return identical body"

    cnt = await session.execute(
        text("SELECT COUNT(*) FROM inventory_count_events WHERE inventory_item_id = :iid"),
        {"iid": ITEM_A},
    )
    assert cnt.scalar() == 1, "expected exactly 1 count event"

    adj = await session.execute(
        text("""
        SELECT COUNT(*) FROM inventory_movements
        WHERE inventory_item_id = :iid AND movement_type = 'count_adjust'
    """),
        {"iid": ITEM_A},
    )
    assert adj.scalar() == 1, "expected exactly 1 count_adjust movement"


@pytest.mark.asyncio
async def test_5_2_different_body_same_key_409(session, client: AsyncClient):
    """
    Same key + different payload → 409.

    Trace:
      Request 1: counted_quantity=100, on_hand=100 → zero drift → no count_adjust
      Request 2: counted_quantity=90, same key, different fingerprint → 409
      Only one count event exists (the first one)
    """
    await seed_item_with_stock(session, ITEM_A, "recipe_deducted", 100)

    payload1 = {
        "inventory_item_id": str(ITEM_A),
        "counted_quantity": 100,
        "counted_at": "2025-09-01T10:00:00Z",
    }
    key = "conflict-key-1"
    r1 = await client.post(
        "/api/v1/inventory/count-events",
        json=payload1,
        headers={"Idempotency-Key": key},
    )
    assert r1.status_code == 201

    payload2 = {
        "inventory_item_id": str(ITEM_A),
        "counted_quantity": 90,
        "counted_at": "2025-09-01T10:00:00Z",
    }
    r2 = await client.post(
        "/api/v1/inventory/count-events",
        json=payload2,
        headers={"Idempotency-Key": key},
    )
    assert r2.status_code == 409
    detail = r2.json().get("detail", r2.json())
    assert detail.get("error") == "idempotency_key_conflict"

    cnt = await session.execute(
        text("SELECT COUNT(*) FROM inventory_count_events WHERE inventory_item_id = :iid"),
        {"iid": ITEM_A},
    )
    assert cnt.scalar() == 1, "second request must not create a second event"


@pytest.mark.asyncio
async def test_5_3_in_flight_detection(session, client: AsyncClient):
    """
    Concurrent request with same key where first request hasn't completed
    (response_status IS NULL) → 409 with error=request_in_flight.

    We pre-insert an idempotency_keys row with the correct fingerprint and
    NULL response_status to simulate a handler that's still running.
    The fingerprint is computed using the real middleware function.

    Trace:
      Middleware finds (tenant_id, key) → fingerprint matches → response_status IS NULL
      → returns 409 {"error": "request_in_flight", "retry_after_seconds": 5}
      Handler is never invoked.
    """
    await seed_item_with_stock(session, ITEM_A, "recipe_deducted", 100)

    key = "in-flight-key-1"
    payload = {
        "inventory_item_id": str(ITEM_A),
        "counted_quantity": 100,
        "counted_at": "2025-09-01T10:00:00Z",
    }

    try:
        from app.modules.inventory.idempotency import compute_fingerprint
    except ImportError:
        pytest.skip(
            "compute_fingerprint not found — update import path to match "
            "your idempotency middleware module"
        )

    # Path MUST match request.url.path that the route handler will see.
    method = "POST"
    path = "/api/v1/inventory/count-events"
    fingerprint = compute_fingerprint(method, path, payload)

    await session.execute(
        text("""
        INSERT INTO idempotency_keys
            (tenant_id, key, request_fingerprint, response_status)
        VALUES (:tid, :key, :fp, NULL)
        ON CONFLICT DO NOTHING
    """),
        {"tid": TENANT_ID, "key": key, "fp": fingerprint},
    )
    await session.flush()

    r = await client.post(
        "/api/v1/inventory/count-events",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
    data = r.json().get("detail", r.json())
    assert data.get("error") == "request_in_flight"
    assert "retry_after_seconds" in data


@pytest.mark.asyncio
async def test_5_4_cross_endpoint_idempotency(session, client: AsyncClient):
    """
    Idempotency also works on the opening-balance endpoint.

    Trace:
      Request 1: inserts opening_balance movement, returns 201
      Request 2: same key + same body → returns stored response
      Only one opening_balance movement exists
    """
    await session.execute(
        text("""
        INSERT INTO inventory_items (
            id, tenant_id, ingredient_id, name,
            inventory_mode, storage_unit_id, purchase_unit_id,
            recipe_unit_id, storage_to_recipe_factor
        ) VALUES (
            :id, :tid, :ing, :name, 'recipe_deducted', :su, :pu, :ru, 1.0
        ) ON CONFLICT (id) DO NOTHING
    """),
        {
            "id": ITEM_A,
            "tid": TENANT_ID,
            "ing": INGREDIENT_A,
            "name": "Item for OB test",
            "su": UOM_GRAM,
            "pu": UOM_GRAM,
            "ru": UOM_GRAM,
        },
    )
    await session.flush()

    key = "ob-idem-key"
    payload = {"quantity": 300}

    r1 = await client.post(
        f"/api/v1/inventory/items/{ITEM_A}/opening-balance",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert r1.status_code in (200, 201), r1.text

    r2 = await client.post(
        f"/api/v1/inventory/items/{ITEM_A}/opening-balance",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert r2.status_code in (200, 201), r2.text
    assert r2.json() == r1.json(), "replay must return identical body"

    cnt = await session.execute(
        text("""
        SELECT COUNT(*) FROM inventory_movements
        WHERE inventory_item_id = :iid AND movement_type = 'opening_balance'
    """),
        {"iid": ITEM_A},
    )
    assert cnt.scalar() == 1, "replay must not create a second movement"


@pytest.mark.asyncio
async def test_5_5_canonical_json_key_ordering(session, client: AsyncClient):
    """
    Different JSON key orderings produce the same fingerprint → treated as replay.

    Trace:
      Request 1: keys in arbitrary order → counted=100, on_hand=100 → zero drift
      Request 2: keys in canonical (sorted) order, same data →
                 fingerprint matches after canonical normalisation → replay
      Only one count event exists
    """
    await seed_item_with_stock(session, ITEM_A, "recipe_deducted", 100)

    key = "norm-key-1"

    # Keys deliberately out of alphabetical order
    payload_out_of_order = {
        "counted_at": "2025-09-01T10:00:00Z",
        "inventory_item_id": str(ITEM_A),
        "counted_quantity": 100,
    }
    # Keys in alphabetical / canonical order
    payload_canonical = {
        "counted_at": "2025-09-01T10:00:00Z",
        "counted_quantity": 100,
        "inventory_item_id": str(ITEM_A),
    }

    r1 = await client.post(
        "/api/v1/inventory/count-events",
        json=payload_out_of_order,
        headers={"Idempotency-Key": key},
    )
    assert r1.status_code == 201

    r2 = await client.post(
        "/api/v1/inventory/count-events",
        json=payload_canonical,
        headers={"Idempotency-Key": key},
    )
    assert r2.status_code == 201
    assert r2.json() == r1.json(), "canonical key sort must produce the same fingerprint"

    cnt = await session.execute(
        text("SELECT COUNT(*) FROM inventory_count_events WHERE inventory_item_id = :iid"),
        {"iid": ITEM_A},
    )
    assert cnt.scalar() == 1, "canonical normalisation must deduplicate the request"
