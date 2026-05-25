"""Phase 2 — 22 test functions: pos_event_inbox.

Fixtures:
  admin_conn   — superuser, autocommit, bypasses RLS (setup/teardown)
  service_conn — direct service_worker login, USING(true) RLS
  app_conn     — SET ROLE app_user, T1 RLS

All SET LOCAL statements are wrapped in asyncpg transaction() blocks
because SET LOCAL is transaction-scoped; outside a transaction it
has no effect on the next statement (autocommit = each statement is
its own single-statement transaction).
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from uuid6 import uuid7

from tests.helpers.inbox_seed import seed_inbox_prereqs
from tests.helpers.pos_inbox import insert_inbox, make_inbox_row, reconciliation_upsert

FUTURE_5M = datetime.now(UTC) + timedelta(minutes=5)
PAST_1M = datetime.now(UTC) - timedelta(minutes=1)


# ── Test 1: Table exists ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_table_exists(admin_conn):
    """pos_event_inbox table exists after migration.

    Why: If the migration failed (FK dependency, syntax error, wrong type),
    every subsequent phase that INSERTs events fails with 'relation does
    not exist' — a silent death.
    """
    result = await admin_conn.fetchval("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'pos_event_inbox'
        )
    """)
    assert result is True


# ── Test 2: vendor_ts is bigint NOT NULL ───────────────────────────────────────


@pytest.mark.asyncio
async def test_vendor_ts_rejects_null(admin_conn, service_conn):
    """vendor_ts = NULL is rejected.

    Why: vendor_ts is the 5th column in the dedup unique key. NULL values
    are not comparable in unique indexes (NULL != NULL), so two rows with
    vendor_ts=NULL would not conflict — making the uniqueness guarantee
    useless. A webhook handler bug that passes None for ts would silently
    allow infinite duplicate events.
    """
    seed = await seed_inbox_prereqs(admin_conn)
    row = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_ts": None,
        }
    )
    with pytest.raises(Exception):
        await insert_inbox(service_conn, row)


# ── Test 3: Multiple UPDATEs survive ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_updates_different_ts_both_survive(admin_conn, service_conn):
    """Three UPDATE events for the same order with different ts all insert.

    Why: Clover sends UPDATE at multiple lifecycle points for one order:
    ts=T1 (item added), ts=T2 (payment attached), ts=T3 (order locked).
    All three must be stored so the worker can process them in sequence.
    The 5th column (vendor_ts) distinguishes each UPDATE as a unique row.
    Without it, the 2nd UPDATE would conflict and be silently lost — the
    worker would never see the locked event and never create the orders row.
    """
    seed = await seed_inbox_prereqs(admin_conn)
    order_id = f"multi_update_{uuid.uuid4().hex[:8]}"
    base_ts = int(time.time() * 1000)

    for ts_offset in [0, 1000, 2000]:
        row = make_inbox_row(
            {
                "tenant_id": seed["tenant_id"],
                "connection_id": seed["connection_id"],
                "vendor_event_id": order_id,
                "vendor_event_type": "UPDATE",
                "vendor_ts": base_ts + ts_offset,
            }
        )
        await insert_inbox(service_conn, row)

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id = $1 AND vendor_event_id = $2",
        seed["tenant_id"],
        order_id,
    )
    assert count == 3


# ── Test 4: Exact duplicate rejected ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_exact_duplicate_rejected(admin_conn, service_conn):
    """Identical 5-column key raises UniqueViolationError.

    Why: A webhook delivered twice (Clover retries on 5xx) must not
    produce two inbox rows. The webhook handler catches UniqueViolationError
    and returns 200 — the event is already stored.
    """
    seed = await seed_inbox_prereqs(admin_conn)
    ts = int(time.time() * 1000)
    order_id = f"dup_{uuid.uuid4().hex[:8]}"

    row = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": order_id,
            "vendor_event_type": "CREATE",
            "vendor_ts": ts,
        }
    )
    await insert_inbox(service_conn, row)

    dupe = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": order_id,
            "vendor_event_type": "CREATE",
            "vendor_ts": ts,
        }
    )
    with pytest.raises(asyncpg.UniqueViolationError) as exc:
        await insert_inbox(service_conn, dupe)
    assert "pos_inbox_dedup_unique" in str(exc.value)


# ── Test 5: CREATE and UPDATE for same order coexist ──────────────────────────


