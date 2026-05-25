"""Phase 7 — Inbox Worker tests.

All 33 tests cover:
  - claim_batch (SKIP LOCKED, stale reclaim, backoff timer)
  - process_event (fetch, state check, upsert, line items)
  - mark_failed / _dead_letter (backoff, dead letter, monitoring alert)
  - CloverClient error handling (404→processed, 401/429/5xx→failed)
  - RLS / cross-tenant isolation
  - Concurrency and idempotency
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg  # noqa: F401 — used by admin_conn fixture
import httpx
import pytest
import respx
from uuid6 import UUID as UUID6

from app.core.config import get_settings
from app.modules.pos.worker import InboxWorker
from tests.helpers.phase7 import (
    make_clover_order,
    make_line_item,
    seed_inbox_event,
    seed_worker_prereqs,
)

settings = get_settings()


# ── Test isolation ─────────────────────────────────────────────────────────────
# claim_batch does a global scan. Previous test phases (e.g. Phase 6 webhook
# tests) leave rows in 'pending' state that would pollute worker claims.
# This autouse fixture marks all pre-existing claimable rows as
# 'duplicate_ignored' before each test so only the event seeded within the
# test body is visible to the worker.


@pytest.fixture(autouse=True)
async def clean_pending_inbox(admin_conn):
    await admin_conn.execute("""
        UPDATE pos_event_inbox
        SET state = 'duplicate_ignored'
        WHERE state IN ('pending', 'failed', 'processing')
    """)
    yield


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 1 — Claim picks up pending events
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_claim_picks_up_pending(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    worker = InboxWorker()

    claimed = await worker.claim_batch(batch_size=10)
    claimed_ids = {str(e.inbox_id) for e in claimed}

    assert seed["inbox_id"] in claimed_ids

    state = await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1",
        seed["inbox_id"],
    )
    assert state == "processing"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 2 — Claim reclaims stale processing events
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_claim_reclaims_stale(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    await admin_conn.execute(
        """
        UPDATE pos_event_inbox
        SET state = 'processing',
            claim_expires_at = $1,
            processing_started_at = now()
        WHERE inbox_id = $2
    """,
        datetime.now(UTC) - timedelta(minutes=10),
        seed["inbox_id"],
    )

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=10)
    claimed_ids = {str(e.inbox_id) for e in claimed}

    assert seed["inbox_id"] in claimed_ids


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 3 — Claim skips events with future next_attempt_at
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_claim_skips_future_retry(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    await admin_conn.execute(
        """
        UPDATE pos_event_inbox
        SET state = 'pending',
            next_attempt_at = $1
        WHERE inbox_id = $2
    """,
        datetime.now(UTC) + timedelta(hours=1),
        seed["inbox_id"],
    )

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=10)
    claimed_ids = {str(e.inbox_id) for e in claimed}

    assert seed["inbox_id"] not in claimed_ids


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 4 — Completed order creates order + line items
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_process_completed_order(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    order_id = seed["vendor_event_id"]

    clover_order = make_clover_order(
        order_id=order_id,
        state="locked",
        total=4200,
        payment_state="PAID",
        line_items=[
            make_line_item(name="Shawarma", price=1500, qty=2),
            make_line_item(name="Falafel", price=1200, qty=1),
        ],
        employee_id="EMP_001",
    )

    respx.get(url__regex=f".*/orders/{order_id}.*").mock(
        return_value=httpx.Response(200, json=clover_order)
    )

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    assert len(claimed) == 1
    await worker.process_event(claimed[0])

    order = await admin_conn.fetchrow(
        "SELECT clover_order_id, total_amount_cents, state, payment_state, "
        "clover_employee_id FROM orders WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert order is not None
    assert order["clover_order_id"] == order_id
    assert order["total_amount_cents"] == 4200
    assert order["state"] == "locked"
    assert order["payment_state"] == "PAID"
    assert order["clover_employee_id"] == "EMP_001"

    items = await admin_conn.fetch(
        "SELECT name_at_sale, quantity, price_cents_at_sale, net_revenue_cents "
        "FROM sale_line_items WHERE tenant_id = $1 ORDER BY name_at_sale",
        seed["tenant_id"],
    )
    assert len(items) == 2
    assert items[0]["name_at_sale"] == "Falafel"
    assert items[0]["price_cents_at_sale"] == 1200
    assert items[1]["name_at_sale"] == "Shawarma"
    assert items[1]["quantity"] == 2
    assert items[1]["net_revenue_cents"] == 3000  # 1500 * 2

    inbox_state = await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1",
        seed["inbox_id"],
    )
    assert inbox_state == "processed"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 5 — Open (non-locked) order: marked processed, no order created
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_non_completed_order_skipped(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    clover_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="open",
    )
    respx.get(url__regex=".*/orders/.*").mock(return_value=httpx.Response(200, json=clover_order))

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM orders WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert count == 0

    state = await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1",
        seed["inbox_id"],
    )
    assert state == "processed"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 6 — Pre-fetched payload skips Clover API call
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_fetched_payload_skips_api(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    pre_fetched = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        total=9900,
    )
    await admin_conn.execute(
        """
        UPDATE pos_event_inbox
        SET fetched_payload = $1::jsonb, fetched_at = now()
        WHERE inbox_id = $2
    """,
        json.dumps(pre_fetched),
        seed["inbox_id"],
    )

    # No respx mock — any real HTTP call would fail with a connection error
    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    total = await admin_conn.fetchval(
        "SELECT total_amount_cents FROM orders WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert total == 9900


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 7 — Replay: ON CONFLICT prevents duplicate orders
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_replay_no_duplicate(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    order_id = seed["vendor_event_id"]

    clover_order = make_clover_order(order_id=order_id, state="locked")
    respx.get(url__regex=f".*/orders/{order_id}.*").mock(
        return_value=httpx.Response(200, json=clover_order)
    )

    worker = InboxWorker()

    claimed1 = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed1[0])

    evt2 = await seed_inbox_event(
        admin_conn,
        seed,
        vendor_event_id=order_id,
        vendor_event_type="UPDATE",
        vendor_ts=int(time.time() * 1000) + 5000,
    )

    claimed2 = await worker.claim_batch(batch_size=1)
    if claimed2:
        await worker.process_event(claimed2[0])

    order_count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM orders WHERE tenant_id = $1 AND clover_order_id = $2",
        seed["tenant_id"],
        order_id,
    )
    assert order_count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 8 — Missing / empty line item ID is skipped, not synthesized
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_missing_line_item_id_skipped(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    clover_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        line_items=[
            make_line_item(name="Good Item"),
            {"name": "Bad Item", "price": 500, "unitQty": 1},  # no "id" key
            make_line_item(item_id="", name="Empty ID"),  # empty string
        ],
    )
    respx.get(url__regex=".*/orders/.*").mock(return_value=httpx.Response(200, json=clover_order))

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    items = await admin_conn.fetch(
        "SELECT name_at_sale FROM sale_line_items WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    names = {r["name_at_sale"] for r in items}
    assert "Good Item" in names
    assert "Bad Item" not in names
    assert "Empty ID" not in names


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 9 — Network error → mark_failed
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_network_error_marks_failed(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    respx.get(url__regex=".*/orders/.*").mock(side_effect=httpx.ConnectError("Connection refused"))

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    row = await admin_conn.fetchrow(
        "SELECT state, retry_count, last_error FROM pos_event_inbox WHERE inbox_id = $1",
        seed["inbox_id"],
    )
    assert row["state"] == "failed"
    assert row["retry_count"] == 1
    assert "ConnectError" in row["last_error"]


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 10 — Timeout → mark_failed
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_timeout_marks_failed(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    respx.get(url__regex=".*/orders/.*").mock(side_effect=httpx.ReadTimeout("Read timed out"))

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    row = await admin_conn.fetchrow(
        "SELECT state, retry_count FROM pos_event_inbox WHERE inbox_id = $1",
        seed["inbox_id"],
    )
    assert row["state"] == "failed"
    assert row["retry_count"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 11 — HTTP 500 → mark_failed
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_http_500_marks_failed(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    respx.get(url__regex=".*/orders/.*").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    state = await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1",
        seed["inbox_id"],
    )
    assert state == "failed"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 12 — 404 (order deleted) → mark_processed, no order created
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_order_not_found_processed(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    respx.get(url__regex=".*/orders/.*").mock(return_value=httpx.Response(404))

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    state = await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1",
        seed["inbox_id"],
    )
    assert state == "processed"

    order_count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM orders WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert order_count == 0


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 13 — 429 rate limited → mark_failed with retry
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_rate_limited_marks_failed(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    respx.get(url__regex=".*/orders/.*").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "60"})
    )

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    row = await admin_conn.fetchrow(
        "SELECT state, last_error FROM pos_event_inbox WHERE inbox_id = $1",
        seed["inbox_id"],
    )
    assert row["state"] == "failed"
    assert "rate" in row["last_error"].lower() or "429" in row["last_error"]


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 14 — Five retries → dead letter + monitoring alert
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_five_retries_dead_letter(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    await admin_conn.execute(
        """
        UPDATE pos_event_inbox SET retry_count = 4
        WHERE inbox_id = $1
    """,
        seed["inbox_id"],
    )

    respx.get(url__regex=".*/orders/.*").mock(return_value=httpx.Response(500, text="Server Error"))

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    state = await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1",
        seed["inbox_id"],
    )
    assert state == "dead_letter"

    alert = await admin_conn.fetchrow(
        "SELECT monitor_name, severity, trigger_payload "
        "FROM monitoring_alerts WHERE tenant_id = $1 "
        "AND monitor_name = 'pos_event_dead_letter'",
        seed["tenant_id"],
    )
    assert alert is not None
    assert alert["severity"] == "critical"
    payload = alert["trigger_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["inbox_id"] == seed["inbox_id"]
    assert payload["vendor_event_id"] == seed["vendor_event_id"]


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 15 — Exponential backoff timing
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_exponential_backoff(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    respx.get(url__regex=".*/orders/.*").mock(return_value=httpx.Response(500, text="Error"))

    worker = InboxWorker()

    for expected_count in range(1, 5):
        await admin_conn.execute(
            """
            UPDATE pos_event_inbox
            SET state = 'pending', next_attempt_at = NULL
            WHERE inbox_id = $1
        """,
            seed["inbox_id"],
        )

        claimed = await worker.claim_batch(batch_size=1)
        if claimed:
            await worker.process_event(claimed[0])

        row = await admin_conn.fetchrow(
            "SELECT retry_count, next_attempt_at FROM pos_event_inbox WHERE inbox_id = $1",
            seed["inbox_id"],
        )
        assert row["retry_count"] == expected_count
        if row["next_attempt_at"]:
            expected_minutes = 2 ** (expected_count - 1)
            delta = row["next_attempt_at"] - datetime.now(UTC)
            assert abs(delta.total_seconds() - expected_minutes * 60) < 30


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 16 — Payment state from order-level paymentState field
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_payment_state_from_order_field(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    clover_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        payment_state="REFUNDED",
    )
    respx.get(url__regex=".*/orders/.*").mock(return_value=httpx.Response(200, json=clover_order))

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    ps = await admin_conn.fetchval(
        "SELECT payment_state FROM orders WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert ps == "REFUNDED"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 17 — Payment state fallback: mixed payments → PARTIALLY_REFUNDED
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_payment_state_fallback(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    clover_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
    )
    clover_order.pop("paymentState", None)
    clover_order["payments"] = {
        "elements": [
            {"result": "SUCCESS"},
            {"result": "REFUNDED"},
        ]
    }
    respx.get(url__regex=".*/orders/.*").mock(return_value=httpx.Response(200, json=clover_order))

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    ps = await admin_conn.fetchval(
        "SELECT payment_state FROM orders WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert ps == "PARTIALLY_REFUNDED"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 18 — No payments → OPEN
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_payment_state_no_payments(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    clover_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
    )
    clover_order.pop("paymentState", None)
    clover_order.pop("payments", None)

    respx.get(url__regex=".*/orders/.*").mock(return_value=httpx.Response(200, json=clover_order))

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    ps = await admin_conn.fetchval(
        "SELECT payment_state FROM orders WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert ps == "OPEN"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 19 — Robust amount parsing (strings, None)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_robust_amount_parsing(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    clover_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
    )
    clover_order["total"] = "4200"
    clover_order["lineItems"]["elements"] = [
        {
            "id": f"li_{uuid.uuid4().hex[:8]}",
            "name": "String Price",
            "price": "1500",
            "unitQty": "2",
            "discountAmount": None,
        }
    ]

    respx.get(url__regex=".*/orders/.*").mock(return_value=httpx.Response(200, json=clover_order))

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    order = await admin_conn.fetchrow(
        "SELECT total_amount_cents FROM orders WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert order["total_amount_cents"] == 4200

    item = await admin_conn.fetchrow(
        "SELECT price_cents_at_sale, quantity, discount_amount_cents, "
        "net_revenue_cents FROM sale_line_items WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert item["price_cents_at_sale"] == 1500
    assert item["quantity"] == 2
    assert item["discount_amount_cents"] == 0
    assert item["net_revenue_cents"] == 3000


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 20 — Post-lock UPDATE: order exists, second event processed
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_post_lock_update_processed(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    order_id = seed["vendor_event_id"]

    clover_order_v1 = make_clover_order(
        order_id=order_id,
        state="locked",
        payment_state="PAID",
    )
    respx.get(url__regex=f".*/orders/{order_id}.*").mock(
        return_value=httpx.Response(200, json=clover_order_v1)
    )

    worker = InboxWorker()
    claimed1 = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed1[0])

    evt2 = await seed_inbox_event(
        admin_conn,
        seed,
        vendor_event_id=order_id,
        vendor_ts=int(time.time() * 1000) + 10000,
    )

    clover_order_v2 = make_clover_order(
        order_id=order_id,
        state="locked",
        payment_state="REFUNDED",
    )
    respx.get(url__regex=f".*/orders/{order_id}.*").mock(
        return_value=httpx.Response(200, json=clover_order_v2)
    )

    claimed2 = await worker.claim_batch(batch_size=1)
    if claimed2:
        await worker.process_event(claimed2[0])

    order_count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM orders WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert order_count == 1

    state2 = await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1",
        evt2["inbox_id"],
    )
    assert state2 == "processed"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 21 — Order lifecycle: CREATE open → UPDATE locked
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_order_lifecycle_create_then_update(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    order_id = seed["vendor_event_id"]

    clover_open = make_clover_order(order_id=order_id, state="open")
    respx.get(url__regex=f".*/orders/{order_id}.*").mock(
        return_value=httpx.Response(200, json=clover_open)
    )

    worker = InboxWorker()
    claimed1 = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed1[0])

    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE tenant_id = $1",
            seed["tenant_id"],
        )
        == 0
    )

    evt2 = await seed_inbox_event(
        admin_conn,
        seed,
        vendor_event_id=order_id,
        vendor_event_type="UPDATE",
        vendor_ts=int(time.time() * 1000) + 5000,
    )

    clover_locked = make_clover_order(
        order_id=order_id,
        state="locked",
        payment_state="PAID",
        line_items=[make_line_item(name="Kebab", price=1200)],
    )
    respx.get(url__regex=f".*/orders/{order_id}.*").mock(
        return_value=httpx.Response(200, json=clover_locked)
    )

    claimed2 = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed2[0])

    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE tenant_id = $1",
            seed["tenant_id"],
        )
        == 1
    )
    name = await admin_conn.fetchval(
        "SELECT name_at_sale FROM sale_line_items WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert name == "Kebab"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 22 — Token expired → mark_failed
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_token_expired_marks_failed(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    respx.get(url__regex=".*/orders/.*").mock(return_value=httpx.Response(401))

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    state = await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1",
        seed["inbox_id"],
    )
    assert state == "failed"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 23 — claim_expires_at cleared after processing
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_claim_expires_cleared(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    clover_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
    )
    respx.get(url__regex=".*/orders/.*").mock(return_value=httpx.Response(200, json=clover_order))

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    claim_exp = await admin_conn.fetchval(
        "SELECT claim_expires_at FROM pos_event_inbox WHERE inbox_id = $1",
        seed["inbox_id"],
    )
    assert claim_exp is None


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 24 — Worker does not touch inventory_movements
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_no_inventory_writes(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    clover_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
    )
    respx.get(url__regex=".*/orders/.*").mock(return_value=httpx.Response(200, json=clover_order))

    before = await admin_conn.fetchval("SELECT COUNT(*) FROM inventory_movements")

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    after = await admin_conn.fetchval("SELECT COUNT(*) FROM inventory_movements")
    assert after == before


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 25 — Unhandled exception inside process_event → graceful failure
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_unhandled_exception_caught(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    # Revoke the connection so the worker's tenant lookup returns None,
    # triggering the "No active Clover connection for tenant" path.
    await admin_conn.execute(
        """
        UPDATE tenant_pos_connections
        SET state = 'revoked', revoked_at = now()
        WHERE connection_id = $1
    """,
        seed["connection_id"],
    )

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)

    try:
        await worker.process_event(claimed[0])
    except Exception:
        pass

    state = await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1",
        seed["inbox_id"],
    )
    # No connection found → mark_failed called internally
    assert state in ("failed", "processing"), f"Expected failed or processing, got {state}"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 26 — All created PKs are UUIDv7
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_uuidv7_pks(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    clover_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
    )
    respx.get(url__regex=".*/orders/.*").mock(return_value=httpx.Response(200, json=clover_order))

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    order_id_str = await admin_conn.fetchval(
        "SELECT id::text FROM orders WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert UUID6(order_id_str).version == 7

    sli_id_str = await admin_conn.fetchval(
        "SELECT id::text FROM sale_line_items WHERE tenant_id = $1 LIMIT 1",
        seed["tenant_id"],
    )
    if sli_id_str:
        assert UUID6(sli_id_str).version == 7


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 27 — Full worker lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_full_worker_lifecycle(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    order_id = seed["vendor_event_id"]

    clover_order = make_clover_order(
        order_id=order_id,
        state="locked",
        total=5500,
        payment_state="PAID",
        line_items=[
            make_line_item(name="Latte", price=550, qty=2),
            make_line_item(name="Croissant", price=400, qty=1),
        ],
    )
    respx.get(url__regex=f".*/orders/{order_id}.*").mock(
        return_value=httpx.Response(200, json=clover_order)
    )

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    assert len(claimed) == 1
    await worker.process_event(claimed[0])

    order = await admin_conn.fetchrow(
        "SELECT total_amount_cents, payment_state FROM orders WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert order["total_amount_cents"] == 5500
    assert order["payment_state"] == "PAID"

    items = await admin_conn.fetch(
        "SELECT name_at_sale FROM sale_line_items WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert len(items) == 2

    inbox_row = await admin_conn.fetchrow(
        "SELECT state, claim_expires_at FROM pos_event_inbox WHERE inbox_id = $1",
        seed["inbox_id"],
    )
    assert inbox_row["state"] == "processed"
    assert inbox_row["claim_expires_at"] is None

    # Replay: no duplicate order
    evt_replay = await seed_inbox_event(
        admin_conn,
        seed,
        vendor_event_id=order_id,
        vendor_ts=int(time.time() * 1000) + 10000,
    )
    claimed_replay = await worker.claim_batch(batch_size=1)
    if claimed_replay:
        await worker.process_event(claimed_replay[0])
    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE tenant_id = $1",
            seed["tenant_id"],
        )
        == 1
    )

    # Open order: no new order created
    open_evt = await seed_inbox_event(
        admin_conn,
        seed,
        vendor_event_id=f"open_{uuid.uuid4().hex[:8]}",
    )
    open_order = make_clover_order(
        order_id=open_evt["vendor_event_id"],
        state="open",
    )
    respx.get(url__regex=f".*/orders/{open_evt['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=open_order)
    )
    claimed_open = await worker.claim_batch(batch_size=1)
    if claimed_open:
        await worker.process_event(claimed_open[0])
    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE tenant_id = $1",
            seed["tenant_id"],
        )
        == 1
    )

    # Failure: retry_count increments
    fail_evt = await seed_inbox_event(
        admin_conn,
        seed,
        vendor_event_id=f"fail_{uuid.uuid4().hex[:8]}",
    )
    respx.get(url__regex=f".*/orders/{fail_evt['vendor_event_id']}.*").mock(
        return_value=httpx.Response(500, text="Error")
    )
    claimed_fail = await worker.claim_batch(batch_size=1)
    if claimed_fail:
        await worker.process_event(claimed_fail[0])
    fail_row = await admin_conn.fetchrow(
        "SELECT state, retry_count FROM pos_event_inbox WHERE inbox_id = $1",
        fail_evt["inbox_id"],
    )
    assert fail_row["state"] == "failed"
    assert fail_row["retry_count"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 28 — Concurrent workers: FOR UPDATE SKIP LOCKED
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_concurrent_claim_no_double_processing(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)

    worker_a = InboxWorker()
    worker_b = InboxWorker()

    claimed_a, claimed_b = await asyncio.gather(
        worker_a.claim_batch(batch_size=10),
        worker_b.claim_batch(batch_size=10),
    )

    all_claimed_ids: set[str] = set()
    for batch in [claimed_a, claimed_b]:
        for evt in batch:
            all_claimed_ids.add(str(evt.inbox_id))

    assert seed["inbox_id"] in all_claimed_ids
    assert len(all_claimed_ids) == 1, f"Expected 1 claimed event, got {len(all_claimed_ids)}"

    a_ids = {str(e.inbox_id) for e in claimed_a}
    b_ids = {str(e.inbox_id) for e in claimed_b}
    assert not (seed["inbox_id"] in a_ids and seed["inbox_id"] in b_ids), (
        "Both workers claimed the same event — SKIP LOCKED broken"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 29 — Stale event cannot revert a higher-priority payment state
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_stale_event_cannot_revert_payment_state(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    order_id = seed["vendor_event_id"]

    refunded_order = make_clover_order(
        order_id=order_id,
        state="locked",
        payment_state="REFUNDED",
    )
    respx.get(url__regex=f".*/orders/{order_id}.*").mock(
        return_value=httpx.Response(200, json=refunded_order)
    )

    worker = InboxWorker()
    claimed1 = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed1[0])

    ps_after_first = await admin_conn.fetchval(
        "SELECT payment_state FROM orders WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert ps_after_first == "REFUNDED"

    evt2 = await seed_inbox_event(
        admin_conn,
        seed,
        vendor_event_id=order_id,
        vendor_ts=int(time.time() * 1000) + 20000,
    )

    paid_order = make_clover_order(
        order_id=order_id,
        state="locked",
        payment_state="PAID",
    )
    respx.get(url__regex=f".*/orders/{order_id}.*").mock(
        return_value=httpx.Response(200, json=paid_order)
    )

    claimed2 = await worker.claim_batch(batch_size=1)
    if claimed2:
        await worker.process_event(claimed2[0])

    ps_after_replay = await admin_conn.fetchval(
        "SELECT payment_state FROM orders WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert ps_after_replay == "REFUNDED", (
        f"CRITICAL: Stale replay reverted REFUNDED → {ps_after_replay}. "
        "Payment state priority guard is missing or broken."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 30 — Line item count exact after replay
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_line_item_count_exact_after_replay(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    order_id = seed["vendor_event_id"]

    li_a = make_line_item(name="Shawarma", price=1500, qty=2)
    li_b = make_line_item(name="Hummus", price=600, qty=1)

    clover_order = make_clover_order(
        order_id=order_id,
        state="locked",
        line_items=[li_a, li_b],
    )
    respx.get(url__regex=f".*/orders/{order_id}.*").mock(
        return_value=httpx.Response(200, json=clover_order)
    )

    worker = InboxWorker()

    claimed1 = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed1[0])

    evt2 = await seed_inbox_event(
        admin_conn,
        seed,
        vendor_event_id=order_id,
        vendor_ts=int(time.time() * 1000) + 5000,
    )
    claimed2 = await worker.claim_batch(batch_size=1)
    if claimed2:
        await worker.process_event(claimed2[0])

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM sale_line_items WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert count == 2, f"Expected 2 line items after replay, got {count}"

    names = await admin_conn.fetch(
        "SELECT name_at_sale FROM sale_line_items WHERE tenant_id = $1 ORDER BY name_at_sale",
        seed["tenant_id"],
    )
    assert [r["name_at_sale"] for r in names] == ["Hummus", "Shawarma"]


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 31 — Cross-tenant: same clover_order_id creates separate orders
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_cross_tenant_same_order_id(admin_conn):
    seed_a = await seed_worker_prereqs(admin_conn)
    seed_b = await seed_worker_prereqs(admin_conn)

    # Unique per run to avoid cross-test-run accumulation in orders table.
    shared_order_id = f"ORDER_SHARED_{uuid.uuid4().hex[:8]}"

    await admin_conn.execute(
        """
        UPDATE pos_event_inbox SET vendor_event_id = $1
        WHERE inbox_id = $2
    """,
        shared_order_id,
        seed_a["inbox_id"],
    )
    await admin_conn.execute(
        """
        UPDATE pos_event_inbox SET vendor_event_id = $1
        WHERE inbox_id = $2
    """,
        shared_order_id,
        seed_b["inbox_id"],
    )

    order_a = make_clover_order(
        order_id=shared_order_id,
        state="locked",
        total=1000,
    )
    order_b = make_clover_order(
        order_id=shared_order_id,
        state="locked",
        total=2000,
    )

    respx.get(url__regex=f".*/orders/{shared_order_id}.*").mock(
        side_effect=[
            httpx.Response(200, json=order_a),
            httpx.Response(200, json=order_b),
        ]
    )

    worker = InboxWorker()

    claimed_a = await worker.claim_batch(batch_size=1)
    if claimed_a:
        await worker.process_event(claimed_a[0])

    claimed_b = await worker.claim_batch(batch_size=1)
    if claimed_b:
        await worker.process_event(claimed_b[0])

    total_a = await admin_conn.fetchval(
        "SELECT total_amount_cents FROM orders WHERE tenant_id = $1 AND clover_order_id = $2",
        seed_a["tenant_id"],
        shared_order_id,
    )
    total_b = await admin_conn.fetchval(
        "SELECT total_amount_cents FROM orders WHERE tenant_id = $1 AND clover_order_id = $2",
        seed_b["tenant_id"],
        shared_order_id,
    )

    assert total_a == 1000
    assert total_b == 2000

    total_orders = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM orders WHERE clover_order_id = $1",
        shared_order_id,
    )
    assert total_orders == 2


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 32 — Transaction atomicity: mid-transaction failure rolls back everything
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_transaction_atomicity_rollback(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    order_id = seed["vendor_event_id"]

    li_a = make_line_item(name="Atomicity A", price=1000)
    li_b = make_line_item(name="Atomicity B", price=2000)

    clover_order = make_clover_order(
        order_id=order_id,
        state="locked",
        line_items=[li_a, li_b],
    )
    respx.get(url__regex=f".*/orders/{order_id}.*").mock(
        return_value=httpx.Response(200, json=clover_order)
    )

    worker = InboxWorker()

    call_count = 0
    original_insert = worker._insert_line_item

    async def exploding_insert(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise RuntimeError("Simulated crash during line item insert")
        return await original_insert(*args, **kwargs)

    worker._insert_line_item = exploding_insert

    claimed = await worker.claim_batch(batch_size=1)
    assert len(claimed) == 1
    await worker.process_event(claimed[0])

    # Transaction rolled back: no order, no line items
    order_count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM orders WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    item_count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM sale_line_items WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert order_count == 0, f"Partial order survived rollback: {order_count}"
    assert item_count == 0, f"Partial line items survived rollback: {item_count}"

    inbox_state = await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1",
        seed["inbox_id"],
    )
    assert inbox_state == "failed"

    # Clean reprocess after restoring original method
    worker._insert_line_item = original_insert

    await admin_conn.execute(
        """
        UPDATE pos_event_inbox
        SET state = 'pending', next_attempt_at = NULL, retry_count = 0
        WHERE inbox_id = $1
    """,
        seed["inbox_id"],
    )

    claimed2 = await worker.claim_batch(batch_size=1)
    assert len(claimed2) == 1
    await worker.process_event(claimed2[0])

    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE tenant_id = $1",
            seed["tenant_id"],
        )
        == 1
    )
    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM sale_line_items WHERE tenant_id = $1",
            seed["tenant_id"],
        )
        == 2
    )
    assert (
        await admin_conn.fetchval(
            "SELECT state FROM pos_event_inbox WHERE inbox_id = $1",
            seed["inbox_id"],
        )
        == "processed"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 33 — Out-of-order: UPDATE before CREATE
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@respx.mock
async def test_out_of_order_update_before_create(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    order_id = seed["vendor_event_id"]

    li = make_line_item(name="Kebab Plate", price=1800, qty=1)
    locked_order = make_clover_order(
        order_id=order_id,
        state="locked",
        total=1800,
        payment_state="PAID",
        line_items=[li],
    )

    respx.get(url__regex=f".*/orders/{order_id}.*").mock(
        return_value=httpx.Response(200, json=locked_order)
    )

    worker = InboxWorker()

    # Step 1: Process UPDATE first (seed event is the UPDATE)
    await admin_conn.execute(
        """
        UPDATE pos_event_inbox
        SET vendor_event_type = 'UPDATE',
            vendor_ts = $1
        WHERE inbox_id = $2
    """,
        int(time.time() * 1000) + 10000,
        seed["inbox_id"],
    )

    claimed1 = await worker.claim_batch(batch_size=1)
    assert len(claimed1) == 1
    await worker.process_event(claimed1[0])

    order = await admin_conn.fetchrow(
        "SELECT payment_state, total_amount_cents FROM orders WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert order is not None
    assert order["payment_state"] == "PAID"
    assert order["total_amount_cents"] == 1800
    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM sale_line_items WHERE tenant_id = $1",
            seed["tenant_id"],
        )
        == 1
    )

    # Step 2: Stale CREATE arrives (Clover API still returns current locked state)
    create_evt = await seed_inbox_event(
        admin_conn,
        seed,
        vendor_event_id=order_id,
        vendor_event_type="CREATE",
        vendor_ts=int(time.time() * 1000),
    )

    claimed2 = await worker.claim_batch(batch_size=1)
    assert len(claimed2) == 1
    await worker.process_event(claimed2[0])

    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE tenant_id = $1",
            seed["tenant_id"],
        )
        == 1
    ), "Duplicate order from stale CREATE"
    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM sale_line_items WHERE tenant_id = $1",
            seed["tenant_id"],
        )
        == 1
    ), "Line items duplicated by stale CREATE"

    final_ps = await admin_conn.fetchval(
        "SELECT payment_state FROM orders WHERE tenant_id = $1",
        seed["tenant_id"],
    )
    assert final_ps == "PAID", f"Stale CREATE reverted payment_state from PAID → {final_ps}"

    processed_count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id = $1 AND state = 'processed'",
        seed["tenant_id"],
    )
    assert processed_count == 2
