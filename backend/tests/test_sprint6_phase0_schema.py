"""Sprint 6 Phase 0 — schema proof for migrations 0023 (additive) + 0024 (tighten).

Proves the receipts/extraction schema actually materialized correctly and behaves
under the REAL Postgres roles — not just that the migration "ran". Splits into:

  * structural assertions (deterministic, no seeding) — tables exist + FORCE'd,
    columns + nullability, widened CHECK, the job-ordinal unique index, and that
    every worker-claim table carries BOTH an app_user _T1 policy and a
    service_worker USING(true) policy;
  * behavioral assertions under app_user / service_worker — tenant isolation
    (app_user sees only its tenant; service_worker sees across tenants), the
    review-visibility CHECK, the source CHECK, and the single-active-channel UNIQUE.

The integrity TRIGGER (trg_receipt_commit_integrity, D-606-05) is intentionally
NOT here — it ships with the Phase-4 commit upgrade (it requires confirmed_at +
affirmation that the pre-Phase-4 commit_receipt does not set). Its behavioral tests
live in the Phase-4 suite. See 0024's module docstring.

Requires a live Postgres at head 0024 (the integration DB). Uses the same
admin_conn / app_conn / service_conn fixtures as test_rls.py.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.conftest import seed_tenant

pytestmark = pytest.mark.integration


# The 10 new tenant tables created by 0023.
_NEW_TABLES = [
    "inbound_email_inbox",
    "receipt_extraction_jobs",
    "inbound_email_attachments",
    "tenant_extraction_rate_limits",
    "tenant_inbound_email_tokens",
    "tenant_inbound_webhook_tokens",
    "tenant_gmail_connections",
    "tenant_invoice_senders",
    "tenant_active_email_channel",
    "receipt_adjustments",
]

# Worker-claim tables: must carry a cross-tenant service_worker USING(true) policy
# AND an app_user tenant-scoped policy (the dual-policy posture that lets us FORCE
# every table while the worker still claims across tenants).
_WORKER_CLAIM_TABLES = [
    "inbound_email_inbox",
    "receipt_extraction_jobs",
    "inbound_email_attachments",
    "tenant_extraction_rate_limits",
]


# ── structural ────────────────────────────────────────────────────────────────


async def test_all_new_tables_exist(admin_conn: Any) -> None:
    rows = await admin_conn.fetch(
        "SELECT relname FROM pg_class WHERE relkind='r' AND relname = ANY($1::text[])",
        _NEW_TABLES,
    )
    found = {r["relname"] for r in rows}
    assert found == set(_NEW_TABLES), f"missing Sprint 6 tables: {set(_NEW_TABLES) - found}"


async def test_all_new_tables_are_force_rls(admin_conn: Any) -> None:
    """Every new tenant table must be ENABLE + FORCE (or the request-path owner
    bypasses its policy, and the test_every_tenant_table_is_force_rls guard fails)."""
    rows = await admin_conn.fetch(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relname = ANY($1::text[])",
        _NEW_TABLES,
    )
    not_forced = [
        r["relname"] for r in rows if not (r["relrowsecurity"] and r["relforcerowsecurity"])
    ]
    assert not not_forced, f"new tenant tables not ENABLE+FORCE: {sorted(not_forced)}"


async def test_worker_tables_have_both_policies(admin_conn: Any) -> None:
    """Worker-claim tables carry an app_user (_T1) policy AND a service_worker
    USING(true) policy — the dual-policy design (FORCE binds owner only)."""
    for table in _WORKER_CLAIM_TABLES:
        pols = await admin_conn.fetch(
            """
            SELECT polname,
                   pg_get_expr(polqual, polrelid) AS using_expr,
                   (SELECT array_agg(pg_get_userbyid(r)) FROM unnest(polroles) r) AS roles
            FROM pg_policy WHERE polrelid = $1::regclass
            """,
            table,
        )
        by_role: dict[str, str] = {}
        for p in pols:
            for role in p["roles"] or []:
                by_role[role] = p["using_expr"] or ""
        assert "service_worker" in by_role, f"{table}: no service_worker policy"
        assert by_role["service_worker"].strip() == "true", (
            f"{table}: service_worker policy must be USING(true), got {by_role['service_worker']!r}"
        )
        assert "app_user" in by_role, f"{table}: no app_user policy"
        assert "app.tenant_id" in by_role["app_user"], (
            f"{table}: app_user policy must be tenant-scoped, got {by_role['app_user']!r}"
        )


async def test_receipts_new_columns_and_types(admin_conn: Any) -> None:
    cols = {
        r["column_name"]: (r["data_type"], r["is_nullable"])
        for r in await admin_conn.fetch(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'receipts'"
        )
    }
    # notes_log is the NEW jsonb column; the legacy TEXT notes is untouched.
    assert cols["notes_log"][0] == "jsonb"
    assert cols["notes"][0] == "text"
    # source is NOT NULL after 0024.
    assert cols["source"][1] == "NO", "receipts.source must be NOT NULL after 0024"
    for c in (
        "photo_object_key",
        "invoice_number",
        "subtotal_cents",
        "extraction_status",
        "extraction_confidence",
        "review_visibility_status",
        "suppression_reason",
        "quota_blocked",
        "confirmed_at",
        "reviewed_affirmation",
        "review_started_at",
        "manual_entry_required",
        "inbound_email_id",
        "filter_flags",
    ):
        assert c in cols, f"receipts missing column {c}"


async def test_receipt_lines_item_nullable_and_new_columns(admin_conn: Any) -> None:
    cols = {
        r["column_name"]: r["is_nullable"]
        for r in await admin_conn.fetch(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'receipt_lines'"
        )
    }
    assert cols["inventory_item_id"] == "YES", "inventory_item_id must be nullable after 0024"
    for c in (
        "extracted_name",
        "confidence",
        "manually_corrected",
        "match_status",
        "extraction_job_id",
        "job_attempt",
        "line_ordinal",
    ):
        assert c in cols, f"receipt_lines missing column {c}"


async def test_commit_state_check_widened(admin_conn: Any) -> None:
    definition = await admin_conn.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'receipts_commit_state_check'"
    )
    for state in ("draft", "pending_review", "committed", "dismissed", "cancelled"):
        assert state in definition, f"commit_state CHECK missing {state}: {definition}"


async def test_job_ordinal_unique_index_exists(admin_conn: Any) -> None:
    idx = await admin_conn.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_receipt_lines_job_ordinal'"
    )
    assert idx is not None and "UNIQUE" in idx
    assert "receipt_id" in idx and "extraction_job_id" in idx and "line_ordinal" in idx


# ── behavioral (real roles / constraints) ─────────────────────────────────────


async def test_app_user_sees_only_own_tenant_rows(admin_conn: Any, app_conn: Any) -> None:
    """app_user under tenant A cannot see tenant B's invoice-sender rows (RLS)."""
    tenant_a = await seed_tenant(admin_conn)
    tenant_b = await seed_tenant(admin_conn)
    await admin_conn.execute(
        "INSERT INTO tenant_invoice_senders (tenant_id, match_type, match_value) "
        "VALUES ($1, 'domain', 'a.example.com'), ($2, 'domain', 'b.example.com')",
        tenant_a["id"],
        tenant_b["id"],
    )
    try:
        async with app_conn.transaction():
            await app_conn.execute(f"SET LOCAL app.tenant_id = '{tenant_a['id']}'")
            await app_conn.execute("SET LOCAL app.rls_mode = ''")
            rows = await app_conn.fetch("SELECT tenant_id FROM tenant_invoice_senders")
        seen = {str(r["tenant_id"]) for r in rows}
        assert str(tenant_a["id"]) in seen
        assert str(tenant_b["id"]) not in seen
    finally:
        await admin_conn.execute(
            "DELETE FROM tenant_invoice_senders WHERE tenant_id IN ($1, $2)",
            tenant_a["id"],
            tenant_b["id"],
        )
        await admin_conn.execute(
            "DELETE FROM tenants WHERE id IN ($1, $2)", tenant_a["id"], tenant_b["id"]
        )


