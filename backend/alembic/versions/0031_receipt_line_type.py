"""Sprint 6 follow-up — materialized non-stock rows: receipt_lines.line_type.

The extractor classifies every invoice row (item/discount/credit/backorder/
fee_or_deposit) but v1 dropped non-item rows at apply time — they survived only
in the retention-bounded raw_extraction checkpoint, so the operator could never
see WHY the invoice total didn't match the item lines. This column lets the
worker materialize them as match_status='skipped' rows (no linkage, no
quantities, signed line_total_cents as printed) that every commit gate already
ignores by the existing skipped-line exemption.

Risk classification (Migration Risk Standard §1.2):
  - Data validity:             Low — additive column, DEFAULT 'item' is true for
                                     every existing row (only items were written).
  - Availability impact:       Low — ADD COLUMN with DEFAULT (PG ≥11 fast-path).
  - Application compatibility: Low — additive; readers ignore unknown columns.
  - Data propagation risk:     Low.
  - Reversibility:             Full — DROP COLUMN in downgrade().

Revision ID: 0031_receipt_line_type
Revises: 0030_inbound_email_worker_grants
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0031_receipt_line_type"
down_revision: str | None = "0030_inbound_email_worker_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE receipt_lines
            ADD COLUMN line_type text NOT NULL DEFAULT 'item'
                CHECK (line_type IN
                    ('item', 'discount', 'credit', 'backorder', 'fee_or_deposit'))
    """)
    # Non-stock rows carry no meaningful quantity. The old blanket NOT NULL
    # becomes CONDITIONAL: item lines must still have a quantity (same guarantee
    # as before — every existing row is an item with a quantity, so the CHECK
    # validates instantly); non-item rows may be NULL.
    op.execute("ALTER TABLE receipt_lines ALTER COLUMN received_quantity DROP NOT NULL")
    op.execute("""
        ALTER TABLE receipt_lines
            ADD CONSTRAINT receipt_lines_item_qty_required
                CHECK (line_type <> 'item' OR received_quantity IS NOT NULL)
    """)


def downgrade() -> None:
    # Machine-created non-stock rows are the only NULL-quantity rows; they are
    # reproducible from re-extraction, so a clean downgrade removes them before
    # restoring the blanket NOT NULL.
    op.execute("DELETE FROM receipt_lines WHERE line_type <> 'item'")
    op.execute(
        "ALTER TABLE receipt_lines DROP CONSTRAINT IF EXISTS receipt_lines_item_qty_required"
    )
    op.execute("ALTER TABLE receipt_lines ALTER COLUMN received_quantity SET NOT NULL")
    op.execute("ALTER TABLE receipt_lines DROP COLUMN IF EXISTS line_type")
