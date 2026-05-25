"""Sprint 4 — Phase 3: orders + sale_line_items (28 test functions).

Schema under test:
  orders:          UNIQUE (tenant_id, clover_order_id), 3 CHECKs, 8 indexes
  sale_line_items: UNIQUE (tenant_id, clover_line_item_id), 2 CHECKs, 4 indexes
  RLS:             T1 for BOTH app_user AND service_worker (SET LOCAL required)
  Grants:          app_user SELECT only; service_worker SELECT/INSERT/UPDATE (no DELETE)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from uuid6 import uuid7

from tests.helpers.orders import insert_order, insert_sli, make_order_row, make_sli_row
from tests.helpers.orders_seed import seed_order_prereqs

# ── Test 1: Tables Exist ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tables_exist(admin_conn):
    for table in ["orders", "sale_line_items"]:
        exists = await admin_conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables WHERE table_name = $1
            )
        """,
            table,
        )
        assert exists is True, f"Table {table} does not exist"


# ── Test 2: Table Is orders, Not sales ───────────────────────────────────────


@pytest.mark.asyncio
async def test_table_is_orders_not_sales(admin_conn):
    """v5 uses 'orders'. Earlier spec versions used 'sales' — must not exist."""
    exists = await admin_conn.fetchval("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables WHERE table_name = 'sales'
        )
    """)
    assert exists is False, "Table 'sales' exists — should be 'orders'"


# ── Test 3: FK Is order_id, Not sale_id ──────────────────────────────────────


@pytest.mark.asyncio
async def test_fk_is_order_id(admin_conn):
    """sale_line_items references orders via order_id, not sale_id."""
    col_exists = await admin_conn.fetchval("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'sale_line_items' AND column_name = 'order_id'
        )
    """)
    assert col_exists is True

    bad_col = await admin_conn.fetchval("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'sale_line_items' AND column_name = 'sale_id'
        )
    """)
    assert bad_col is False, "Column 'sale_id' exists — should be 'order_id'"


# ── Test 4: Order Creation with SET LOCAL ─────────────────────────────────────


@pytest.mark.asyncio
async def test_order_creation_with_set_local(admin_conn, service_conn):
    """service_worker can create an order only after setting tenant context.

    Validates tightened RLS (T1 for service_worker, not USING(true)).
    """
    seed = await seed_order_prereqs(admin_conn, service_conn)

    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
        }
    )

    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_order(service_conn, order)

    stored = await admin_conn.fetchrow(
        "SELECT state, payment_state, payment_state_previous FROM orders WHERE id = $1",
        order["id"],
    )
    assert stored["state"] == "locked"
    assert stored["payment_state"] == "PAID"
    assert stored["payment_state_previous"] is None


# ── Test 5: service_worker Cannot INSERT Without SET LOCAL ────────────────────


@pytest.mark.asyncio
async def test_service_worker_blocked_without_set_local(admin_conn, service_conn):
    """T1 RLS on orders requires SET LOCAL app.tenant_id.

    Without it, INSERT violates the WITH CHECK policy.
    """
    seed = await seed_order_prereqs(admin_conn, service_conn)
    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
        }
    )

    with pytest.raises(Exception) as exc:
        await insert_order(service_conn, order)
    error_msg = str(exc.value).lower()
    assert "row-level security" in error_msg or "permission" in error_msg or "policy" in error_msg


# ── Test 6: pos_event_inbox_id NOT NULL ──────────────────────────────────────


@pytest.mark.asyncio
async def test_pos_event_inbox_id_not_null(admin_conn, service_conn):
    """Every order must trace to an inbox event.

    Without this, orders could be created with no audit lineage.
    """
    seed = await seed_order_prereqs(admin_conn, service_conn)
    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": None,  # Violates NOT NULL
        }
    )

    with pytest.raises(Exception):
        async with service_conn.transaction():
            await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
            await insert_order(service_conn, order)


# ── Test 7: ON CONFLICT — Duplicate Order Ignored ────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_order_on_conflict_do_nothing(admin_conn, service_conn):
    seed = await seed_order_prereqs(admin_conn, service_conn)
    clover_id = f"DUP-{uuid.uuid4().hex[:8]}"

    order1 = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
            "clover_order_id": clover_id,
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_order(service_conn, order1)

    order2 = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
            "clover_order_id": clover_id,  # Same clover_order_id
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await service_conn.execute(
            """
            INSERT INTO orders (
                id, tenant_id, pos_event_inbox_id, clover_order_id,
                total_amount_cents, state, payment_state, processed_at
            ) VALUES ($1,$2,$3,$4,$5,'locked','PAID',now())
            ON CONFLICT ON CONSTRAINT uq_orders_clover DO NOTHING
        """,
            order2["id"],
            seed["tenant_id"],
            seed["inbox_id"],
            clover_id,
            0,
        )

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM orders WHERE tenant_id = $1 AND clover_order_id = $2",
        seed["tenant_id"],
        clover_id,
    )
    assert count == 1


# ── Test 8: ON CONFLICT — Duplicate Line Item Ignored ────────────────────────


@pytest.mark.asyncio
async def test_duplicate_line_item_on_conflict_do_nothing(admin_conn, service_conn):
    seed = await seed_order_prereqs(admin_conn, service_conn)
    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_order(service_conn, order)

    clover_li_id = f"DUP-LI-{uuid.uuid4().hex[:8]}"

    sli1 = make_sli_row(
        {
            "tenant_id": seed["tenant_id"],
            "order_id": order["id"],
            "clover_line_item_id": clover_li_id,
            "name_at_sale": "Latte",
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_sli(service_conn, sli1)

    sli2 = make_sli_row(
        {
            "tenant_id": seed["tenant_id"],
            "order_id": order["id"],
            "clover_line_item_id": clover_li_id,
            "name_at_sale": "Different Name",
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await service_conn.execute(
            """
            INSERT INTO sale_line_items (
                id, tenant_id, order_id, clover_line_item_id,
                name_at_sale, quantity, price_cents_at_sale, net_revenue_cents
            ) VALUES ($1,$2,$3,$4,$5,1,0,0)
            ON CONFLICT ON CONSTRAINT uq_sli_clover DO NOTHING
        """,
            sli2["id"],
            seed["tenant_id"],
            order["id"],
            clover_li_id,
            "X",
        )

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM sale_line_items WHERE tenant_id = $1 AND clover_line_item_id = $2",
        seed["tenant_id"],
        clover_li_id,
    )
    assert count == 1


# ── Test 9: menu_item_id Nullable (Sprint 4 Default) ─────────────────────────


@pytest.mark.asyncio
async def test_menu_item_id_nullable(admin_conn, service_conn):
    """Sprint 4 leaves menu_item_id NULL for unmapped items.

    Sprint 5 populates it during item mapping.
    """
    seed = await seed_order_prereqs(admin_conn, service_conn)
    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_order(service_conn, order)

    sli = make_sli_row(
        {
            "tenant_id": seed["tenant_id"],
            "order_id": order["id"],
            "menu_item_id": None,
            "name_at_sale": "Unmapped Item",
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_sli(service_conn, sli)

    stored = await admin_conn.fetchval(
        "SELECT menu_item_id FROM sale_line_items WHERE id = $1",
        sli["id"],
    )
    assert stored is None


# ── Test 10: CHECK Constraints — Negative Tests ───────────────────────────────


@pytest.mark.asyncio
async def test_check_state_rejects_invalid(admin_conn, service_conn):
    seed = await seed_order_prereqs(admin_conn, service_conn)
    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
            "state": "paid",  # Not in CHECK: open, OPEN, locked
        }
    )
    with pytest.raises(Exception) as exc:
        async with service_conn.transaction():
            await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
            await insert_order(service_conn, order)
    assert "orders_state_valid" in str(exc.value)


@pytest.mark.asyncio
async def test_check_payment_state_rejects_invalid(admin_conn, service_conn):
    seed = await seed_order_prereqs(admin_conn, service_conn)
    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
            "payment_state": "CANCELLED",  # Not in CHECK
        }
    )
    with pytest.raises(Exception) as exc:
        async with service_conn.transaction():
            await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
            await insert_order(service_conn, order)
    assert "orders_payment_state_valid" in str(exc.value)


@pytest.mark.asyncio
async def test_check_refunded_quantity_lte_quantity(admin_conn, service_conn):
    """refunded_quantity cannot exceed quantity."""
    seed = await seed_order_prereqs(admin_conn, service_conn)
    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_order(service_conn, order)

    sli = make_sli_row(
        {
            "tenant_id": seed["tenant_id"],
            "order_id": order["id"],
            "quantity": 2,
            "refunded_quantity": 5,  # > quantity → CHECK violation
        }
    )
    with pytest.raises(Exception) as exc:
        async with service_conn.transaction():
            await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
            await insert_sli(service_conn, sli)
    assert "sli_refund_max" in str(exc.value)


@pytest.mark.asyncio
async def test_check_refunded_quantity_nonneg(admin_conn, service_conn):
    """refunded_quantity cannot be negative."""
    seed = await seed_order_prereqs(admin_conn, service_conn)
    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_order(service_conn, order)

    sli = make_sli_row(
        {
            "tenant_id": seed["tenant_id"],
            "order_id": order["id"],
            "refunded_quantity": -1,
        }
    )
    with pytest.raises(Exception) as exc:
        async with service_conn.transaction():
            await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
            await insert_sli(service_conn, sli)
    assert "sli_refund_nonneg" in str(exc.value)


@pytest.mark.asyncio
async def test_check_clock_order(admin_conn, service_conn):
    """processed_at must be >= closed_at (or closed_at NULL).

    Tests by explicitly setting both values in a single UPDATE
    where processed_at < closed_at.
    """
    seed = await seed_order_prereqs(admin_conn, service_conn)
    now = datetime.now(UTC)

    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
            "closed_at": None,
            "processed_at": now,
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_order(service_conn, order)

    with pytest.raises(Exception) as exc:
        async with service_conn.transaction():
            await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
            await service_conn.execute(
                "UPDATE orders SET closed_at = $1, processed_at = $2 WHERE id = $3",
                now + timedelta(hours=2),  # closed_at in future
                now,  # processed_at < closed_at
                order["id"],
            )
    assert "orders_clock_order" in str(exc.value)


# ── Test 11: payment_state and state Are Separate Columns ────────────────────


@pytest.mark.asyncio
async def test_payment_state_and_state_separate(admin_conn):
    """Both columns exist independently — they were conflated in early spec versions."""
    for col in ["state", "payment_state", "payment_state_previous"]:
        exists = await admin_conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'orders' AND column_name = $1
            )
        """,
            col,
        )
        assert exists is True, f"Column {col} missing from orders table"