async def test_service_worker_sees_across_tenants(admin_conn: Any, service_conn: Any) -> None:
    """service_worker (USING(true)) must see BOTH tenants' inbox rows — the
    webhook/worker resolves merchant→tenant by reading cross-tenant."""
    tenant_a = await seed_tenant(admin_conn)
    tenant_b = await seed_tenant(admin_conn)
    await admin_conn.execute(
        "INSERT INTO tenant_invoice_senders (tenant_id, match_type, match_value) "
        "VALUES ($1, 'domain', 'a.example.com'), ($2, 'domain', 'b.example.com')",
        tenant_a["id"],
        tenant_b["id"],
    )
    try:
        rows = await service_conn.fetch(
            "SELECT tenant_id FROM tenant_invoice_senders WHERE tenant_id = ANY($1::uuid[])",
            [tenant_a["id"], tenant_b["id"]],
        )
        seen = {str(r["tenant_id"]) for r in rows}
        assert str(tenant_a["id"]) in seen and str(tenant_b["id"]) in seen
    finally:
        await admin_conn.execute(
            "DELETE FROM tenant_invoice_senders WHERE tenant_id IN ($1, $2)",
            tenant_a["id"],
            tenant_b["id"],
        )
        await admin_conn.execute(
            "DELETE FROM tenants WHERE id IN ($1, $2)", tenant_a["id"], tenant_b["id"]
        )


