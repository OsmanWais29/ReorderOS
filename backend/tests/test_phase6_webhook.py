"""Sprint 4 — Phase 6: Clover webhook endpoint (36 tests + 13 fuzz = 49 executions).

POST /api/v1/webhooks/pos/clover

admin_conn is asyncpg autocommit — seed data commits immediately and is
visible to the TestClient's separate DB connection.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import app
from tests.helpers.phase6 import (
    make_non_order_event,
    make_order_event,
    make_webhook_payload,
    seed_webhook_prereqs,
)

# ── Sync TestClient (shadows async one from conftest for this file) ───────────


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── Auth helper ───────────────────────────────────────────────────────────────


def _auth():
    from app.core.config import get_settings

    return {"X-Clover-Auth": get_settings().clover_webhook_auth_code}


# ── Test 1: Verification Code Echoed ─────────────────────────────────────────


def test_verification_code_echoed(client):
    resp = client.post(
        "/api/v1/webhooks/pos/clover", json={"verificationCode": "test_verify_abc123"}
    )
    assert resp.status_code == 200
    assert resp.text == "test_verify_abc123"
    assert "text/plain" in resp.headers.get("content-type", "")


# ── Test 2: Missing Auth → 401 ────────────────────────────────────────────────


def test_missing_auth_rejected(client):
    resp = client.post(
        "/api/v1/webhooks/pos/clover", json=make_webhook_payload(f"merch_{uuid.uuid4().hex[:8]}")
    )
    assert resp.status_code == 401


# ── Test 3: Wrong Auth → 401 ─────────────────────────────────────────────────


def test_wrong_auth_rejected(client):
    resp = client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(f"merch_{uuid.uuid4().hex[:8]}"),
        headers={"X-Clover-Auth": "wrong_code_xyz"},
    )
    assert resp.status_code == 401


# ── Test 4: Blank Auth Env → 401 ─────────────────────────────────────────────


def test_blank_auth_env_rejects(client, monkeypatch):
    """blank expected_auth → 'if not expected_auth' fires before compare_digest."""
    from app.core.config import get_settings

    monkeypatch.setenv("CLOVER_WEBHOOK_AUTH_CODE", "")
    get_settings.cache_clear()

    resp = client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(f"merch_{uuid.uuid4().hex[:8]}"),
        headers={"X-Clover-Auth": ""},
    )
    assert resp.status_code == 401

    get_settings.cache_clear()  # restore for subsequent tests


# ── Test 5: Valid Webhook Stores Event ───────────────────────────────────────


@pytest.mark.asyncio
async def test_valid_webhook_stores_event(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)
    order_id = f"order_{uuid.uuid4().hex[:8]}"
    ts = int(time.time() * 1000)

    resp = client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(
            seed["merchant_id"], events=[make_order_event(order_id, "UPDATE", ts)]
        ),
        headers=_auth(),
    )
    assert resp.status_code == 200

    row = await admin_conn.fetchrow(
        "SELECT vendor_event_id, vendor_object_type, vendor_event_type, "
        "vendor_ts, signature_verified, source, state, connection_id "
        "FROM pos_event_inbox WHERE tenant_id = $1 AND vendor_event_id = $2",
        seed["tenant_id"],
        order_id,
    )
    assert row is not None
    assert row["vendor_event_id"] == order_id
    assert row["vendor_object_type"] == "O"
    assert row["vendor_event_type"] == "UPDATE"
    assert row["vendor_ts"] == ts
    assert row["signature_verified"] is True
    assert row["source"] == "webhook"
    assert row["state"] == "pending"
    assert str(row["connection_id"]) == seed["connection_id"]


# ── Test 6: Two UPDATEs Different ts → Two Rows (exact tuples) ───────────────


@pytest.mark.asyncio
async def test_two_updates_different_ts(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)
    order_id = f"multi_{uuid.uuid4().hex[:8]}"
    base_ts = int(time.time() * 1000)

    for offset in [0, 5000]:
        resp = client.post(
            "/api/v1/webhooks/pos/clover",
            json=make_webhook_payload(
                seed["merchant_id"], events=[make_order_event(order_id, "UPDATE", base_ts + offset)]
            ),
            headers=_auth(),
        )
        assert resp.status_code == 200

    rows = await admin_conn.fetch(
        "SELECT vendor_event_type, vendor_ts FROM pos_event_inbox "
        "WHERE tenant_id = $1 AND vendor_event_id = $2 ORDER BY vendor_ts ASC",
        seed["tenant_id"],
        order_id,
    )
    assert len(rows) == 2
    assert rows[0]["vendor_event_type"] == "UPDATE"
    assert rows[0]["vendor_ts"] == base_ts
    assert rows[1]["vendor_event_type"] == "UPDATE"
    assert rows[1]["vendor_ts"] == base_ts + 5000


# ── Test 7: Exact Duplicate → 200, No Duplicate Row ──────────────────────────


@pytest.mark.asyncio
async def test_exact_duplicate_ignored(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)
    order_id = f"dup_{uuid.uuid4().hex[:8]}"
    ts = int(time.time() * 1000)
    payload = make_webhook_payload(
        seed["merchant_id"], events=[make_order_event(order_id, "CREATE", ts)]
    )

    r1 = client.post("/api/v1/webhooks/pos/clover", json=payload, headers=_auth())
    r2 = client.post("/api/v1/webhooks/pos/clover", json=payload, headers=_auth())
    assert r1.status_code == 200
    assert r2.status_code == 200

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id=$1 AND vendor_event_id=$2",
        seed["tenant_id"],
        order_id,
    )
    assert count == 1


# ── Test 8: CREATE + UPDATE Same ts → Both Stored (exact tuples) ─────────────


@pytest.mark.asyncio
async def test_create_and_update_same_ts(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)
    order_id = f"lifecycle_{uuid.uuid4().hex[:8]}"
    ts = int(time.time() * 1000)

    for etype in ["CREATE", "UPDATE"]:
        client.post(
            "/api/v1/webhooks/pos/clover",
            json=make_webhook_payload(
                seed["merchant_id"], events=[make_order_event(order_id, etype, ts)]
            ),
            headers=_auth(),
        )

    rows = await admin_conn.fetch(
        "SELECT vendor_event_type, vendor_ts FROM pos_event_inbox "
        "WHERE tenant_id=$1 AND vendor_event_id=$2 ORDER BY vendor_event_type ASC",
        seed["tenant_id"],
        order_id,
    )
    assert len(rows) == 2
    assert {r["vendor_event_type"] for r in rows} == {"CREATE", "UPDATE"}
    assert all(r["vendor_ts"] == ts for r in rows)


# ── Test 9: Non-Order Events Not Stored ──────────────────────────────────────


@pytest.mark.asyncio
async def test_non_order_events_not_stored(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)
    events = [
        make_non_order_event("P", f"pay_{uuid.uuid4().hex[:8]}"),
        make_non_order_event("I", f"inv_{uuid.uuid4().hex[:8]}"),
        make_non_order_event("E", f"emp_{uuid.uuid4().hex[:8]}"),
    ]
    resp = client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(seed["merchant_id"], events=events),
        headers=_auth(),
    )
    assert resp.status_code == 200

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id=$1",
        seed["tenant_id"],
    )
    assert count == 0


# ── Test 10: Unknown Merchant → 200, No Leak ─────────────────────────────────


def test_unknown_merchant_200(client):
    resp = client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(f"UNKNOWN_{uuid.uuid4().hex[:8]}", events=[make_order_event()]),
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert "UNKNOWN" not in resp.text


# ── Test 11: Multiple Events Batched ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_events_batched(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)
    base_ts = int(time.time() * 1000)
    events = [
        make_order_event(f"batch_A_{uuid.uuid4().hex[:6]}", ts=base_ts),
        make_order_event(f"batch_B_{uuid.uuid4().hex[:6]}", ts=base_ts + 1000),
        make_non_order_event("P", f"skip_{uuid.uuid4().hex[:6]}"),
    ]
    resp = client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(seed["merchant_id"], events=events),
        headers=_auth(),
    )
    assert resp.status_code == 200

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id=$1",
        seed["tenant_id"],
    )
    assert count == 2


# ── Test 12: Multiple Merchants ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_merchants(admin_conn, client):
    seed_a = await seed_webhook_prereqs(admin_conn)
    seed_b = await seed_webhook_prereqs(admin_conn)

    payload = {
        "appId": "TEST",
        "merchants": {
            seed_a["merchant_id"]: [make_order_event(f"ma_{uuid.uuid4().hex[:6]}")],
            seed_b["merchant_id"]: [make_order_event(f"mb_{uuid.uuid4().hex[:6]}")],
            f"UNKNOWN_{uuid.uuid4().hex[:8]}": [make_order_event(f"mu_{uuid.uuid4().hex[:6]}")],
        },
    }
    resp = client.post("/api/v1/webhooks/pos/clover", json=payload, headers=_auth())
    assert resp.status_code == 200

    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id=$1",
            seed_a["tenant_id"],
        )
        == 1
    )
    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id=$1",
            seed_b["tenant_id"],
        )
        == 1
    )


# ── Test 13: Malformed objectId → No Crash ───────────────────────────────────


def test_malformed_objectid(client):
    payload = {
        "appId": "TEST",
        "merchants": {
            f"merch_{uuid.uuid4().hex[:8]}": [
                {"objectId": "NOCOLON", "type": "UPDATE", "ts": 1000},
                {"objectId": "", "type": "UPDATE", "ts": 2000},
                {"type": "UPDATE", "ts": 3000},
            ]
        },
    }
    resp = client.post("/api/v1/webhooks/pos/clover", json=payload, headers=_auth())
    assert resp.status_code == 200


# ── Test 14: vendor_ts Stored Correctly ──────────────────────────────────────


@pytest.mark.asyncio
async def test_vendor_ts_stored(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)
    order_id = f"ts_{uuid.uuid4().hex[:8]}"
    exact_ts = 1700000000000

    client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(
            seed["merchant_id"], events=[make_order_event(order_id, ts=exact_ts)]
        ),
        headers=_auth(),
    )

    stored_ts = await admin_conn.fetchval(
        "SELECT vendor_ts FROM pos_event_inbox WHERE tenant_id=$1 AND vendor_event_id=$2",
        seed["tenant_id"],
        order_id,
    )
    assert stored_ts == exact_ts


# ── Test 15: connection_id FK Populated ──────────────────────────────────────


@pytest.mark.asyncio
async def test_connection_id_fk(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)
    order_id = f"fk_{uuid.uuid4().hex[:8]}"

    client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(seed["merchant_id"], events=[make_order_event(order_id)]),
        headers=_auth(),
    )

    cid = await admin_conn.fetchval(
        "SELECT connection_id FROM pos_event_inbox WHERE tenant_id=$1 AND vendor_event_id=$2",
        seed["tenant_id"],
        order_id,
    )
    assert str(cid) == seed["connection_id"]


# ── Test 16: Zero External HTTP Calls ────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_external_calls(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)

    with respx.mock(assert_all_mocked=False) as mock:
        client.post(
            "/api/v1/webhooks/pos/clover",
            json=make_webhook_payload(seed["merchant_id"], events=[make_order_event()]),
            headers=_auth(),
        )
        assert len(mock.calls) == 0, f"Handler made {len(mock.calls)} outbound HTTP calls"


# ── Test 17: Response Time < 500ms ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_response_under_500ms(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)

    # Warm up DB connection pool
    client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(
            seed["merchant_id"], events=[make_order_event(f"warm_{uuid.uuid4().hex[:6]}")]
        ),
        headers=_auth(),
    )

    payload = make_webhook_payload(
        seed["merchant_id"], events=[make_order_event(f"timed_{uuid.uuid4().hex[:6]}")]
    )
    start = time.monotonic()
    resp = client.post("/api/v1/webhooks/pos/clover", json=payload, headers=_auth())
    elapsed_ms = (time.monotonic() - start) * 1000

    assert resp.status_code == 200
    assert elapsed_ms < 500, f"Took {elapsed_ms:.0f}ms (limit: 500ms)"


# ── Test 18: inbox_id Is UUIDv7 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbox_id_uuidv7(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)
    order_id = f"v7_{uuid.uuid4().hex[:8]}"

    client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(seed["merchant_id"], events=[make_order_event(order_id)]),
        headers=_auth(),
    )

    inbox_id = await admin_conn.fetchval(
        "SELECT inbox_id::text FROM pos_event_inbox WHERE tenant_id=$1 AND vendor_event_id=$2",
        seed["tenant_id"],
        order_id,
    )
    assert inbox_id is not None
    import uuid as _uuid

    assert _uuid.UUID(inbox_id).version == 7


# ── Test 19: Error-State Connection Still Resolves ────────────────────────────


@pytest.mark.asyncio
async def test_error_state_connection_stores_events(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)
    await admin_conn.execute(
        "UPDATE tenant_pos_connections SET state='error' WHERE connection_id=$1",
        seed["connection_id"],
    )

    order_id = f"err_{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(seed["merchant_id"], events=[make_order_event(order_id)]),
        headers=_auth(),
    )
    assert resp.status_code == 200

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id=$1 AND vendor_event_id=$2",
        seed["tenant_id"],
        order_id,
    )
    assert count == 1


# ── Test 20: Revoked Connection → Events Not Stored ───────────────────────────


@pytest.mark.asyncio
async def test_revoked_connection_events_dropped(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)
    await admin_conn.execute(
        "UPDATE tenant_pos_connections SET state='revoked' WHERE connection_id=$1",
        seed["connection_id"],
    )

    order_id = f"rev_{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(seed["merchant_id"], events=[make_order_event(order_id)]),
        headers=_auth(),
    )
    assert resp.status_code == 200

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id=$1",
        seed["tenant_id"],
    )
    assert count == 0


# ── Test 21: Full Lifecycle ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_webhook_lifecycle(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)
    order_id = f"life_{uuid.uuid4().hex[:8]}"
    base_ts = int(time.time() * 1000)
    auth = _auth()

    # 1. Verify
    vr = client.post("/api/v1/webhooks/pos/clover", json={"verificationCode": "lifecycle_123"})
    assert vr.status_code == 200
    assert vr.text == "lifecycle_123"

    # 2. CREATE
    client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(
            seed["merchant_id"], events=[make_order_event(order_id, "CREATE", base_ts)]
        ),
        headers=auth,
    )

    # 3. UPDATE (different ts)
    client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(
            seed["merchant_id"], events=[make_order_event(order_id, "UPDATE", base_ts + 5000)]
        ),
        headers=auth,
    )

    # 4. Duplicate CREATE
    client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(
            seed["merchant_id"], events=[make_order_event(order_id, "CREATE", base_ts)]
        ),
        headers=auth,
    )

    # 5. Payment (skipped)
    client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(seed["merchant_id"], events=[make_non_order_event("P")]),
        headers=auth,
    )

    # 6. Unknown merchant
    client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(f"UNKNOWN_{uuid.uuid4().hex[:8]}", events=[make_order_event()]),
        headers=auth,
    )

    # 7. Verify inbox
    rows = await admin_conn.fetch(
        "SELECT vendor_event_id, vendor_event_type, vendor_ts, "
        "connection_id, signature_verified, state "
        "FROM pos_event_inbox WHERE tenant_id=$1 ORDER BY vendor_ts ASC",
        seed["tenant_id"],
    )
    assert len(rows) == 2
    assert rows[0]["vendor_event_type"] == "CREATE"
    assert rows[0]["vendor_ts"] == base_ts
    assert str(rows[0]["connection_id"]) == seed["connection_id"]
    assert rows[0]["signature_verified"] is True
    assert rows[0]["state"] == "pending"
    assert rows[1]["vendor_event_type"] == "UPDATE"
    assert rows[1]["vendor_ts"] == base_ts + 5000


# ── Test 22: Concurrent Duplicate Delivery ────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_duplicate_delivery(admin_conn, client):
    """20 identical webhooks fired concurrently — exactly 1 row must result."""
    seed = await seed_webhook_prereqs(admin_conn)
    order_id = f"concurrent_{uuid.uuid4().hex[:8]}"
    ts = int(time.time() * 1000)

    payload = make_webhook_payload(
        seed["merchant_id"], events=[make_order_event(order_id, "CREATE", ts)]
    )

    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app),
            base_url="http://testserver",
        ) as ac:
            return await ac.post(
                "/api/v1/webhooks/pos/clover",
                json=payload,
                headers=_auth(),
            )

    responses = await asyncio.gather(*[send() for _ in range(20)])
    for i, r in enumerate(responses):
        assert r.status_code == 200, f"Request {i} got {r.status_code}"

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id=$1 AND vendor_event_id=$2",
        seed["tenant_id"],
        order_id,
    )
    assert count == 1, f"Expected 1 row, got {count} (concurrent duplicate leak)"


# ── Test 23: Partial Batch — Valid Events Survive Unknown Merchant ────────────


@pytest.mark.asyncio
async def test_partial_batch_independent_commits(admin_conn, client):
    seed_a = await seed_webhook_prereqs(admin_conn)
    seed_b = await seed_webhook_prereqs(admin_conn)
    order_a = f"batch_ok1_{uuid.uuid4().hex[:6]}"
    order_b = f"batch_ok2_{uuid.uuid4().hex[:6]}"

    payload = {
        "appId": "TEST",
        "merchants": {
            seed_a["merchant_id"]: [make_order_event(order_a)],
            f"UNKNOWN_{uuid.uuid4().hex[:8]}": [make_order_event(f"skip_{uuid.uuid4().hex[:6]}")],
            seed_b["merchant_id"]: [make_order_event(order_b)],
        },
    }
    resp = client.post("/api/v1/webhooks/pos/clover", json=payload, headers=_auth())
    assert resp.status_code == 200

    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id=$1 AND vendor_event_id=$2",
            seed_a["tenant_id"],
            order_a,
        )
        == 1
    )
    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id=$1 AND vendor_event_id=$2",
            seed_b["tenant_id"],
            order_b,
        )
        == 1
    )


# ── Test 24: Dedup Constraint Exists With Correct Column Order ────────────────


@pytest.mark.asyncio
async def test_dedup_constraint_exists(admin_conn):
    constraint = await admin_conn.fetchval("""
        SELECT conname FROM pg_constraint
        WHERE conname='pos_inbox_dedup_unique' AND contype='u'
    """)
    assert constraint == "pos_inbox_dedup_unique"

    cols = await admin_conn.fetch("""
        SELECT a.attname
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
        WHERE c.conname = 'pos_inbox_dedup_unique'
        ORDER BY array_position(c.conkey, a.attnum)
    """)
    assert [r["attname"] for r in cols] == [
        "tenant_id",
        "vendor",
        "vendor_event_id",
        "vendor_event_type",
        "vendor_ts",
    ]


# ── Test 25: Payload Fuzz — No 500 ───────────────────────────────────────────


@pytest.mark.parametrize(
    "fuzz_payload",
    [
        {"appId": "T", "merchants": {"M": [{"objectId": None, "type": "UPDATE", "ts": 1}]}},
        {"appId": "T", "merchants": {"M": [{"objectId": "O:x", "type": 12345, "ts": 1}]}},
        {"appId": "T", "merchants": {"M": [{"objectId": "O:x", "type": "UPDATE", "ts": "abc"}]}},
        {"appId": "T", "merchants": []},
        {"appId": "T", "merchants": {"M": "not_a_list"}},
        {"appId": "T", "merchants": {}},
        {"appId": "T"},
        {"appId": "T", "merchants": {"M": []}},
        {"appId": "T", "merchants": {"M": [{"objectId": "O:x", "type": None, "ts": None}]}},
        {
            "appId": "T",
            "merchants": {
                "M": [{"objectId": "O:x", "type": "UPDATE", "ts": 1, "extra": {"n": True}}]
            },
        },
        {"appId": "T", "merchants": {"M": [{"objectId": "O:x", "type": "UPDATE", "ts": -1}]}},
        {
            "appId": "T",
            "merchants": {"M": [{"objectId": "O:x", "type": "UPDATE", "ts": 999999999999999}]},
        },
        {"appId": "T", "merchants": {"M": [{"objectId": "O:x", "type": "UPDATE", "ts": 0}]}},
    ],
)
def test_payload_fuzz_no_crash(client, fuzz_payload):
    resp = client.post(
        "/api/v1/webhooks/pos/clover",
        json=fuzz_payload,
        headers=_auth(),
    )
    assert resp.status_code in (200, 401), f"Got {resp.status_code} on fuzz payload: {fuzz_payload}"


# ── Test 26: Out-of-Order Delivery ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_out_of_order_delivery(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)
    order_id = f"ooo_{uuid.uuid4().hex[:8]}"
    ts_create = int(time.time() * 1000)
    ts_update = ts_create + 5000

    # UPDATE arrives first (out of order)
    client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(
            seed["merchant_id"], events=[make_order_event(order_id, "UPDATE", ts_update)]
        ),
        headers=_auth(),
    )
    # CREATE arrives second (delayed)
    client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(
            seed["merchant_id"], events=[make_order_event(order_id, "CREATE", ts_create)]
        ),
        headers=_auth(),
    )

    rows_by_ts = await admin_conn.fetch(
        "SELECT vendor_event_type, vendor_ts FROM pos_event_inbox "
        "WHERE tenant_id=$1 AND vendor_event_id=$2 ORDER BY vendor_ts ASC",
        seed["tenant_id"],
        order_id,
    )
    assert len(rows_by_ts) == 2
    assert rows_by_ts[0]["vendor_event_type"] == "CREATE"
    assert rows_by_ts[0]["vendor_ts"] == ts_create
    assert rows_by_ts[1]["vendor_event_type"] == "UPDATE"
    assert rows_by_ts[1]["vendor_ts"] == ts_update

    rows_by_rcv = await admin_conn.fetch(
        "SELECT vendor_event_type FROM pos_event_inbox "
        "WHERE tenant_id=$1 AND vendor_event_id=$2 ORDER BY received_at ASC",
        seed["tenant_id"],
        order_id,
    )
    assert rows_by_rcv[0]["vendor_event_type"] == "UPDATE"  # received first
    assert rows_by_rcv[1]["vendor_event_type"] == "CREATE"  # received second


# ── Test 27: Session Survives ON CONFLICT DO NOTHING ─────────────────────────


@pytest.mark.asyncio
async def test_session_healthy_after_duplicate(admin_conn, client):
    """Duplicate on event 2 must not prevent event 3 from inserting."""
    seed = await seed_webhook_prereqs(admin_conn)
    ts = int(time.time() * 1000)
    event_1_id = f"sess_A_{uuid.uuid4().hex[:6]}"
    event_3_id = f"sess_C_{uuid.uuid4().hex[:6]}"

    # First request: insert event_1
    client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(
            seed["merchant_id"], events=[make_order_event(event_1_id, "CREATE", ts)]
        ),
        headers=_auth(),
    )

    # Second request: duplicate of event_1 + new event_3
    client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(
            seed["merchant_id"],
            events=[
                make_order_event(event_1_id, "CREATE", ts),  # duplicate
                make_order_event(event_3_id, "CREATE", ts + 1000),  # new
            ],
        ),
        headers=_auth(),
    )

    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id=$1 AND vendor_event_id=$2",
            seed["tenant_id"],
            event_1_id,
        )
        == 1
    ), "Duplicate was not deduplicated"

    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id=$1 AND vendor_event_id=$2",
            seed["tenant_id"],
            event_3_id,
        )
        == 1
    ), "Event after duplicate was lost — session poisoned"


# ── Test 28: Race with Disconnect → Graceful 200 ─────────────────────────────


@pytest.mark.asyncio
async def test_race_with_disconnect_handled(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)
    await admin_conn.execute(
        "UPDATE tenant_pos_connections SET state='revoked' WHERE connection_id=$1",
        seed["connection_id"],
    )

    resp = client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(
            seed["merchant_id"], events=[make_order_event(f"fk_fault_{uuid.uuid4().hex[:8]}")]
        ),
        headers=_auth(),
    )
    assert resp.status_code == 200


# ── Test 29: Large Batch — 50 Events, Exact Set ───────────────────────────────


@pytest.mark.asyncio
async def test_large_batch_50_events(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)
    base_ts = int(time.time() * 1000)
    expected_ids = set()
    events = []
    for i in range(50):
        oid = f"bulk_{i}_{uuid.uuid4().hex[:6]}"
        expected_ids.add(oid)
        events.append(make_order_event(oid, "UPDATE", base_ts + i * 100))

    start = time.monotonic()
    resp = client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(seed["merchant_id"], events=events),
        headers=_auth(),
    )
    elapsed_ms = (time.monotonic() - start) * 1000

    assert resp.status_code == 200
    assert elapsed_ms < 5000, f"50-event batch took {elapsed_ms:.0f}ms"

    rows = await admin_conn.fetch(
        "SELECT vendor_event_id FROM pos_event_inbox WHERE tenant_id=$1",
        seed["tenant_id"],
    )
    stored_ids = {r["vendor_event_id"] for r in rows}
    assert stored_ids == expected_ids, (
        f"Missing: {expected_ids - stored_ids}, Extra: {stored_ids - expected_ids}"
    )


# ── Test 30: Response Time Benchmark (slow marker) ───────────────────────────


@pytest.mark.slow
@pytest.mark.asyncio
async def test_response_time_benchmark(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)

    client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(
            seed["merchant_id"], events=[make_order_event(f"warm_{uuid.uuid4().hex[:6]}")]
        ),
        headers=_auth(),
    )

    start = time.monotonic()
    resp = client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(
            seed["merchant_id"], events=[make_order_event(f"bench_{uuid.uuid4().hex[:6]}")]
        ),
        headers=_auth(),
    )
    elapsed_ms = (time.monotonic() - start) * 1000

    assert resp.status_code == 200
    if elapsed_ms > 200:
        import warnings

        warnings.warn(f"Webhook handler took {elapsed_ms:.0f}ms (target: <200ms)")
    assert elapsed_ms < 500


# ── Test 31: Delayed Retry Duplicate (Cross-Commit) ──────────────────────────


@pytest.mark.asyncio
async def test_delayed_retry_duplicate(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)
    order_id = f"retry_{uuid.uuid4().hex[:8]}"
    ts = int(time.time() * 1000)
    payload = make_webhook_payload(
        seed["merchant_id"], events=[make_order_event(order_id, "CREATE", ts)]
    )

    r1 = client.post("/api/v1/webhooks/pos/clover", json=payload, headers=_auth())
    assert r1.status_code == 200

    await asyncio.sleep(0.5)

    r2 = client.post("/api/v1/webhooks/pos/clover", json=payload, headers=_auth())
    assert r2.status_code == 200

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id=$1 AND vendor_event_id=$2",
        seed["tenant_id"],
        order_id,
    )
    assert count == 1, "Delayed retry created duplicate"


# ── Test 32: Commit Durability ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_commit_before_response(admin_conn, client):
    seed = await seed_webhook_prereqs(admin_conn)
    order_id = f"durable_{uuid.uuid4().hex[:8]}"

    resp = client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(seed["merchant_id"], events=[make_order_event(order_id)]),
        headers=_auth(),
    )
    assert resp.status_code == 200

    row = await admin_conn.fetchrow(
        "SELECT inbox_id, state FROM pos_event_inbox WHERE tenant_id=$1 AND vendor_event_id=$2",
        seed["tenant_id"],
        order_id,
    )
    assert row is not None, "Data not committed before response returned"
    assert row["state"] == "pending"


# ── Test 33: Multi-Merchant Partial Duplicate ─────────────────────────────────


@pytest.mark.asyncio
async def test_multi_merchant_partial_duplicate(admin_conn, client):
    seed_a = await seed_webhook_prereqs(admin_conn)
    seed_b = await seed_webhook_prereqs(admin_conn)
    order_a = f"mdup_a_{uuid.uuid4().hex[:6]}"
    order_b = f"mdup_b_{uuid.uuid4().hex[:6]}"
    ts = int(time.time() * 1000)

    # Pre-insert merchant A's event
    client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(
            seed_a["merchant_id"], events=[make_order_event(order_a, "CREATE", ts)]
        ),
        headers=_auth(),
    )

    # Combined: duplicate for A + new for B
    payload = {
        "appId": "TEST",
        "merchants": {
            seed_a["merchant_id"]: [make_order_event(order_a, "CREATE", ts)],  # duplicate
            seed_b["merchant_id"]: [make_order_event(order_b, "CREATE", ts)],  # new
        },
    }
    resp = client.post("/api/v1/webhooks/pos/clover", json=payload, headers=_auth())
    assert resp.status_code == 200

    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id=$1 AND vendor_event_id=$2",
            seed_a["tenant_id"],
            order_a,
        )
        == 1
    )

    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id=$1 AND vendor_event_id=$2",
            seed_b["tenant_id"],
            order_b,
        )
        == 1
    ), "Merchant B's event lost after A's duplicate"


# ── Test 34: ON CONFLICT Targets Full 5-Column Key ───────────────────────────


@pytest.mark.asyncio
async def test_on_conflict_targets_full_key(admin_conn, client):
    """CREATE + UPDATE same (order_id, ts) must both insert (event_type differs)."""
    seed = await seed_webhook_prereqs(admin_conn)
    order_id = f"conflict_{uuid.uuid4().hex[:8]}"
    ts = int(time.time() * 1000)

    client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(
            seed["merchant_id"], events=[make_order_event(order_id, "CREATE", ts)]
        ),
        headers=_auth(),
    )
    client.post(
        "/api/v1/webhooks/pos/clover",
        json=make_webhook_payload(
            seed["merchant_id"], events=[make_order_event(order_id, "UPDATE", ts)]
        ),
        headers=_auth(),
    )

    rows = await admin_conn.fetch(
        "SELECT vendor_event_type FROM pos_event_inbox WHERE tenant_id=$1 AND vendor_event_id=$2",
        seed["tenant_id"],
        order_id,
    )
    assert len(rows) == 2, f"Expected 2, got {len(rows)}"
    assert {r["vendor_event_type"] for r in rows} == {"CREATE", "UPDATE"}


# ── Test 35: Oversized Payload → No OOM/Timeout ───────────────────────────────


def test_oversized_payload_no_crash(client):
    # Giant objectId
    resp1 = client.post(
        "/api/v1/webhooks/pos/clover",
        json={
            "appId": "T",
            "merchants": {
                f"merch_{uuid.uuid4().hex[:8]}": [
                    {
                        "objectId": "O:" + "x" * 10000,
                        "type": "UPDATE",
                        "ts": int(time.time() * 1000),
                    }
                ]
            },
        },
        headers=_auth(),
    )
    assert resp1.status_code == 200

    # 100 merchants
    merchants = {}
    for i in range(100):
        merchants[f"merch_{i}_{uuid.uuid4().hex[:6]}"] = [
            make_order_event(f"dos_{i}_{uuid.uuid4().hex[:4]}")
        ]
    start = time.monotonic()
    resp2 = client.post(
        "/api/v1/webhooks/pos/clover",
        json={"appId": "T", "merchants": merchants},
        headers=_auth(),
    )
    elapsed_ms = (time.monotonic() - start) * 1000
    assert resp2.status_code == 200
    assert elapsed_ms < 10000


# ── Test 36: Session Recovery After Skip Path ─────────────────────────────────


@pytest.mark.asyncio
async def test_session_recovers_after_db_exception(admin_conn, client):
    """Events before and after an unknown-merchant skip both commit."""
    seed = await seed_webhook_prereqs(admin_conn)
    event_before = f"before_{uuid.uuid4().hex[:6]}"
    event_after = f"after_{uuid.uuid4().hex[:6]}"

    # Payload: valid → unknown (skip) — in one request
    payload = {
        "appId": "TEST",
        "merchants": {
            seed["merchant_id"]: [make_order_event(event_before)],
            f"UNKNOWN_{uuid.uuid4().hex[:8]}": [make_order_event(f"skip_{uuid.uuid4().hex[:6]}")],
        },
    }
    assert (
        client.post("/api/v1/webhooks/pos/clover", json=payload, headers=_auth()).status_code == 200
    )

    # Second request: same session lifecycle, event_after
    assert (
        client.post(
            "/api/v1/webhooks/pos/clover",
            json=make_webhook_payload(seed["merchant_id"], events=[make_order_event(event_after)]),
            headers=_auth(),
        ).status_code
        == 200
    )

    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id=$1 AND vendor_event_id=$2",
            seed["tenant_id"],
            event_before,
        )
        == 1
    ), "Event before skip lost"
    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id=$1 AND vendor_event_id=$2",
            seed["tenant_id"],
            event_after,
        )
        == 1
    ), "Event after skip lost — session poisoned"
