"""Sprint 6 Phase 3b — inbound email worker grants.

The inbound email fan-out worker (service_worker) creates one receipt DRAFT per
qualified attachment (D-606-01), which needs INSERT on receipts — 0025 granted
only SELECT + column-scoped UPDATE (the extraction worker never creates rows).
The receipts RLS policies are role-agnostic (_T1 predicate), so the worker
operates tenant-scoped after SET'ing app.tenant_id exactly like extraction does;
this grant is the only missing piece.

Risk classification (Migration Risk Standard §1.2):
  - Data validity:             None — GRANT only, no data touched.
  - Availability impact:       None — catalog change, no locks on user tables.
  - Application compatibility: None — additive privilege.
  - Data propagation risk:     None.
  - Reversibility:             Full — REVOKE in downgrade().

Revision ID: 0030_inbound_email_worker_grants
Revises: 0029_cost_precision
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0030_inbound_email_worker_grants"
down_revision: str | None = "0029_cost_precision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT INSERT ON receipts TO service_worker")


def downgrade() -> None:
    op.execute("REVOKE INSERT ON receipts FROM service_worker")