@pytest.mark.asyncio
async def test_create_and_update_same_order(admin_conn, service_conn):
    """Different vendor_event_type = different 5-col key, even with same ts.

    Why: vendor_event_type is column 4 in the unique key. CREATE/UPDATE/DELETE
    for the same order at the same timestamp are three different events.
    Collapsing them would mean the worker could only ever see one event per
    (order, timestamp) — silently dropping the others.
    """
    seed = await seed_inbox_prereqs(admin_conn)
    ts = int(time.time() * 1000)
    order_id = f"lifecycle_{uuid.uuid4().hex[:8]}"

    for event_type in ["CREATE", "UPDATE", "DELETE"]:
        row = make_inbox_row(
            {
                "tenant_id": seed["tenant_id"],
                "connection_id": seed["connection_id"],
                "vendor_event_id": order_id,
                "vendor_event_type": event_type,
                "vendor_ts": ts,
            }
        )
        await insert_inbox(service_conn, row)

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM pos_event_inbox WHERE tenant_id = $1 AND vendor_event_id = $2",
        seed["tenant_id"],
        order_id,
    )
    assert count == 3


# ── Test 6a–e: CHECK constraints ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_vendor_event_type_rejects_invalid(admin_conn, service_conn):
    """'RECONCILIATION' was a bug in v3.1 spec. Only CREATE/UPDATE/DELETE."""
    seed = await seed_inbox_prereqs(admin_conn)
    row = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_type": "RECONCILIATION",
        }
    )
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await insert_inbox(service_conn, row)
    assert "pos_inbox_event_type_valid" in str(exc.value)


@pytest.mark.asyncio
async def test_check_vendor_object_type_rejects_invalid(admin_conn, service_conn):
    """'X' is not a valid object type. Only O, P, I, C, M, E, A."""
    seed = await seed_inbox_prereqs(admin_conn)
    row = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_object_type": "X",
        }
    )
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await insert_inbox(service_conn, row)
    assert "pos_inbox_object_type_valid" in str(exc.value)


@pytest.mark.asyncio
async def test_check_state_rejects_invalid_via_update(admin_conn, service_conn):
    """'claimed' is not a valid state. Checked via UPDATE so insert succeeds first.

    Why: state defaults to 'pending'. This test proves the CHECK fires on
    UPDATE too — not just INSERT. The worker's state machine uses UPDATE to
    transition state; if the CHECK only fired on INSERT, invalid states
    could be written via UPDATE.
    """
    seed = await seed_inbox_prereqs(admin_conn)
    row = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
        }
    )
    await insert_inbox(service_conn, row)

    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await service_conn.execute(
            "UPDATE pos_event_inbox SET state = 'claimed' WHERE inbox_id = $1",
            row["inbox_id"],
        )
    assert "pos_inbox_state_valid" in str(exc.value)


@pytest.mark.asyncio
async def test_check_source_rejects_invalid(admin_conn, service_conn):
    """'api' is not a valid source. Only webhook, reconciliation, manual."""
    seed = await seed_inbox_prereqs(admin_conn)
    row = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "source": "api",
        }
    )
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await insert_inbox(service_conn, row)
    assert "pos_inbox_source_valid" in str(exc.value)


@pytest.mark.asyncio
async def test_check_retry_count_nonnegative(admin_conn, service_conn):
    """retry_count = -1 is rejected. Checked via UPDATE.

    Why: The worker increments retry_count. If it could go negative (via
    a bug that decrements instead of increments), the failure threshold
    check (>= 5) would never trigger — events would retry forever.
    """
    seed = await seed_inbox_prereqs(admin_conn)
    row = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
        }
    )
    await insert_inbox(service_conn, row)

    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await service_conn.execute(
            "UPDATE pos_event_inbox SET retry_count = -1 WHERE inbox_id = $1",
            row["inbox_id"],
        )
    assert "pos_inbox_retry_nonneg" in str(exc.value)


# ── Test 7a–b: processed_consistency CHECK ────────────────────────────────────


@pytest.mark.asyncio
async def test_processed_consistency_rejects_null_timestamp(admin_conn, service_conn):
    """state='processed' with processed_at=NULL is rejected.

    Why: A worker bug that marks events done without recording when would
    leave processed_at NULL. Monitoring jobs that query 'how long did this
    take' would get NULL and silently fail. The constraint forces the worker
    to always record the completion timestamp.
    """
    seed = await seed_inbox_prereqs(admin_conn)
    row = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
        }
    )
    await insert_inbox(service_conn, row)

    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await service_conn.execute(
            "UPDATE pos_event_inbox SET state = 'processed', processed_at = NULL "
            "WHERE inbox_id = $1",
            row["inbox_id"],
        )
    assert "pos_inbox_processed_consistency" in str(exc.value)