# ── Test 12: service_worker Can UPDATE Orders (Sprint 6 Readiness) ────────────


@pytest.mark.asyncio
async def test_service_worker_can_update_orders(admin_conn, service_conn):
    """service_worker needs UPDATE for Sprint 6 refund processing."""
    seed = await seed_order_prereqs(admin_conn, service_conn)
    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
            "payment_state": "PAID",
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_order(service_conn, order)

    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await service_conn.execute(
            "UPDATE orders SET payment_state = 'REFUNDED', "
            "payment_state_previous = 'PAID' WHERE id = $1",
            order["id"],
        )

    result = await admin_conn.fetchrow(
        "SELECT payment_state, payment_state_previous FROM orders WHERE id = $1",
        order["id"],
    )
    assert result["payment_state"] == "REFUNDED"
    assert result["payment_state_previous"] == "PAID"


# ── Test 13: app_user Cannot INSERT or service_worker Cannot DELETE ───────────


@pytest.mark.asyncio
async def test_app_user_cannot_insert_orders(admin_conn, app_conn, service_conn):
    seed = await seed_order_prereqs(admin_conn, service_conn)
    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
        }
    )

    with pytest.raises(Exception) as exc:
        async with app_conn.transaction():
            await app_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
            await insert_order(app_conn, order)
    assert "permission denied" in str(exc.value).lower() or "insufficient" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_service_worker_cannot_delete_orders(admin_conn, service_conn):
    seed = await seed_order_prereqs(admin_conn, service_conn)
    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_order(service_conn, order)

    with pytest.raises(Exception) as exc:
        async with service_conn.transaction():
            await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
            await service_conn.execute(
                "DELETE FROM orders WHERE id = $1",
                order["id"],
            )
    assert "permission denied" in str(exc.value).lower() or "insufficient" in str(exc.value).lower()


