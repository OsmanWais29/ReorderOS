"""Sprint 5 Phase 13 — coverage view verification + monitoring diagnostics.

Design: backend/docs/sprints/sprint-5-phase-13-notes.md. A verification phase — the
vw_depletion_coverage view shipped in 0017; this proves the properties that were load-bearing
in that review, plus the op-concern-6 refund-pattern diagnostic. No migration, no new behavior.

  N1 — coverage three-state matrix (gate 30): each state fails a DIFFERENT wrong view —
       zero-depleted/nonzero-total → 0.00 (not NULL); count% ≠ revenue% (independent columns);
       zero-total → NULL (honest no-denominator).
  N2 — RLS: the security_invoker view scopes to the querying tenant (cross-tenant revenue
       visibility is the regression this permanently guards).
  N3 — failed_reason_breakdown: NULL reason → 'unknown' (surface the impossible state), and
       windowed on created_at (op-concern 6 is about spikes).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.core.database import engine, make_bound_session
from app.modules.inventory.depletion import diagnostics

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = make_bound_session(conn)
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()


async def _seed_lines(db: AsyncSession, lines: list[tuple[str, str | None, int, int]]) -> str:
    """Seed a tenant + inbox + order and one sale_line_item per
    (status, reason, revenue_cents, age_days). status↔reason must satisfy the 0016 CHECK.
    Returns the tenant id."""
    tid = str(uuid7())
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:t,'T',:s)"),
        {"t": tid, "s": f"t-{uuid.uuid4().hex[:8]}"},
    )
    inbox_id = str(uuid.uuid4())
    await db.execute(
        text("""
            INSERT INTO pos_event_inbox
            (inbox_id, tenant_id, vendor, vendor_event_id, vendor_object_type,
             vendor_event_type, vendor_ts, raw_payload, signature_verified, source)
            VALUES (:iid,:t,'clover','O:p13','O','UPDATE',0,'{}',false,'webhook')
        """),
        {"iid": inbox_id, "t": tid},
    )
    order_id = str(uuid.uuid4())
    await db.execute(
        text("""
            INSERT INTO orders
            (id, tenant_id, pos_event_inbox_id, clover_order_id, total_amount_cents,
             state, payment_state, processed_at)
            VALUES (:oid,:t,:iid,'p13_order',0,'locked','PAID',now())
        """),
        {"oid": order_id, "t": tid, "iid": inbox_id},
    )
    for status, reason, revenue, age_days in lines:
        await db.execute(
            text("""
                INSERT INTO sale_line_items
                (id, tenant_id, order_id, clover_line_item_id, name_at_sale, quantity,
                 price_cents_at_sale, net_revenue_cents, depletion_status, depletion_reason,
                 created_at)
                VALUES (:id,:t,:oid,:cli,'Item',1,0,:rev,:st,:rsn,
                        now() - make_interval(days => :age))
            """),
            {
                "id": str(uuid.uuid4()),
                "t": tid,
                "oid": order_id,
                "cli": f"cli_{uuid.uuid4().hex[:8]}",
                "rev": revenue,
                "st": status,
                "rsn": reason,
                "age": age_days,
            },
        )
    await db.flush()
    return tid


async def _coverage(db: AsyncSession, tid: str):
    return (
        (
            await db.execute(
                text("""
                SELECT depleted_count, total_count, depleted_count_pct,
                       depleted_revenue_cents, total_revenue_cents, depleted_revenue_pct
                FROM vw_depletion_coverage WHERE tenant_id = :t
            """),
                {"t": tid},
            )
        )
        .mappings()
        .fetchone()
    )


# ═══════════════════════════════════════════════════════════════════════════════
# N1 — the three-state coverage matrix (gate 30)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_coverage_zero_depleted_nonzero_total_reads_zero_not_null(db) -> None:
    """State 1 (alert-correctness): revenue present, ZERO depleted → 0.00, NOT NULL, so the
    `pct < threshold` collapse alert evaluates instead of going UNKNOWN."""
    tid = await _seed_lines(
        db,
        [
            ("failed", "sale_ineligible", 500, 0),
            ("unmapped", "no_recipe", 700, 0),
        ],
    )
    row = await _coverage(db, tid)
    assert row["depleted_count"] == 0
    assert Decimal(str(row["depleted_count_pct"])) == Decimal("0.00")
    assert row["total_revenue_cents"] == 1200
    assert row["depleted_revenue_pct"] is not None, "must be 0.00, not NULL"
    assert Decimal(str(row["depleted_revenue_pct"])) == Decimal("0.00")


@pytest.mark.integration
async def test_coverage_count_pct_differs_from_revenue_pct(db) -> None:
    """State 2 (independent columns / factor≠1 at the view layer): weighted so count share
    (50%) ≠ revenue share (40%) — a test that conflated the columns would fail."""
    tid = await _seed_lines(
        db,
        [
            ("depleted", None, 100, 0),
            ("depleted", None, 300, 0),
            ("failed", "sale_ineligible", 200, 0),
            ("failed", "line_refunded", 400, 0),
        ],
    )
    row = await _coverage(db, tid)
    assert row["depleted_count"] == 2 and row["total_count"] == 4
    assert Decimal(str(row["depleted_count_pct"])) == Decimal("50.00")  # 2/4
    assert row["depleted_revenue_cents"] == 400 and row["total_revenue_cents"] == 1000
    assert Decimal(str(row["depleted_revenue_pct"])) == Decimal("40.00")  # 400/1000
    assert row["depleted_count_pct"] != row["depleted_revenue_pct"]


@pytest.mark.integration
async def test_coverage_zero_total_revenue_is_null(db) -> None:
    """State 3 (honest no-denominator): all revenue 0 → total_revenue 0 → revenue_pct NULL,
    NOT a fake 0.00 or a divide-by-zero. Guards against an over-COALESCE on the denominator."""
    tid = await _seed_lines(
        db,
        [
            ("depleted", None, 0, 0),
            ("failed", "sale_ineligible", 0, 0),
        ],
    )
    row = await _coverage(db, tid)
    assert row["total_revenue_cents"] == 0
    assert row["depleted_revenue_pct"] is None, "no-denominator → NULL is the honest answer"
    # count side still computes (2 lines, 1 depleted)
    assert Decimal(str(row["depleted_count_pct"])) == Decimal("50.00")


# ═══════════════════════════════════════════════════════════════════════════════
# N2 — RLS: the security_invoker view scopes to the querying tenant
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_coverage_view_rls_scopes_to_tenant(db) -> None:
    """The view is security_invoker, so app_user sees only its own tenant's coverage row.
    Seed two tenants, query as app_user scoped to A → exactly A's row. Guards cross-tenant
    revenue visibility (the regression a future view-recreate dropping the reloption causes)."""
    tid_a = await _seed_lines(db, [("depleted", None, 100, 0)])
    tid_b = await _seed_lines(db, [("depleted", None, 200, 0)])

    # become app_user, scoped to tenant A (both transaction-local; rolled back by the fixture)
    await db.execute(text("SET LOCAL ROLE app_user"))
    await db.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid_a})
    rows = (await db.execute(text("SELECT tenant_id FROM vw_depletion_coverage"))).mappings().all()

    seen = {str(r["tenant_id"]) for r in rows}
    assert seen == {tid_a}, f"app_user scoped to A must see only A, saw {seen}"
    assert tid_b not in seen


# ═══════════════════════════════════════════════════════════════════════════════
# N3 — failed_reason_breakdown: windowed + NULL→'unknown'
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_failed_reason_breakdown_groups_and_windows(db) -> None:
    """Groups failed lines by reason within window_days; lines outside the window are excluded
    (op-concern 6 is a spike/rate signal, not an all-time count)."""
    tid = await _seed_lines(
        db,
        [
            ("failed", "line_refunded", 0, 0),
            ("failed", "line_refunded", 0, 0),
            ("failed", "sale_ineligible", 0, 0),
            ("failed", "missing_conversion", 0, 40),  # 40 days old → outside a 30-day window
            ("depleted", None, 0, 0),  # not failed → excluded
        ],
    )
    breakdown = await diagnostics.failed_reason_breakdown(
        db, tenant_id=uuid.UUID(tid), window_days=30
    )
    assert breakdown == {"line_refunded": 2, "sale_ineligible": 1}  # old missing_conversion gone


@pytest.mark.integration
async def test_failed_reason_breakdown_null_reason_surfaces_as_unknown(db) -> None:
    """A failed line with a NULL reason is IMPOSSIBLE under the consistency CHECK — so if one
    exists it's a violation, and the diagnostic must surface it loudly as 'unknown', not hide
    it. Drop the CHECK in-txn to inject the anomaly (rolled back by the fixture)."""
    tid = await _seed_lines(db, [("failed", "line_refunded", 0, 0)])
    await db.execute(
        text("ALTER TABLE sale_line_items DROP CONSTRAINT depletion_status_reason_consistency")
    )
    order_id = (
        await db.execute(text("SELECT id FROM orders WHERE tenant_id = :t LIMIT 1"), {"t": tid})
    ).scalar()
    await db.execute(
        text("""
            INSERT INTO sale_line_items
            (id, tenant_id, order_id, clover_line_item_id, name_at_sale, quantity,
             price_cents_at_sale, net_revenue_cents, depletion_status, depletion_reason)
            VALUES (gen_random_uuid(),:t,:oid,:cli,'Item',1,0,0,'failed',NULL)
        """),
        {"t": tid, "oid": order_id, "cli": f"cli_{uuid.uuid4().hex[:8]}"},
    )
    await db.flush()

    breakdown = await diagnostics.failed_reason_breakdown(db, tenant_id=uuid.UUID(tid))
    assert breakdown == {"line_refunded": 1, "unknown": 1}, "the anomaly must surface as 'unknown'"