@pytest.mark.asyncio
async def test_processed_consistency_allows_valid(admin_conn, service_conn):
    """state='processed' WITH processed_at set must succeed."""
    seed = await seed_inbox_prereqs(admin_conn)
    row = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
        }
    )
    await insert_inbox(service_conn, row)

    await service_conn.execute(
        "UPDATE pos_event_inbox SET state = 'processed', processed_at = now() WHERE inbox_id = $1",
        row["inbox_id"],
    )
    state = await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1",
        row["inbox_id"],
    )
    assert state == "processed"


# ── Test 8: Worker claim query ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_claim_query_finds_pending_and_stale(admin_conn, service_conn):
    """The spec's exact claim query finds pending events AND stale-claimed events.

    Why: The worker must handle two cases in one pass: (A) new events waiting
    to be processed, and (B) events whose previous worker crashed and left
    claim_expires_at in the past. Testing each path in isolation misses bugs
    in the combined OR clause — e.g., if the OR is accidentally AND.
    """
    seed = await seed_inbox_prereqs(admin_conn)

    # A: pending event ready for pickup
    event_pending = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": f"pending_{uuid.uuid4().hex[:8]}",
        }
    )
    await insert_inbox(service_conn, event_pending)

    # B: stale processing event (worker A crashed — claim_expires_at is past)
    event_stale = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": f"stale_{uuid.uuid4().hex[:8]}",
        }
    )
    await insert_inbox(service_conn, event_stale)
    await service_conn.execute(
        "UPDATE pos_event_inbox SET state = 'processing', "
        "claim_expires_at = $1, processing_started_at = now() "
        "WHERE inbox_id = $2",
        PAST_1M,
        event_stale["inbox_id"],
    )

    # C: failed event with past next_attempt_at — NOT in the claim query
    # (failed events are reset to pending by a separate sweep)
    event_not_ready = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": f"notready_{uuid.uuid4().hex[:8]}",
        }
    )
    await insert_inbox(service_conn, event_not_ready)
    await service_conn.execute(
        "UPDATE pos_event_inbox SET state = 'failed', retry_count = 1, "
        "next_attempt_at = $1 WHERE inbox_id = $2",
        FUTURE_5M,
        event_not_ready["inbox_id"],
    )

    # Run the spec's claim query scoped to this test's tenant.
    # Global LIMIT 10 would be consumed by pending events from earlier tests
    # before reaching this test's events. Tenant scoping is correct: in
    # production each worker pod is assigned a tenant partition anyway.
    claimed = await service_conn.fetch(
        """
        UPDATE pos_event_inbox
        SET state = 'processing',
            processing_started_at = now(),
            claim_expires_at = $1
        WHERE inbox_id IN (
            SELECT inbox_id FROM pos_event_inbox
            WHERE tenant_id = $2
              AND (
                (state = 'pending'
                 AND (next_attempt_at IS NULL OR next_attempt_at <= now()))
                OR
                (state = 'processing' AND claim_expires_at < now())
              )
            AND vendor_object_type = 'O'
            ORDER BY received_at ASC
            LIMIT 10
            FOR UPDATE SKIP LOCKED
        )
        RETURNING inbox_id
    """,
        FUTURE_5M,
        seed["tenant_id"],
    )

    claimed_ids = {str(r["inbox_id"]) for r in claimed}

    assert event_pending["inbox_id"] in claimed_ids, "Pending event was not claimed"
    assert event_stale["inbox_id"] in claimed_ids, "Stale event was not reclaimed"
    assert event_not_ready["inbox_id"] not in claimed_ids, (
        "Failed event with future next_attempt_at should not be claimed"
    )


# ── Test 9: claim_expires_at cleared after processing ─────────────────────────