# ── Test 14: RLS — app_user Sees Own Tenant Only ─────────────────────────────


@pytest.mark.asyncio
async def test_rls_app_user_tenant_isolation(admin_conn, app_conn, service_conn):
    seed_a = await seed_order_prereqs(admin_conn, service_conn)
    seed_b = await seed_order_prereqs(admin_conn, service_conn)

    order_a = make_order_row(
        {
            "tenant_id": seed_a["tenant_id"],
            "pos_event_inbox_id": seed_a["inbox_id"],
        }
    )
    order_b = make_order_row(
        {
            "tenant_id": seed_b["tenant_id"],
            "pos_event_inbox_id": seed_b["inbox_id"],
        }
    )

    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed_a['tenant_id']}'")
        await insert_order(service_conn, order_a)
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed_b['tenant_id']}'")
        await insert_order(service_conn, order_b)

    async with app_conn.transaction():
        await app_conn.execute(f"SET LOCAL app.tenant_id = '{seed_a['tenant_id']}'")
        rows = await app_conn.fetch("SELECT id FROM orders")
        ids = {str(r["id"]) for r in rows}
        assert order_a["id"] in ids
        assert order_b["id"] not in ids


# ── Test 15: Partial Indexes Exist ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_partial_index_active_sales(admin_conn):
    idx = await admin_conn.fetchval("""
        SELECT indexdef FROM pg_indexes
        WHERE indexname = 'idx_sli_active_sales'
    """)
    assert idx is not None, "idx_sli_active_sales does not exist"
    assert "NOT is_refunded" in idx
    assert "NOT is_voided" in idx


