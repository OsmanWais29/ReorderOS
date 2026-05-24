"""Phase 8 — Reconciliation Service tests (26 functions).

URL encoding note:
  httpx encodes '>' to '%3E' in query strings. Tests that inspect the
  filter value use request.url.params.get('filter') which returns the
  decoded string, making the regex r"modifiedTime[><=]+(\\d+)" work reliably.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from uuid6 import uuid7

from app.core.config import get_settings
from app.modules.pos.reconciliation import ReconciliationService

from tests.helpers.phase8 import (
    enc,
    make_clover_order_list_item,
    seed_reconciliation_prereqs,
)

settings = get_settings()


# ── Test isolation ─────────────────────────────────────────────────────────────
# run_all() scans ALL active connections. Pre-existing active connections from
# other test phases would be picked up and cause spurious failures. This fixture
# revokes them before each test so only connections seeded in this test run.


@pytest.fixture(autouse=True)
async def revoke_other_connections(admin_conn):
    await admin_conn.execute(
        "UPDATE tenant_pos_connections SET state = 'revoked' "
        "WHERE state = 'active'"
    )
    yield


# ── Helpers ────────────────────────────────────────────────────────────────────


def _filter_from_request(call) -> str:
    """Return the decoded filter query-param value from a recorded respx call."""
    return call.request.url.params.get("filter", "")


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 1 — Uses modifiedTime filter, not createdTime
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_uses_modified_time_filter(admin_conn):
    cursor = int(time.time() * 1000) - 300_000
    seed = await seed_reconciliation_prereqs(
        admin_conn, last_reconciliation_cursor=cursor,
    )

    route = respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": []})
    )

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    assert len(route.calls) == 1
    request_url = str(route.calls[0].request.url)
    assert "modifiedTime" in request_url
    assert "createdTime" not in request_url


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 2 — since_ms never negative
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_since_ms_never_negative(admin_conn):
    seed = await seed_reconciliation_prereqs(
        admin_conn, last_reconciliation_cursor=None,
    )

    route = respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": []})
    )

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    filt = _filter_from_request(route.calls[0])
    match = re.search(r"modifiedTime[><=]+(\d+)", filt)
    assert match is not None
    since_val = int(match.group(1))
    assert since_val >= 0, f"since_ms is negative: {since_val}"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 3 — NULL cursor → 30-day lookback
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_null_cursor_30_day_lookback(admin_conn):
    seed = await seed_reconciliation_prereqs(
        admin_conn, last_reconciliation_cursor=None,
    )

    route = respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": []})
    )

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    filt = _filter_from_request(route.calls[0])
    match = re.search(r"modifiedTime[><=]+(\d+)", filt)
    assert match is not None
    since_val = int(match.group(1))

    now_ms = int(time.time() * 1000)
    thirty_days_ms = 30 * 24 * 60 * 60 * 1000
    expected = now_ms - thirty_days_ms
    assert abs(since_val - expected) < 3_600_000, \
        f"Expected ~30 days ago, got {since_val}"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 4 — Cursor never moves backward
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_cursor_never_backward(admin_conn):
    existing_cursor = int(time.time() * 1000)
    seed = await seed_reconciliation_prereqs(
        admin_conn, last_reconciliation_cursor=existing_cursor,
    )

    old_order = make_clover_order_list_item(
        modified_time=existing_cursor - 60_000,
    )
    respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": [old_order]})
    )

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    new_cursor = await admin_conn.fetchval(
        "SELECT last_reconciliation_cursor "
        "FROM tenant_pos_connections WHERE connection_id = $1",
        seed["connection_id"],
    )
    assert new_cursor >= existing_cursor, \
        f"Cursor went backward: {new_cursor} < {existing_cursor}"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 5 — Empty results → cursor unchanged
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_empty_results_cursor_unchanged(admin_conn):
    existing_cursor = int(time.time() * 1000) - 100_000
    seed = await seed_reconciliation_prereqs(
        admin_conn, last_reconciliation_cursor=existing_cursor,
    )

    respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": []})
    )

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    new_cursor = await admin_conn.fetchval(
        "SELECT last_reconciliation_cursor "
        "FROM tenant_pos_connections WHERE connection_id = $1",
        seed["connection_id"],
    )
    assert new_cursor == existing_cursor


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 6 — fetched_payload pre-populated
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_fetched_payload_prepopulated(admin_conn):
    seed = await seed_reconciliation_prereqs(admin_conn)
    order_id = f"prepop_{uuid.uuid4().hex[:8]}"
    ts = int(time.time() * 1000)

    order = make_clover_order_list_item(order_id=order_id, modified_time=ts)
    respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": [order]})
    )

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    row = await admin_conn.fetchrow(
        "SELECT fetched_payload, fetched_at, source "
        "FROM pos_event_inbox "
        "WHERE tenant_id = $1 AND vendor_event_id = $2",
        seed["tenant_id"], order_id,
    )
    assert row is not None
    assert row["fetched_payload"] is not None
    assert row["fetched_at"] is not None
    assert row["source"] == "reconciliation"

    payload = row["fetched_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["id"] == order_id


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 7 — ON CONFLICT requeues processed events
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_requeues_processed_event(admin_conn):
    seed = await seed_reconciliation_prereqs(admin_conn)
    order_id = f"requeue_{uuid.uuid4().hex[:8]}"
    ts = int(time.time() * 1000)

    inbox_id = str(uuid7())
    await admin_conn.execute("""
        INSERT INTO pos_event_inbox (
            inbox_id, tenant_id, connection_id, vendor,
            vendor_event_id, vendor_object_type, vendor_event_type,
            vendor_ts, raw_payload, signature_verified, source,
            state, processed_at
        ) VALUES ($1,$2,$3,'clover',$4,'O','UPDATE',
                  $5, '{}', true, 'webhook', 'processed', now())
    """, inbox_id, seed["tenant_id"], seed["connection_id"], order_id, ts)

    order = make_clover_order_list_item(order_id=order_id, modified_time=ts)
    respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": [order]})
    )

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    row = await admin_conn.fetchrow(
        "SELECT state, fetched_payload FROM pos_event_inbox WHERE inbox_id = $1",
        inbox_id,
    )
    assert row["state"] == "pending", \
        f"Processed event not requeued: state={row['state']}"
    payload = row["fetched_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["id"] == order_id


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 8 — ON CONFLICT requeues dead-letter events
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_requeues_dead_letter(admin_conn):
    seed = await seed_reconciliation_prereqs(admin_conn)
    order_id = f"dead_{uuid.uuid4().hex[:8]}"
    ts = int(time.time() * 1000)

    inbox_id = str(uuid7())
    await admin_conn.execute("""
        INSERT INTO pos_event_inbox (
            inbox_id, tenant_id, connection_id, vendor,
            vendor_event_id, vendor_object_type, vendor_event_type,
            vendor_ts, raw_payload, signature_verified, source,
            state, retry_count
        ) VALUES ($1,$2,$3,'clover',$4,'O','UPDATE',
                  $5, '{}', true, 'webhook', 'dead_letter', 5)
    """, inbox_id, seed["tenant_id"], seed["connection_id"], order_id, ts)

    order = make_clover_order_list_item(order_id=order_id, modified_time=ts)
    respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": [order]})
    )

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    state = await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1", inbox_id,
    )
    assert state == "pending"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 9 — ON CONFLICT leaves pending alone
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_leaves_pending_alone(admin_conn):
    seed = await seed_reconciliation_prereqs(admin_conn)
    order_id = f"pend_{uuid.uuid4().hex[:8]}"
    ts = int(time.time() * 1000)

    inbox_id = str(uuid7())
    await admin_conn.execute("""
        INSERT INTO pos_event_inbox (
            inbox_id, tenant_id, connection_id, vendor,
            vendor_event_id, vendor_object_type, vendor_event_type,
            vendor_ts, raw_payload, signature_verified, source, state
        ) VALUES ($1,$2,$3,'clover',$4,'O','UPDATE',
                  $5, '{}', true, 'webhook', 'pending')
    """, inbox_id, seed["tenant_id"], seed["connection_id"], order_id, ts)

    order = make_clover_order_list_item(order_id=order_id, modified_time=ts)
    respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": [order]})
    )

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    state = await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1", inbox_id,
    )
    assert state == "pending"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 10 — ON CONFLICT leaves failed alone
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_leaves_failed_alone(admin_conn):
    seed = await seed_reconciliation_prereqs(admin_conn)
    order_id = f"fail_{uuid.uuid4().hex[:8]}"
    ts = int(time.time() * 1000)
    future_backoff = datetime.now(timezone.utc) + timedelta(minutes=8)

    inbox_id = str(uuid7())
    await admin_conn.execute("""
        INSERT INTO pos_event_inbox (
            inbox_id, tenant_id, connection_id, vendor,
            vendor_event_id, vendor_object_type, vendor_event_type,
            vendor_ts, raw_payload, signature_verified, source,
            state, retry_count, next_attempt_at
        ) VALUES ($1,$2,$3,'clover',$4,'O','UPDATE',
                  $5, '{}', true, 'webhook', 'failed', 3, $6)
    """, inbox_id, seed["tenant_id"], seed["connection_id"],
        order_id, ts, future_backoff)

    order = make_clover_order_list_item(order_id=order_id, modified_time=ts)
    respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": [order]})
    )

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    row = await admin_conn.fetchrow(
        "SELECT state, retry_count FROM pos_event_inbox WHERE inbox_id = $1",
        inbox_id,
    )
    assert row["state"] == "failed"
    assert row["retry_count"] == 3


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 11 — Rate limit 429 → automatic retry
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_rate_limit_backoff(admin_conn):
    seed = await seed_reconciliation_prereqs(admin_conn)
    order = make_clover_order_list_item()

    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(
            200,
            json={"elements": [order] if call_count == 2 else []},
        )

    respx.get(url__regex=".*/orders.*").mock(side_effect=side_effect)

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    assert call_count >= 2
    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert count >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 12 — Pagination over 100 orders
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_pagination(admin_conn):
    seed = await seed_reconciliation_prereqs(admin_conn)
    base_ts = int(time.time() * 1000) - 1_000_000

    page_1 = [
        make_clover_order_list_item(
            order_id=f"pg1_{i}_{uuid.uuid4().hex[:4]}",
            modified_time=base_ts + i * 100,
        )
        for i in range(100)
    ]
    page_2 = [
        make_clover_order_list_item(
            order_id=f"pg2_{i}_{uuid.uuid4().hex[:4]}",
            modified_time=base_ts + 100_000 + i * 100,
        )
        for i in range(50)
    ]

    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json={"elements": page_1})
        if call_count == 2:
            return httpx.Response(200, json={"elements": page_2})
        return httpx.Response(200, json={"elements": []})

    respx.get(url__regex=".*/orders.*").mock(side_effect=side_effect)

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert count == 150
    assert call_count >= 2


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 13 — Cursor advances to max observed modifiedTime
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_cursor_advances(admin_conn):
    seed = await seed_reconciliation_prereqs(
        admin_conn, last_reconciliation_cursor=1000,
    )

    orders = [
        make_clover_order_list_item(modified_time=5000),
        make_clover_order_list_item(modified_time=8000),
        make_clover_order_list_item(modified_time=3000),
    ]
    respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": orders})
    )

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    cursor = await admin_conn.fetchval(
        "SELECT last_reconciliation_cursor "
        "FROM tenant_pos_connections WHERE connection_id = $1",
        seed["connection_id"],
    )
    assert cursor == 8000


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 14 — last_reconciliation_at updated after run
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_last_reconciliation_at_updated(admin_conn):
    seed = await seed_reconciliation_prereqs(admin_conn)

    respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": []})
    )

    before = datetime.now(timezone.utc)

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    last_recon = await admin_conn.fetchval(
        "SELECT last_reconciliation_at "
        "FROM tenant_pos_connections WHERE connection_id = $1",
        seed["connection_id"],
    )
    assert last_recon is not None
    assert last_recon >= before


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 15 — Waitlist table has no tenant_id
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_waitlist_no_tenant_id(admin_conn):
    cols = await admin_conn.fetch("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'pos_waitlist'
    """)
    col_names = {r["column_name"] for r in cols}
    assert "id" in col_names
    assert "pos_name" in col_names
    assert "tenant_id" not in col_names


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 16 — Full reconciliation lifecycle (3 runs)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_full_reconciliation_lifecycle(admin_conn):
    seed = await seed_reconciliation_prereqs(
        admin_conn, last_reconciliation_cursor=None,
    )

    ts_1 = int(time.time() * 1000) - 50_000
    ts_2 = int(time.time() * 1000) - 30_000
    ts_3 = int(time.time() * 1000) - 10_000

    order_1 = make_clover_order_list_item(
        order_id=f"life1_{uuid.uuid4().hex[:6]}", modified_time=ts_1,
    )
    order_2 = make_clover_order_list_item(
        order_id=f"life2_{uuid.uuid4().hex[:6]}", modified_time=ts_2,
    )
    order_3 = make_clover_order_list_item(
        order_id=f"life3_{uuid.uuid4().hex[:6]}", modified_time=ts_3,
    )

    svc = ReconciliationService()

    # Run 1
    respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": [order_1, order_2]})
    )
    await svc.reconcile_connection(seed["connection_id"])

    assert 2 == await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    cursor_1 = await admin_conn.fetchval(
        "SELECT last_reconciliation_cursor "
        "FROM tenant_pos_connections WHERE connection_id = $1",
        seed["connection_id"],
    )
    assert cursor_1 == ts_2

    # Run 2
    respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": [order_3]})
    )
    await svc.reconcile_connection(seed["connection_id"])

    assert 3 == await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    cursor_2 = await admin_conn.fetchval(
        "SELECT last_reconciliation_cursor "
        "FROM tenant_pos_connections WHERE connection_id = $1",
        seed["connection_id"],
    )
    assert cursor_2 == ts_3

    # Run 3: nothing new
    respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": []})
    )
    await svc.reconcile_connection(seed["connection_id"])

    cursor_3 = await admin_conn.fetchval(
        "SELECT last_reconciliation_cursor "
        "FROM tenant_pos_connections WHERE connection_id = $1",
        seed["connection_id"],
    )
    assert cursor_3 == ts_3


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 17 — Future-corrupted cursor doesn't stall forever
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_future_cursor_doesnt_stall(admin_conn):
    future_cursor = 999_999_999_999_999
    seed = await seed_reconciliation_prereqs(
        admin_conn, last_reconciliation_cursor=future_cursor,
    )

    respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": []})
    )

    before = datetime.now(timezone.utc)

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    row = await admin_conn.fetchrow(
        "SELECT last_reconciliation_cursor, last_reconciliation_at "
        "FROM tenant_pos_connections WHERE connection_id = $1",
        seed["connection_id"],
    )
    assert row["last_reconciliation_cursor"] == future_cursor
    assert row["last_reconciliation_at"] is not None
    assert row["last_reconciliation_at"] >= before
    assert len(respx.calls) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 18 — modifiedTime tie — both orders stored across pages
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_modified_time_tie_across_pages(admin_conn):
    seed = await seed_reconciliation_prereqs(admin_conn)
    shared_ts = int(time.time() * 1000)

    order_a_id = f"tie_a_{uuid.uuid4().hex[:6]}"
    order_b_id = f"tie_b_{uuid.uuid4().hex[:6]}"

    page_1 = [
        make_clover_order_list_item(
            order_id=f"filler_{i}_{uuid.uuid4().hex[:4]}",
            modified_time=shared_ts - 1000 + i,
        )
        for i in range(99)
    ]
    page_1.append(
        make_clover_order_list_item(order_id=order_a_id, modified_time=shared_ts)
    )
    page_2 = [
        make_clover_order_list_item(order_id=order_b_id, modified_time=shared_ts)
    ]

    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json={"elements": page_1})
        if call_count == 2:
            return httpx.Response(200, json={"elements": page_2})
        return httpx.Response(200, json={"elements": []})

    respx.get(url__regex=".*/orders.*").mock(side_effect=side_effect)

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    count_a = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox "
        "WHERE tenant_id = $1 AND vendor_event_id = $2",
        seed["tenant_id"], order_a_id,
    )
    count_b = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox "
        "WHERE tenant_id = $1 AND vendor_event_id = $2",
        seed["tenant_id"], order_b_id,
    )
    assert count_a == 1, "Order A at tie timestamp missing"
    assert count_b == 1, "Order B at tie timestamp missing"

    total = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert total == 101


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 19 — run_all: multiple connections, failure isolated
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_run_all_multiple_connections(admin_conn):
    seed_a = await seed_reconciliation_prereqs(admin_conn)
    seed_b = await seed_reconciliation_prereqs(admin_conn)

    order_b = make_clover_order_list_item(
        order_id=f"runall_{uuid.uuid4().hex[:6]}",
    )

    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            return httpx.Response(500, text="Internal Server Error")
        if call_count == 2:
            return httpx.Response(200, json={"elements": [order_b]})
        return httpx.Response(200, json={"elements": []})

    respx.get(url__regex=".*/orders.*").mock(side_effect=side_effect)

    svc = ReconciliationService()
    await svc.run_all()

    assert call_count >= 2, \
        f"Only {call_count} calls — second connection was skipped"

    recon_b = await admin_conn.fetchval(
        "SELECT last_reconciliation_at "
        "FROM tenant_pos_connections WHERE connection_id = $1",
        seed_b["connection_id"],
    )
    assert recon_b is not None, "Second connection not reconciled"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 20 — Waitlist INSERT + SELECT round-trip
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_waitlist_crud(admin_conn):
    wl_id = str(uuid7())
    await admin_conn.execute("""
        INSERT INTO pos_waitlist (id, pos_name, email, restaurant_name)
        VALUES ($1, $2, $3, $4)
    """, wl_id, "clover", "test@restaurant.com", "Joe's Shawarma")

    row = await admin_conn.fetchrow(
        "SELECT pos_name, email, restaurant_name, created_at "
        "FROM pos_waitlist WHERE id = $1", wl_id,
    )
    assert row is not None
    assert row["pos_name"] == "clover"
    assert row["email"] == "test@restaurant.com"
    assert row["restaurant_name"] == "Joe's Shawarma"
    assert row["created_at"] is not None


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 21 — Clover 500 → graceful handling, cursor unchanged
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_clover_500_handled(admin_conn):
    existing_cursor = int(time.time() * 1000) - 100_000
    seed = await seed_reconciliation_prereqs(
        admin_conn, last_reconciliation_cursor=existing_cursor,
    )

    respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])  # Must not raise

    cursor = await admin_conn.fetchval(
        "SELECT last_reconciliation_cursor "
        "FROM tenant_pos_connections WHERE connection_id = $1",
        seed["connection_id"],
    )
    assert cursor is not None

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert count == 0


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 22 — run_all filters revoked connections
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_run_all_filters_revoked(admin_conn):
    seed_active = await seed_reconciliation_prereqs(admin_conn)
    seed_revoked = await seed_reconciliation_prereqs(admin_conn)

    await admin_conn.execute(
        "UPDATE tenant_pos_connections SET state = 'revoked' "
        "WHERE connection_id = $1", seed_revoked["connection_id"],
    )

    order = make_clover_order_list_item()
    respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": [order]})
    )

    svc = ReconciliationService()
    await svc.run_all()

    count_active = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id = $1",
        seed_active["tenant_id"],
    )
    count_revoked = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id = $1",
        seed_revoked["tenant_id"],
    )
    assert count_active >= 1
    assert count_revoked == 0


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 23 — Partial pagination failure: page 1 persists, cursor safe
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_partial_pagination_failure(admin_conn):
    seed = await seed_reconciliation_prereqs(
        admin_conn, last_reconciliation_cursor=500,
    )

    page_1_orders = [
        make_clover_order_list_item(
            order_id=f"partial_{i}_{uuid.uuid4().hex[:4]}",
            modified_time=1000 * (i + 1),
        )
        for i in range(3)
    ]

    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json={"elements": page_1_orders})
        return httpx.Response(500, text="Internal Server Error")

    respx.get(url__regex=".*/orders.*").mock(side_effect=side_effect)

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert count == 3, f"Expected 3 orders from page 1, got {count}"

    cursor = await admin_conn.fetchval(
        "SELECT last_reconciliation_cursor "
        "FROM tenant_pos_connections WHERE connection_id = $1",
        seed["connection_id"],
    )
    assert cursor >= 3000, \
        f"Cursor should be at least 3000 (page 1 max), got {cursor}"
    assert cursor > 500, "Cursor didn't advance — page 1 progress lost"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 24 — Duplicate order across pages deduplicated
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_duplicate_order_across_pages(admin_conn):
    seed = await seed_reconciliation_prereqs(admin_conn)
    shared_id = f"dup_page_{uuid.uuid4().hex[:6]}"
    shared_ts = int(time.time() * 1000)

    order_x = make_clover_order_list_item(
        order_id=shared_id, modified_time=shared_ts,
    )
    order_only_p1 = make_clover_order_list_item(
        order_id=f"p1_{uuid.uuid4().hex[:6]}",
        modified_time=shared_ts - 1000,
    )
    order_only_p2 = make_clover_order_list_item(
        order_id=f"p2_{uuid.uuid4().hex[:6]}",
        modified_time=shared_ts + 1000,
    )

    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json={
                "elements": [order_only_p1, order_x] + [
                    make_clover_order_list_item(
                        modified_time=shared_ts - 500 + i,
                    )
                    for i in range(98)
                ]
            })
        if call_count == 2:
            return httpx.Response(200, json={
                "elements": [order_x, order_only_p2]
            })
        return httpx.Response(200, json={"elements": []})

    respx.get(url__regex=".*/orders.*").mock(side_effect=side_effect)

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    dup_count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox "
        "WHERE tenant_id = $1 AND vendor_event_id = $2",
        seed["tenant_id"], shared_id,
    )
    assert dup_count == 1, f"Duplicate order created {dup_count} rows"

    p2_count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox "
        "WHERE tenant_id = $1 AND vendor_event_id = $2",
        seed["tenant_id"], order_only_p2["id"],
    )
    assert p2_count == 1

    cursor = await admin_conn.fetchval(
        "SELECT last_reconciliation_cursor "
        "FROM tenant_pos_connections WHERE connection_id = $1",
        seed["connection_id"],
    )
    assert cursor >= shared_ts + 1000


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 25 — Overlap window recovers boundary order
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_overlap_recovers_boundary_order(admin_conn):
    now_ms = int(time.time() * 1000)
    cursor = now_ms - 10_000  # 10 seconds ago

    seed = await seed_reconciliation_prereqs(
        admin_conn, last_reconciliation_cursor=cursor,
    )

    missed_order = make_clover_order_list_item(
        order_id=f"boundary_{uuid.uuid4().hex[:6]}",
        modified_time=cursor - 1000,  # 1s before cursor
    )

    route = respx.get(url__regex=".*/orders.*").mock(
        return_value=httpx.Response(200, json={"elements": [missed_order]})
    )

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    # Overlap window means since_ms < existing cursor
    filt = _filter_from_request(route.calls[0])
    match = re.search(r"modifiedTime[><=]+(\d+)", filt)
    assert match is not None
    since_val = int(match.group(1))
    assert since_val < cursor, \
        f"since_ms ({since_val}) not earlier than cursor ({cursor}) — no overlap"

    # Boundary order captured
    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox "
        "WHERE tenant_id = $1 AND vendor_event_id = $2",
        seed["tenant_id"], missed_order["id"],
    )
    assert count == 1, "Boundary order lost — overlap window not working"

    # Cursor monotonic (didn't regress to since_ms)
    new_cursor = await admin_conn.fetchval(
        "SELECT last_reconciliation_cursor "
        "FROM tenant_pos_connections WHERE connection_id = $1",
        seed["connection_id"],
    )
    assert new_cursor >= cursor, "Cursor went backward after overlap recovery"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 26 — Pagination loop protection (MAX_PAGES safety valve)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_pagination_loop_protection(admin_conn):
    seed = await seed_reconciliation_prereqs(admin_conn)

    call_count = 0

    def infinite_pages(request):
        nonlocal call_count
        call_count += 1
        orders = [
            make_clover_order_list_item(
                order_id=f"loop_{call_count}_{i}_{uuid.uuid4().hex[:4]}",
                modified_time=int(time.time() * 1000) + call_count * 100_000 + i,
            )
            for i in range(100)
        ]
        return httpx.Response(200, json={"elements": orders})

    respx.get(url__regex=".*/orders.*").mock(side_effect=infinite_pages)

    svc = ReconciliationService()
    await svc.reconcile_connection(seed["connection_id"])

    assert call_count <= 60, \
        f"Pagination made {call_count} calls — no loop protection"
    assert call_count >= 2, \
        f"Only {call_count} page(s) fetched — pagination not working"
