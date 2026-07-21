"""Adjustment disposition — a product discount/credit can never be silently ignored.

Gate-1 live failure: an extracted product discount was never linked, nothing
forced a decision, and commit proceeded at gross cost. Every linkable
adjustment row (line_type discount/credit) now carries an explicit persisted
disposition:

  pending   — operator has not decided (COMMIT BLOCKER)
  linked    — adjusts_line_id points at the chosen item line
  excluded  — operator explicitly kept it out of inventory cost (reason code)

Fees, deposits, backorders and taxes stay non-linkable and carry NULL (no
decision required). Consistency is a DB fact, not a convention:
linked ⇔ adjusts_line_id set.

NOTE: this claims revision 0033 — the generic-webhook draft that provisionally
used the number is unimplemented and renumbers to 0034+.

Risk classification (Migration Risk Standard §1.2):
  - Data validity:  Low — additive columns; backfill derives from existing
                    adjusts_line_id state (linked where set, else pending).
  - Availability:   Low — ADD COLUMN + UPDATE over receipt_lines (small at
                    pilot scale); CHECKs validate in one pass.
  - Compatibility:  Low — additive; commit gate ships in the same deploy.
  - Reversibility:  Full — DROP COLUMNs.

Revision ID: 0033_adjustment_disposition
Revises: 0032_receipt_line_adjustments
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0033_adjustment_disposition"
down_revision: str | None = "0032_receipt_line_adjustments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE receipt_lines
            ADD COLUMN adjustment_disposition text
                CHECK (adjustment_disposition IN ('pending', 'linked', 'excluded')),
            ADD COLUMN disposition_reason text
                CHECK (char_length(disposition_reason) <= 100),
            ADD COLUMN disposition_reviewed_at timestamptz,
            ADD COLUMN disposition_reviewed_by uuid REFERENCES users(id)
    """)
    # Backfill BEFORE the consistency checks: existing links are 'linked',
    # every other discount/credit is 'pending' (incl. rows on committed
    # receipts — harmless: commit short-circuits already_committed before any
    # gate, and history is never re-gated).
    op.execute("""
        UPDATE receipt_lines
           SET adjustment_disposition = CASE
                   WHEN adjusts_line_id IS NOT NULL THEN 'linked'
                   ELSE 'pending'
               END
         WHERE line_type IN ('discount', 'credit')
    """)
    # Only linkable rows carry a disposition…
    op.execute("""
        ALTER TABLE receipt_lines
            ADD CONSTRAINT ck_disposition_linkable_only CHECK (
                adjustment_disposition IS NULL OR line_type IN ('discount', 'credit')
            )
    """)
    # …and 'linked' is exactly synonymous with a set adjusts_line_id.
    op.execute("""
        ALTER TABLE receipt_lines
            ADD CONSTRAINT ck_disposition_link_consistent CHECK (
                adjustment_disposition IS NULL
                OR ((adjustment_disposition = 'linked') = (adjusts_line_id IS NOT NULL))
            )
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE receipt_lines DROP CONSTRAINT IF EXISTS ck_disposition_link_consistent")
    op.execute("ALTER TABLE receipt_lines DROP CONSTRAINT IF EXISTS ck_disposition_linkable_only")
    op.execute("""
        ALTER TABLE receipt_lines
            DROP COLUMN IF EXISTS disposition_reviewed_by,
            DROP COLUMN IF EXISTS disposition_reviewed_at,
            DROP COLUMN IF EXISTS disposition_reason,
            DROP COLUMN IF EXISTS adjustment_disposition
    """)