@pytest.mark.asyncio
async def test_partial_index_payment_state_open(admin_conn):
    idx = await admin_conn.fetchval("""
        SELECT indexdef FROM pg_indexes
        WHERE indexname = 'idx_orders_payment_state_open'
    """)
    assert idx is not None, "idx_orders_payment_state_open does not exist"
    assert "OPEN" in idx


# ── Test 16: Active Sales Query Excludes Refunded/Voided ─────────────────────


@pytest.mark.asyncio
async def test_active_sales_excludes_refunded_voided(admin_conn, service_conn):
    """The partial index predicate matches the Sprint 5 depletion query pattern."""
    seed = await seed_order_prereqs(admin_conn, service_conn)
    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_order(service_conn, order)

    sli_normal = make_sli_row(
        {
            "tenant_id": seed["tenant_id"],
            "order_id": order["id"],
            "name_at_sale": "Active Item",
            "is_refunded": False,
            "is_voided": False,
        }
    )
    sli_refunded = make_sli_row(
        {
            "tenant_id": seed["tenant_id"],
            "order_id": order["id"],
            "name_at_sale": "Refunded Item",
            "quantity": 2,
            "refunded_quantity": 2,
            "is_refunded": True,
            "is_voided": False,
        }
    )
    sli_voided = make_sli_row(
        {
            "tenant_id": seed["tenant_id"],
            "order_id": order["id"],
            "name_at_sale": "Voided Item",
            "is_refunded": False,
            "is_voided": True,
        }
    )

    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_sli(service_conn, sli_normal)
        await insert_sli(service_conn, sli_refunded)
        await insert_sli(service_conn, sli_voided)

    active = await admin_conn.fetch(
        "SELECT id FROM sale_line_items WHERE tenant_id = $1 AND NOT is_refunded AND NOT is_voided",
        seed["tenant_id"],
    )
    active_ids = {str(r["id"]) for r in active}
    assert sli_normal["id"] in active_ids
    assert sli_refunded["id"] not in active_ids
    assert sli_voided["id"] not in active_ids