@pytest.mark.asyncio
async def test_claim_expires_cleared_after_processing(admin_conn, service_conn):
    """claim_expires_at is NULL after processing.

    Why: If claim_expires_at is left set after processing, a subsequent
    stale-claim recovery query (WHERE state='processing' AND claim_expires_at
    < now()) would never match this row (state is 'processed'). But it is
    still dirty data — operational dashboards that check claim_expires_at
    would show confusing values. The worker's mark_processed() must clear it.
    """
    seed = await seed_inbox_prereqs(admin_conn)
    row = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
        }
    )
    await insert_inbox(service_conn, row)

    await service_conn.execute(
        "UPDATE pos_event_inbox SET state = 'processing', "
        "claim_expires_at = $1 WHERE inbox_id = $2",
        FUTURE_5M,
        row["inbox_id"],
    )
    await service_conn.execute(
        "UPDATE pos_event_inbox SET state = 'processed', "
        "processed_at = now(), claim_expires_at = NULL WHERE inbox_id = $1",
        row["inbox_id"],
    )

    claim_exp = await admin_conn.fetchval(
        "SELECT claim_expires_at FROM pos_event_inbox WHERE inbox_id = $1",
        row["inbox_id"],
    )
    assert claim_exp is None


# ── Tests 10a–d: Reconciliation ON CONFLICT behaviour ─────────────────────────


@pytest.mark.asyncio
async def test_reconciliation_requeues_processed_event(admin_conn, service_conn):
    """Reconciliation updates fetched_payload and requeues processed → pending.

    Why: Reconciliation runs nightly and hits the same 5-column key when
    Clover's modifiedTime matches an existing webhook ts (rare). The
    ON CONFLICT clause must requeue 'processed' events in case the order
    was subsequently refunded. raw_payload must NOT change — only
    fetched_payload (the fresh Clover order data) is updated.
    """
    seed = await seed_inbox_prereqs(admin_conn)
    order_id = f"recon_proc_{uuid.uuid4().hex[:8]}"
    ts = int(time.time() * 1000)

    # vendor_event_type MUST be "UPDATE" — reconciliation_upsert hardcodes
    # vendor_event_type='UPDATE' in its INSERT. The 5-column unique key
    # includes vendor_event_type, so the event and the reconciliation row
    # must share the same type to trigger ON CONFLICT.
    row = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": order_id,
            "vendor_event_type": "UPDATE",
            "vendor_ts": ts,
            "raw_payload": json.dumps({"original": True}),
        }
    )
    await insert_inbox(service_conn, row)
    await service_conn.execute(
        "UPDATE pos_event_inbox SET state = 'processed', processed_at = now() WHERE inbox_id = $1",
        row["inbox_id"],
    )

    await reconciliation_upsert(
        service_conn,
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": order_id,
            "vendor_ts": ts,
            "fetched_payload": json.dumps({"full_order": True, "state": "locked"}),
        },
    )

    result = await admin_conn.fetchrow(
        "SELECT state, raw_payload, fetched_payload, next_attempt_at "
        "FROM pos_event_inbox WHERE inbox_id = $1",
        row["inbox_id"],
    )

    assert result["state"] == "pending"
    # asyncpg may return JSONB as a string — parse defensively
    fetched = (
        json.loads(result["fetched_payload"])
        if isinstance(result["fetched_payload"], str)
        else result["fetched_payload"]
    )
    raw = (
        json.loads(result["raw_payload"])
        if isinstance(result["raw_payload"], str)
        else result["raw_payload"]
    )
    assert fetched["full_order"] is True
    assert raw["original"] is True  # raw_payload untouched by ON CONFLICT
    assert result["next_attempt_at"] is not None


@pytest.mark.asyncio
async def test_reconciliation_leaves_pending_alone(admin_conn, service_conn):
    """Reconciliation updates fetched_payload but does NOT change pending state.

    Why: The CASE clause in DO UPDATE only changes 'processed'/'dead_letter'.
    A 'pending' event is already queued — resetting its next_attempt_at to
    now() would jump the queue ahead of other events unfairly.
    """
    seed = await seed_inbox_prereqs(admin_conn)
    order_id = f"recon_pend_{uuid.uuid4().hex[:8]}"
    ts = int(time.time() * 1000)

    row = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": order_id,
            "vendor_event_type": "UPDATE",  # must match reconciliation's hardcoded type
            "vendor_ts": ts,
        }
    )
    await insert_inbox(service_conn, row)

    await reconciliation_upsert(
        service_conn,
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": order_id,
            "vendor_ts": ts,
            "fetched_payload": json.dumps({"refreshed": True}),
        },
    )

    result = await admin_conn.fetchrow(
        "SELECT state, fetched_payload FROM pos_event_inbox WHERE inbox_id = $1",
        row["inbox_id"],
    )
    assert result["state"] == "pending"
    fetched = (
        json.loads(result["fetched_payload"])
        if isinstance(result["fetched_payload"], str)
        else result["fetched_payload"]
    )
    assert fetched["refreshed"] is True


