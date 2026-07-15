"""Cost precision — line totals + exact unit costs (additive).

Revision ID: 0029_cost_precision
Revises: 0028_purchase_unit_conversion
Create Date: 2026-07-15 02:00:00.000000

The Lauzon live smoke exposed two costing corruptions:
  - integer cents per TINY storage unit destroys food cost (oat: true
    0.5803¢/ml snapshotted as 1¢/ml — a 72% error)
  - weight-priced lines (espresso $17.85/kg with SAC purchase units)
    snapshotted from unit_cost x purchase_qty instead of the printed total

Fix at the data layer:
  - receipt_lines.line_total_cents: the invoice's printed extended total —
    the costing ground truth (extraction supplies it; operator lines may not).
  - ingredient_cost_snapshots.unit_cost_cents_exact: NUMERIC cents per
    storage unit, computed at commit as line_total_cents / storage_qty.
    The legacy integer column stays populated (rounded) for existing readers;
    display rounds, storage never destroys precision.

Additive only. Risk: Low.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0029_cost_precision"
down_revision: str | None = "0028_purchase_unit_conversion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE receipt_lines ADD COLUMN line_total_cents integer")
    op.execute("ALTER TABLE ingredient_cost_snapshots ADD COLUMN unit_cost_cents_exact numeric")


def downgrade() -> None:
    op.execute("ALTER TABLE ingredient_cost_snapshots DROP COLUMN IF EXISTS unit_cost_cents_exact")
    op.execute("ALTER TABLE receipt_lines DROP COLUMN IF EXISTS line_total_cents")
