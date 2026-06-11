"""Depletion monitoring diagnostics (Sprint 5 Phase 12, v5 §12 + operational-concern 5).

Read-only queries that surface the health of the depletion pipeline. They write nothing and
are import-isolated from the LLM layer like the rest of `depletion/` (the Phase 14 guard).

  stuck_pending_lines — sale lines stuck UNPROCESSED beyond a threshold (exit-gate 29). See
  phase-12 notes N4: "unprocessed" = NULL-or-'pending' (the same definition the handler's
  process-once gate uses), and the anchor is `created_at` (ingested-but-unprocessed, a
  worker-health signal) — NOT `recorded_at`, which would conflate worker lag with webhook
  delivery lag (that gap is the late-signal monitor's job).

  failed_reason_breakdown — failed lines grouped by reason within a time window (exit-gate 30
  support / operational-concern 6, the refund-pattern monitor). See phase-13 notes: WINDOWED
  on created_at (op-concern 6 is about SPIKES — a rate — and an all-time count swamps any
  recent spike under months of accumulated base), and a NULL reason surfaces as 'unknown'
  rather than being filtered out (the consistency CHECK makes failed-with-NULL-reason
  impossible, so if one exists it's a violation the diagnostic must show LOUDLY).

Accepts an explicit tenant_id (the caller scopes per tenant); does NOT rely on RLS, so it
works from any role with SELECT on sale_line_items (service_worker has it since 0006).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class StuckPendingLine:
    sale_line_item_id: UUID
    order_id: UUID
    age_seconds: float


async def stuck_pending_lines(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    older_than_minutes: int = 5,
) -> list[StuckPendingLine]:
    """Return sale lines still unprocessed (NULL or 'pending') older than the threshold,
    by ingestion time (created_at). v5 exit-gate 29 / operational-concern 5: pending rows
    >5 min old must be queryable so ops can alert on a wedged worker. Ordered oldest-first."""
    rows = (
        await session.execute(
            text("""
                SELECT id, order_id,
                       EXTRACT(EPOCH FROM (now() - created_at)) AS age_seconds
                FROM sale_line_items
                WHERE tenant_id = :tid
                  AND (depletion_status IS NULL OR depletion_status = 'pending')
                  AND created_at < now() - make_interval(mins => :mins)
                ORDER BY created_at ASC
            """),
            {"tid": tenant_id, "mins": older_than_minutes},
        )
    ).mappings().all()
    return [
        StuckPendingLine(
            sale_line_item_id=r["id"],
            order_id=r["order_id"],
            age_seconds=float(r["age_seconds"]),
        )
        for r in rows
    ]


async def failed_reason_breakdown(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    window_days: int = 30,
) -> dict[str, int]:
    """Return {depletion_reason: count} over failed lines in the last ``window_days``
    (operational-concern 6, refund-pattern monitoring). 'line_refunded' spikes are expected;
    'sale_ineligible' / 'missing_conversion' spikes indicate a Clover-state or config problem.

    Windowed on created_at because the signal is a SPIKE (a rate): an all-time count would
    swamp a recent spike under months of accumulated base. A NULL reason is surfaced as
    'unknown' (not excluded): the depletion_status_reason_consistency CHECK makes a failed
    line with a NULL reason impossible, so an 'unknown' with a nonzero count is a loud signal
    that the invariant was violated/bypassed — exactly what a diagnostic should show."""
    rows = (
        await session.execute(
            text("""
                SELECT COALESCE(depletion_reason, 'unknown') AS reason, COUNT(*) AS n
                FROM sale_line_items
                WHERE tenant_id = :tid
                  AND depletion_status = 'failed'
                  AND created_at > now() - make_interval(days => :days)
                GROUP BY COALESCE(depletion_reason, 'unknown')
            """),
            {"tid": tenant_id, "days": window_days},
        )
    ).mappings().all()
    return {r["reason"]: int(r["n"]) for r in rows}