@pytest.mark.asyncio
async def test_reconciliation_requeues_dead_letter(admin_conn, service_conn):
    """Dead-lettered events are requeued by reconciliation — a second chance.

    Why: A dead-lettered event means a sale may be lost. Reconciliation
    gives it one more chance by queuing it as 'pending' with fresh data.
    Without this, dead-lettered events would sit permanently — the only
    recovery would be manual database intervention.
    """
    seed = await seed_inbox_prereqs(admin_conn)
    order_id = f"recon_dl_{uuid.uuid4().hex[:8]}"
    ts = int(time.time() * 1000)

    row = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": order_id,
            "vendor_event_type": "UPDATE",  # must match reconciliation's hardcoded type
            "vendor_ts": ts,
        }
    )
    await insert_inbox(service_conn, row)
    await service_conn.execute(
        "UPDATE pos_event_inbox SET state = 'dead_letter', retry_count = 5 WHERE inbox_id = $1",
        row["inbox_id"],
    )

    await reconciliation_upsert(
        service_conn,
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": order_id,
            "vendor_ts": ts,
            "fetched_payload": json.dumps({"fixed_data": True}),
        },
    )

    state = await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1",
        row["inbox_id"],
    )
    assert state == "pending"


@pytest.mark.asyncio
async def test_reconciliation_leaves_failed_alone(admin_conn, service_conn):
    """Reconciliation does NOT requeue 'failed' events — backoff is preserved.

    Why: A failed event is waiting for its exponential backoff timer. If
    reconciliation reset it to 'pending' with next_attempt_at = now(), the
    worker would immediately re-process an event that just failed — bypassing
    the backoff and potentially hammering Clover with rapid retries on an
    already-failing operation. The CASE clause only touches 'processed' and
    'dead_letter'. fetched_payload IS still updated (fresh data for the retry).
    """
    seed = await seed_inbox_prereqs(admin_conn)
    order_id = f"recon_fail_{uuid.uuid4().hex[:8]}"
    ts = int(time.time() * 1000)

    row = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": order_id,
            "vendor_event_type": "UPDATE",  # must match reconciliation's hardcoded type
            "vendor_ts": ts,
        }
    )
    await insert_inbox(service_conn, row)

    future_backoff = datetime.now(UTC) + timedelta(minutes=8)
    await service_conn.execute(
        "UPDATE pos_event_inbox SET state = 'failed', retry_count = 3, "
        "next_attempt_at = $1, last_error = 'Clover 500' WHERE inbox_id = $2",
        future_backoff,
        row["inbox_id"],
    )

    await reconciliation_upsert(
        service_conn,
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": order_id,
            "vendor_ts": ts,
            "fetched_payload": json.dumps({"refreshed": True}),
        },
    )

    result = await admin_conn.fetchrow(
        "SELECT state, retry_count, next_attempt_at, fetched_payload "
        "FROM pos_event_inbox WHERE inbox_id = $1",
        row["inbox_id"],
    )

    assert result["state"] == "failed"
    assert result["retry_count"] == 3
    diff = abs((result["next_attempt_at"] - future_backoff).total_seconds())
    assert diff < 2, (
        f"next_attempt_at changed: expected ~{future_backoff}, got {result['next_attempt_at']}"
    )
    fetched = (
        json.loads(result["fetched_payload"])
        if isinstance(result["fetched_payload"], str)
        else result["fetched_payload"]
    )
    assert fetched["refreshed"] is True


# ── Test 11: RLS — app_user tenant isolation ──────────────────────────────────


@pytest.mark.asyncio
async def test_rls_app_user_isolation(admin_conn, app_conn, service_conn):
    """app_user with tenant context sees only its rows; no context → zero rows.

    Why: POS event metadata is tenant-private. A cross-tenant data leak
    would expose which Clover orders a competitor is processing. The
    NULLIF guard prevents ''::uuid crash when context is cleared.
    """
    seed_a = await seed_inbox_prereqs(admin_conn)
    seed_b = await seed_inbox_prereqs(admin_conn)

    row_a = make_inbox_row(
        {"tenant_id": seed_a["tenant_id"], "connection_id": seed_a["connection_id"]}
    )
    row_b = make_inbox_row(
        {"tenant_id": seed_b["tenant_id"], "connection_id": seed_b["connection_id"]}
    )
    await insert_inbox(service_conn, row_a)
    await insert_inbox(service_conn, row_b)

    # With tenant-A context: sees only tenant-A
    # asyncpg transaction() keeps SET LOCAL in scope for the block
    async with app_conn.transaction():
        await app_conn.execute(f"SET LOCAL app.tenant_id = '{seed_a['tenant_id']}'")
        rows = await app_conn.fetch(
            "SELECT inbox_id::text FROM pos_event_inbox WHERE inbox_id = ANY($1::uuid[])",
            [row_a["inbox_id"], row_b["inbox_id"]],
        )
        ids = {r["inbox_id"] for r in rows}
        assert row_a["inbox_id"] in ids
        assert row_b["inbox_id"] not in ids

    # No context: fail-safe → zero rows (not a crash)
    async with app_conn.transaction():
        rows_empty = await app_conn.fetch(
            "SELECT inbox_id FROM pos_event_inbox WHERE inbox_id = ANY($1::uuid[])",
            [row_a["inbox_id"], row_b["inbox_id"]],
        )
        assert len(rows_empty) == 0


