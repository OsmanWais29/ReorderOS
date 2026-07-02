"""Sprint 5 Phase 11 — service_worker UPDATE grant on sale_line_items.is_refunded.

Revision ID: 0021_refund_reversal_grant
Revises: 0020_depletion_worker_grants
Create Date: 2026-06-10 00:00:00.000000

Per ADR 0001 (decisions/adr/0001-sprint5-phase11-refund-reversal.md), decision D2 + the
grant section. Metadata-only (Migration Risk Standard §2.1: a single column-level GRANT;
no existing-data validation, no lock of consequence).

Why this migration exists
-------------------------
Phase 11 wires Clover line refunds to inventory reversal. When a refund arrives the worker's
refund-reconcile step marks `sale_line_items.is_refunded` false → true (ADR D2: the worker
owns refund detection + the is_refunded write; reverse_line owns only the ledger negation).
The worker runs as `service_worker`, whose UPDATE on sale_line_items is COLUMN-SCOPED to
exactly (depletion_status, depletion_reason) by 0017 — which DELIBERATELY excluded is_refunded
("Table-wide UPDATE would let the worker mutate ... is_refunded ..."). That exclusion was
correct while is_refunded was set only at INSERT; Phase 11 is the first code that legitimately
mutates it post-insert, so the grant must widen by exactly one column.

We add ONLY is_refunded — not net_revenue_cents, not recipe_version_id (the frozen snapshot),
not name_at_sale/quantity/price. The least-privilege posture of 0017 is preserved.

§4.5 application-impact verification (Migration Risk Standard)
-------------------------------------------------------------
Call sites that UPDATE sale_line_items.is_refunded as service_worker after this grant:
  - app/modules/pos/worker.py — the refund-reconcile step (the ONLY new writer; introduced in
    the same Phase 11 change). RLS (sli_tenant_isolation) still scopes WHICH rows to the
    worker's tenant. No other service_worker path writes is_refunded.
Proven under the real service_worker role by the Phase 11 e2e (a missing grant fails there
with "permission denied for ... sale_line_items" while superuser unit tests stay green).

Risk classification (Migration Risk Standard §1.2):
  - Data validity:            Low — GRANT only; validates no existing data.
  - Availability impact:      Low — a single catalog GRANT; no table lock.
  - Application compatibility: Low — purely additive privilege; nothing removed.
  - Data propagation risk:    Low — no replication-affecting change.
  - Reversibility:            Low — downgrade REVOKEs exactly the one column, restoring the
                                    0017 (depletion_status, depletion_reason)-only grant.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021_refund_reversal_grant"
down_revision: str | None = "0020_depletion_worker_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Widen the worker's column-scoped UPDATE by exactly is_refunded. Postgres column GRANTs
    # accumulate, so the worker now holds UPDATE on (depletion_status, depletion_reason,
    # is_refunded) and no other columns.
    op.execute("GRANT UPDATE (is_refunded) ON sale_line_items TO service_worker")


def downgrade() -> None:
    # Remove only the is_refunded column privilege; the 0017 depletion-lifecycle grant is
    # untouched (column REVOKEs are independent per column).
    op.execute("REVOKE UPDATE (is_refunded) ON sale_line_items FROM service_worker")