# ── Test 17: UUIDv7 Primary Keys ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uuidv7_pks(admin_conn, service_conn):
    seed = await seed_order_prereqs(admin_conn, service_conn)
    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_order(service_conn, order)

    sli = make_sli_row(
        {
            "tenant_id": seed["tenant_id"],
            "order_id": order["id"],
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_sli(service_conn, sli)

    # Version nibble at index 14 must be '7'
    assert order["id"][14] == "7"
    assert sli["id"][14] == "7"

    stored_order = await admin_conn.fetchval(
        "SELECT id::text FROM orders WHERE id = $1::uuid",
        order["id"],
    )
    stored_sli = await admin_conn.fetchval(
        "SELECT id::text FROM sale_line_items WHERE id = $1::uuid",
        sli["id"],
    )
    assert stored_order == order["id"]
    assert stored_sli == sli["id"]


# ── Test 18: Full Lifecycle Integration ──────────────────────────────────────


@pytest.mark.asyncio
async def test_full_order_lifecycle(admin_conn, app_conn, service_conn):
    """Complete order lifecycle covering 8 critical paths."""
    seed = await seed_order_prereqs(admin_conn, service_conn)
    tid = seed["tenant_id"]
    clover_oid = f"LIFE-{uuid.uuid4().hex[:8]}"

    # 1. CREATE ORDER
    order = make_order_row(
        {
            "tenant_id": tid,
            "pos_event_inbox_id": seed["inbox_id"],
            "clover_order_id": clover_oid,
            "payment_state": "PAID",
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{tid}'")
        await insert_order(service_conn, order)

    # 2. LINE ITEMS: normal + refunded
    sli_active = make_sli_row(
        {
            "tenant_id": tid,
            "order_id": order["id"],
            "name_at_sale": "Shawarma",
            "quantity": 2,
            "price_cents_at_sale": 1500,
            "net_revenue_cents": 3000,
        }
    )
    sli_refunded = make_sli_row(
        {
            "tenant_id": tid,
            "order_id": order["id"],
            "name_at_sale": "Falafel",
            "quantity": 2,
            "price_cents_at_sale": 1000,
            "net_revenue_cents": 2000,
            "is_refunded": True,
            "refunded_quantity": 1,
            "refunded_amount_cents": 1000,
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{tid}'")
        await insert_sli(service_conn, sli_active)
        await insert_sli(service_conn, sli_refunded)

    # 3. DUPLICATE ORDER → no-op
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{tid}'")
        await service_conn.execute(
            """
            INSERT INTO orders (id, tenant_id, pos_event_inbox_id, clover_order_id,
                total_amount_cents, state, payment_state, processed_at)
            VALUES ($1,$2,$3,$4,0,'locked','OPEN',now())
            ON CONFLICT ON CONSTRAINT uq_orders_clover DO NOTHING
        """,
            str(uuid7()),
            tid,
            seed["inbox_id"],
            clover_oid,
        )
    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE tenant_id=$1 AND clover_order_id=$2",
            tid,
            clover_oid,
        )
        == 1
    )

    # 4. DUPLICATE LINE ITEM → no-op
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{tid}'")
        await service_conn.execute(
            """
            INSERT INTO sale_line_items (id, tenant_id, order_id, clover_line_item_id,
                name_at_sale, quantity, price_cents_at_sale, net_revenue_cents)
            VALUES ($1,$2,$3,$4,'X',1,0,0)
            ON CONFLICT ON CONSTRAINT uq_sli_clover DO NOTHING
        """,
            str(uuid7()),
            tid,
            order["id"],
            sli_active["clover_line_item_id"],
        )
    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM sale_line_items WHERE clover_line_item_id=$1",
            sli_active["clover_line_item_id"],
        )
        == 1
    )

    # 5. app_user reads own tenant
    async with app_conn.transaction():
        await app_conn.execute(f"SET LOCAL app.tenant_id = '{tid}'")
        rows = await app_conn.fetch("SELECT id FROM orders")
        assert any(str(r["id"]) == order["id"] for r in rows)

    # 6. service_worker UPDATE (Sprint 6 refund path)
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{tid}'")
        await service_conn.execute(
            "UPDATE orders SET payment_state = 'REFUNDED', "
            "payment_state_previous = 'PAID' WHERE id = $1",
            order["id"],
        )
    ps = await admin_conn.fetchval(
        "SELECT payment_state FROM orders WHERE id = $1",
        order["id"],
    )
    assert ps == "REFUNDED"

    # 7. service_worker CANNOT DELETE
    with pytest.raises(Exception) as exc:
        async with service_conn.transaction():
            await service_conn.execute(f"SET LOCAL app.tenant_id = '{tid}'")
            await service_conn.execute(
                "DELETE FROM orders WHERE id = $1",
                order["id"],
            )
    assert "permission denied" in str(exc.value).lower() or "insufficient" in str(exc.value).lower()

    # 8. ACTIVE SALES excludes refunded
    active = await admin_conn.fetch(
        "SELECT id FROM sale_line_items WHERE tenant_id=$1 AND NOT is_refunded AND NOT is_voided",
        tid,
    )
    active_ids = {str(r["id"]) for r in active}
    assert sli_active["id"] in active_ids
    assert sli_refunded["id"] not in active_ids


# ── Test 19: app_user Cannot INSERT sale_line_items ──────────────────────────


@pytest.mark.asyncio
async def test_app_user_cannot_insert_line_items(admin_conn, app_conn, service_conn):
    """app_user has SELECT only on sale_line_items.

    If INSERT were allowed, a compromised frontend session could inject fake
    line items, inflating sales data and triggering incorrect inventory
    depletion in Sprint 5.
    """
    seed = await seed_order_prereqs(admin_conn, service_conn)
    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_order(service_conn, order)

    sli = make_sli_row(
        {
            "tenant_id": seed["tenant_id"],
            "order_id": order["id"],
        }
    )
    with pytest.raises(Exception) as exc:
        async with app_conn.transaction():
            await app_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
            await insert_sli(app_conn, sli)
    assert "permission denied" in str(exc.value).lower() or "insufficient" in str(exc.value).lower()


# ── Test 20: RLS Isolation on sale_line_items ─────────────────────────────────


@pytest.mark.asyncio
async def test_rls_app_user_line_item_isolation(admin_conn, app_conn, service_conn):
    """Tenant-A must not see tenant-B's line items."""
    seed_a = await seed_order_prereqs(admin_conn, service_conn)
    seed_b = await seed_order_prereqs(admin_conn, service_conn)

    order_a = make_order_row(
        {
            "tenant_id": seed_a["tenant_id"],
            "pos_event_inbox_id": seed_a["inbox_id"],
        }
    )
    order_b = make_order_row(
        {
            "tenant_id": seed_b["tenant_id"],
            "pos_event_inbox_id": seed_b["inbox_id"],
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed_a['tenant_id']}'")
        await insert_order(service_conn, order_a)
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed_b['tenant_id']}'")
        await insert_order(service_conn, order_b)

    sli_a = make_sli_row(
        {
            "tenant_id": seed_a["tenant_id"],
            "order_id": order_a["id"],
            "name_at_sale": "Tenant A Item",
        }
    )
    sli_b = make_sli_row(
        {
            "tenant_id": seed_b["tenant_id"],
            "order_id": order_b["id"],
            "name_at_sale": "Tenant B Item",
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed_a['tenant_id']}'")
        await insert_sli(service_conn, sli_a)
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed_b['tenant_id']}'")
        await insert_sli(service_conn, sli_b)

    async with app_conn.transaction():
        await app_conn.execute(f"SET LOCAL app.tenant_id = '{seed_a['tenant_id']}'")
        rows = await app_conn.fetch("SELECT id FROM sale_line_items")
        ids = {str(r["id"]) for r in rows}
        assert sli_a["id"] in ids
        assert sli_b["id"] not in ids


# ── Test 21: service_worker Cannot UPDATE sale_line_items ─────────────────────


@pytest.mark.asyncio
async def test_service_worker_cannot_update_line_items(admin_conn, service_conn):
    """service_worker has SELECT + INSERT on sale_line_items, not UPDATE.

    Line items are immutable after creation — price_cents_at_sale,
    name_at_sale, and net_revenue_cents are historical snapshots.
    Sprint 6 adds UPDATE grant when refund logic is implemented.
    """
    seed = await seed_order_prereqs(admin_conn, service_conn)
    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
        }
    )
    sli = make_sli_row(
        {
            "tenant_id": seed["tenant_id"],
            "order_id": order["id"],
            "name_at_sale": "Original Name",
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_order(service_conn, order)
        await insert_sli(service_conn, sli)

    with pytest.raises(Exception) as exc:
        async with service_conn.transaction():
            await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
            await service_conn.execute(
                "UPDATE sale_line_items SET name_at_sale = 'Tampered' WHERE id = $1",
                sli["id"],
            )
    assert "permission denied" in str(exc.value).lower() or "insufficient" in str(exc.value).lower()


# ── Test 22: app_user Cannot DELETE Either Table ─────────────────────────────


@pytest.mark.asyncio
async def test_app_user_cannot_delete(admin_conn, app_conn, service_conn):
    """app_user has SELECT only — no DELETE on orders or sale_line_items."""
    seed = await seed_order_prereqs(admin_conn, service_conn)
    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
        }
    )
    sli = make_sli_row(
        {
            "tenant_id": seed["tenant_id"],
            "order_id": order["id"],
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_order(service_conn, order)
        await insert_sli(service_conn, sli)

    with pytest.raises(Exception) as exc_o:
        async with app_conn.transaction():
            await app_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
            await app_conn.execute(
                "DELETE FROM orders WHERE id = $1",
                order["id"],
            )
    assert (
        "permission denied" in str(exc_o.value).lower()
        or "insufficient" in str(exc_o.value).lower()
    )

    with pytest.raises(Exception) as exc_s:
        async with app_conn.transaction():
            await app_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
            await app_conn.execute(
                "DELETE FROM sale_line_items WHERE id = $1",
                sli["id"],
            )
    assert (
        "permission denied" in str(exc_s.value).lower()
        or "insufficient" in str(exc_s.value).lower()
    )


# ── Test 23: menu_item_id FK Works When Populated ────────────────────────────


@pytest.mark.asyncio
async def test_menu_item_fk_works(admin_conn, service_conn):
    """menu_item_id FK to menu_items must work when populated in Sprint 5.

    Also verifies a bad FK is rejected.
    """
    seed = await seed_order_prereqs(admin_conn, service_conn)

    menu_item_id = str(uuid7())
    await admin_conn.execute(
        "INSERT INTO menu_items (id, tenant_id, name) VALUES ($1, $2, $3)",
        menu_item_id,
        seed["tenant_id"],
        "Latte",
    )

    order = make_order_row(
        {
            "tenant_id": seed["tenant_id"],
            "pos_event_inbox_id": seed["inbox_id"],
        }
    )
    sli = make_sli_row(
        {
            "tenant_id": seed["tenant_id"],
            "order_id": order["id"],
            "menu_item_id": menu_item_id,
            "name_at_sale": "Latte",
        }
    )
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        await insert_order(service_conn, order)
        await insert_sli(service_conn, sli)

    result = await admin_conn.fetchrow(
        """
        SELECT sli.name_at_sale, mi.name AS menu_name
        FROM sale_line_items sli
        JOIN menu_items mi ON mi.id = sli.menu_item_id
        WHERE sli.id = $1
    """,
        sli["id"],
    )
    assert result["name_at_sale"] == "Latte"
    assert result["menu_name"] == "Latte"

    bad_sli = make_sli_row(
        {
            "tenant_id": seed["tenant_id"],
            "order_id": order["id"],
            "menu_item_id": str(uuid7()),  # Non-existent menu item
            "name_at_sale": "Ghost Item",
        }
    )
    with pytest.raises(Exception) as exc:
        async with service_conn.transaction():
            await service_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
            await insert_sli(service_conn, bad_sli)
    assert "menu_items" in str(exc.value).lower() or "foreign key" in str(exc.value).lower()