# ── Tests 12a–b: service_worker cross-tenant + no DELETE ──────────────────────


@pytest.mark.asyncio
async def test_rls_service_worker_cross_tenant(admin_conn, service_conn):
    """service_worker sees all tenants without setting app.tenant_id.

    Why: The worker claims events across all tenants. T1 RLS would require
    the worker to know which tenants exist and loop through them — O(n)
    roundtrips instead of one global scan.
    """
    seed_a = await seed_inbox_prereqs(admin_conn)
    seed_b = await seed_inbox_prereqs(admin_conn)

    row_a = make_inbox_row(
        {"tenant_id": seed_a["tenant_id"], "connection_id": seed_a["connection_id"]}
    )
    row_b = make_inbox_row(
        {"tenant_id": seed_b["tenant_id"], "connection_id": seed_b["connection_id"]}
    )
    await insert_inbox(service_conn, row_a)
    await insert_inbox(service_conn, row_b)

    rows = await service_conn.fetch(
        "SELECT inbox_id::text FROM pos_event_inbox WHERE inbox_id = ANY($1::uuid[])",
        [row_a["inbox_id"], row_b["inbox_id"]],
    )
    ids = {r["inbox_id"] for r in rows}
    assert row_a["inbox_id"] in ids
    assert row_b["inbox_id"] in ids


@pytest.mark.asyncio
async def test_service_worker_cannot_delete(admin_conn, service_conn):
    """service_worker has SELECT/INSERT/UPDATE but NOT DELETE.

    Why: The inbox is append-only for audit — every Clover event, even
    dead-lettered ones, must be preserved for post-mortem analysis. If
    service_worker could DELETE, a bug in the worker could silently destroy
    the audit trail during error recovery.
    """
    seed = await seed_inbox_prereqs(admin_conn)
    row = make_inbox_row({"tenant_id": seed["tenant_id"], "connection_id": seed["connection_id"]})
    await insert_inbox(service_conn, row)

    with pytest.raises((asyncpg.InsufficientPrivilegeError, Exception)) as exc:
        await service_conn.execute(
            "DELETE FROM pos_event_inbox WHERE inbox_id = $1",
            row["inbox_id"],
        )
    assert "permission denied" in str(exc.value).lower() or "insufficient" in str(exc.value).lower()


# ── Test 13: app_user cannot INSERT ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_app_user_cannot_insert(admin_conn, app_conn):
    """app_user has SELECT only on pos_event_inbox — INSERT is denied.

    Why: Webhook inserts run as service_worker. If app_user could INSERT,
    a compromised tenant session could inject fake POS events — fake sales
    that deplete inventory for items never actually sold.
    """
    seed = await seed_inbox_prereqs(admin_conn)
    row = make_inbox_row({"tenant_id": seed["tenant_id"], "connection_id": seed["connection_id"]})

    async with app_conn.transaction():
        await app_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        with pytest.raises((asyncpg.InsufficientPrivilegeError, Exception)) as exc:
            await insert_inbox(app_conn, row)
        assert (
            "permission denied" in str(exc.value).lower()
            or "insufficient" in str(exc.value).lower()
        )


# ── Test 14: UUIDv7 primary key ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uuidv7_pk_accepted(admin_conn, service_conn):
    """PostgreSQL accepts a UUIDv7 value as inbox_id primary key."""
    seed = await seed_inbox_prereqs(admin_conn)
    v7_id = str(uuid7())
    row = make_inbox_row(
        {
            "inbox_id": v7_id,
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
        }
    )
    await insert_inbox(service_conn, row)

    stored = await admin_conn.fetchval(
        "SELECT inbox_id::text FROM pos_event_inbox WHERE inbox_id = $1::uuid",
        v7_id,
    )
    assert stored == v7_id
    assert v7_id[14] == "7"