async def test_active_email_channel_unique_rejects_second(admin_conn: Any) -> None:
    """D-606-18: UNIQUE(tenant_id) makes a second active channel impossible."""
    import asyncpg

    tenant = await seed_tenant(admin_conn)
    await admin_conn.execute(
        "INSERT INTO tenant_active_email_channel (tenant_id, channel_type, channel_ref) "
        "VALUES ($1, 'gmail', $2)",
        tenant["id"],
        uuid.uuid4(),
    )
    try:
        with pytest.raises(asyncpg.UniqueViolationError):
            await admin_conn.execute(
                "INSERT INTO tenant_active_email_channel (tenant_id, channel_type, channel_ref) "
                "VALUES ($1, 'postmark', $2)",
                tenant["id"],
                uuid.uuid4(),
            )
    finally:
        await admin_conn.execute(
            "DELETE FROM tenant_active_email_channel WHERE tenant_id = $1", tenant["id"]
        )
        await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


async def test_review_visibility_check_rejects_illegal_combo(admin_conn: Any) -> None:
    """suppressed must pair with suppression_reason='not_invoice'; visible must
    pair with NULL reason (receipts_review_visibility_reason_check)."""
    import asyncpg

    tenant = await seed_tenant(admin_conn)
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await admin_conn.execute(
                "INSERT INTO receipts (tenant_id, source, review_visibility_status, suppression_reason) "
                "VALUES ($1, 'manual', 'suppressed', NULL)",
                tenant["id"],
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await admin_conn.execute(
                "INSERT INTO receipts (tenant_id, source, review_visibility_status, suppression_reason) "
                "VALUES ($1, 'manual', 'visible', 'not_invoice')",
                tenant["id"],
            )
    finally:
        await admin_conn.execute("DELETE FROM receipts WHERE tenant_id = $1", tenant["id"])
        await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


async def test_source_check_rejects_unknown_value(admin_conn: Any) -> None:
    """source is a fail-closed enum — an unknown value cannot be written."""
    import asyncpg

    tenant = await seed_tenant(admin_conn)
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await admin_conn.execute(
                "INSERT INTO receipts (tenant_id, source) VALUES ($1, 'clover')",
                tenant["id"],
            )
    finally:
        await admin_conn.execute("DELETE FROM receipts WHERE tenant_id = $1", tenant["id"])
        await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
