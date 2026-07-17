"""Part C — operator-linked cost adjustments: receipt_lines.adjusts_line_id.

A materialized non-stock DISCOUNT/CREDIT row can be explicitly linked (never
silently) to one receivable item line on the same receipt; commit then computes
that line's cost basis from the NET amount (gross line total + signed linked
adjustments). Deposits, fuel/delivery surcharges, and taxes are deliberately
NOT linkable — they stay out of food inventory cost (no landed-cost allocation
policy exists yet).

Risk classification (Migration Risk Standard §1.2):
  - Data validity:             Low — additive nullable self-FK; no rows validated.
  - Availability impact:       Low — ADD COLUMN nullable, metadata-only lock.
  - Application compatibility: Low — additive.
  - Data propagation risk:     Low.
  - Reversibility:             Full — DROP COLUMN.

Revision ID: 0032_receipt_line_adjustments
Revises: 0031_receipt_line_type
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0032_receipt_line_adjustments"
down_revision: str | None = "0031_receipt_line_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE receipt_lines
            ADD COLUMN adjusts_line_id uuid
                REFERENCES receipt_lines(id) ON DELETE SET NULL
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE receipt_lines DROP COLUMN IF EXISTS adjusts_line_id")