# ── Test 15: Full inbox lifecycle ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_inbox_lifecycle(admin_conn, app_conn, service_conn):
    """Complete Clover order lifecycle through the inbox.

    Sequence:
    1. Webhook: order CREATE → pending
    2. Worker claims → processing
    3. Worker processes → processed (claim_expires_at cleared)
    4. Webhook: order UPDATE (different ts) → new pending row
    5. Worker processes UPDATE → processed
    6. Exact duplicate webhook → rejected by 5-column unique
    7. New event fails 5× → dead_letter
    8. Reconciliation requeues dead_letter → pending (fresh fetched_payload)
    9. Worker processes requeued event → processed
    10. Stale claim: event stuck in processing → reclaimed → processed
    11. RLS: app_user sees own tenant, service_worker sees all

    Why: Each guard works alone. This test proves they compose correctly.
    The real failure mode is a guard that works in isolation but breaks
    when preceded by another state transition.
    """
    seed = await seed_inbox_prereqs(admin_conn)
    order_id = f"lifecycle_{uuid.uuid4().hex[:8]}"
    base_ts = int(time.time() * 1000)

    # ── 1. WEBHOOK: ORDER CREATE ─────────────────────────────────────────────
    evt_create = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": order_id,
            "vendor_event_type": "CREATE",
            "vendor_ts": base_ts,
        }
    )
    await insert_inbox(service_conn, evt_create)
    state = await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1",
        evt_create["inbox_id"],
    )
    assert state == "pending"

    # ── 2. WORKER CLAIMS ─────────────────────────────────────────────────────
    await service_conn.execute(
        "UPDATE pos_event_inbox SET state = 'processing', "
        "processing_started_at = now(), claim_expires_at = $1 WHERE inbox_id = $2",
        FUTURE_5M,
        evt_create["inbox_id"],
    )

    # ── 3. WORKER PROCESSES ──────────────────────────────────────────────────
    await service_conn.execute(
        "UPDATE pos_event_inbox SET state = 'processed', "
        "processed_at = now(), claim_expires_at = NULL WHERE inbox_id = $1",
        evt_create["inbox_id"],
    )
    result = await admin_conn.fetchrow(
        "SELECT state, claim_expires_at FROM pos_event_inbox WHERE inbox_id = $1",
        evt_create["inbox_id"],
    )
    assert result["state"] == "processed"
    assert result["claim_expires_at"] is None

    # ── 4. WEBHOOK: ORDER UPDATE (different ts) ───────────────────────────────
    evt_update = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": order_id,
            "vendor_event_type": "UPDATE",
            "vendor_ts": base_ts + 5000,
        }
    )
    await insert_inbox(service_conn, evt_update)

    # ── 5. WORKER PROCESSES UPDATE ────────────────────────────────────────────
    await service_conn.execute(
        "UPDATE pos_event_inbox SET state = 'processed', processed_at = now() WHERE inbox_id = $1",
        evt_update["inbox_id"],
    )

    # ── 6. EXACT DUPLICATE WEBHOOK REJECTED ───────────────────────────────────
    dupe = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": order_id,
            "vendor_event_type": "CREATE",
            "vendor_ts": base_ts,
        }
    )
    with pytest.raises(asyncpg.UniqueViolationError) as exc:
        await insert_inbox(service_conn, dupe)
    assert "pos_inbox_dedup_unique" in str(exc.value)

    # ── 7. NEW EVENT FAILS 5 TIMES → DEAD LETTER ─────────────────────────────
    evt_fail = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": f"fail_{uuid.uuid4().hex[:8]}",
            "vendor_event_type": "UPDATE",  # reconciliation_upsert uses 'UPDATE'
            "vendor_ts": base_ts + 10000,
        }
    )
    await insert_inbox(service_conn, evt_fail)

    for attempt in range(1, 6):
        await service_conn.execute(
            "UPDATE pos_event_inbox SET state = 'processing', claim_expires_at = $1 "
            "WHERE inbox_id = $2",
            FUTURE_5M,
            evt_fail["inbox_id"],
        )
        new_state = "dead_letter" if attempt >= 5 else "failed"
        backoff = datetime.now(UTC) + timedelta(minutes=2 ** (attempt - 1))
        await service_conn.execute(
            "UPDATE pos_event_inbox SET state = $1, retry_count = $2, "
            "next_attempt_at = $3, last_error = $4, claim_expires_at = NULL "
            "WHERE inbox_id = $5",
            new_state,
            attempt,
            backoff if new_state == "failed" else None,
            "simulated failure",
            evt_fail["inbox_id"],
        )

    assert (
        await admin_conn.fetchval(
            "SELECT state FROM pos_event_inbox WHERE inbox_id = $1",
            evt_fail["inbox_id"],
        )
        == "dead_letter"
    )

    # ── 8. RECONCILIATION REQUEUES DEAD LETTER ────────────────────────────────
    await reconciliation_upsert(
        service_conn,
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": evt_fail["vendor_event_id"],
            "vendor_ts": evt_fail["vendor_ts"],
            "fetched_payload": json.dumps({"recovered": True}),
        },
    )
    requeued = await admin_conn.fetchrow(
        "SELECT state, fetched_payload FROM pos_event_inbox WHERE inbox_id = $1",
        evt_fail["inbox_id"],
    )
    assert requeued["state"] == "pending"
    fp = (
        json.loads(requeued["fetched_payload"])
        if isinstance(requeued["fetched_payload"], str)
        else requeued["fetched_payload"]
    )
    assert fp["recovered"] is True

    # ── 9. WORKER PROCESSES REQUEUED EVENT ────────────────────────────────────
    await service_conn.execute(
        "UPDATE pos_event_inbox SET state = 'processed', processed_at = now() WHERE inbox_id = $1",
        evt_fail["inbox_id"],
    )
    assert (
        await admin_conn.fetchval(
            "SELECT state FROM pos_event_inbox WHERE inbox_id = $1",
            evt_fail["inbox_id"],
        )
        == "processed"
    )

    # ── 10. STALE CLAIM RECOVERY ──────────────────────────────────────────────
    evt_stale = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": f"stale_{uuid.uuid4().hex[:8]}",
            "vendor_ts": base_ts + 20000,
        }
    )
    await insert_inbox(service_conn, evt_stale)
    await service_conn.execute(
        "UPDATE pos_event_inbox SET state = 'processing', "
        "claim_expires_at = $1, processing_started_at = now() WHERE inbox_id = $2",
        PAST_1M,
        evt_stale["inbox_id"],
    )

    reclaimed = await service_conn.fetch(
        """
        UPDATE pos_event_inbox
        SET state = 'processing', processing_started_at = now(), claim_expires_at = $1
        WHERE inbox_id IN (
            SELECT inbox_id FROM pos_event_inbox
            WHERE tenant_id = $2
              AND (
                (state = 'pending'
                 AND (next_attempt_at IS NULL OR next_attempt_at <= now()))
                OR
                (state = 'processing' AND claim_expires_at < now())
              )
            AND vendor_object_type = 'O'
            ORDER BY received_at ASC
            LIMIT 10
            FOR UPDATE SKIP LOCKED
        )
        RETURNING inbox_id
    """,
        FUTURE_5M,
        seed["tenant_id"],
    )

    reclaimed_ids = {str(r["inbox_id"]) for r in reclaimed}
    assert evt_stale["inbox_id"] in reclaimed_ids

    await service_conn.execute(
        "UPDATE pos_event_inbox SET state = 'processed', "
        "processed_at = now(), claim_expires_at = NULL WHERE inbox_id = $1",
        evt_stale["inbox_id"],
    )

    # ── 11. RLS ISOLATION ─────────────────────────────────────────────────────
    async with app_conn.transaction():
        await app_conn.execute(f"SET LOCAL app.tenant_id = '{seed['tenant_id']}'")
        app_rows = await app_conn.fetch(
            "SELECT inbox_id::text FROM pos_event_inbox WHERE inbox_id = ANY($1::uuid[])",
            [
                evt_create["inbox_id"],
                evt_update["inbox_id"],
                evt_fail["inbox_id"],
                evt_stale["inbox_id"],
            ],
        )
        app_ids = {r["inbox_id"] for r in app_rows}
        assert evt_create["inbox_id"] in app_ids
        assert evt_update["inbox_id"] in app_ids

    svc_rows = await service_conn.fetch(
        "SELECT inbox_id::text FROM pos_event_inbox WHERE inbox_id = ANY($1::uuid[])",
        [
            evt_create["inbox_id"],
            evt_update["inbox_id"],
            evt_fail["inbox_id"],
            evt_stale["inbox_id"],
        ],
    )
    assert len(svc_rows) == 4
