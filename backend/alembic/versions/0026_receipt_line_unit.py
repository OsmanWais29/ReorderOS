"""Sprint 6 S3 — receipt_lines.extracted_unit (additive).

Revision ID: 0026_receipt_line_unit
Revises: 0025_receipt_extraction_grants
Create Date: 2026-06-28 00:00:00.000000

The extraction LLM returns a unit per line (canonical string, e.g. 'kg'), but
receipt_lines stores `purchase_unit_id` (a units_of_measure FK) which is only
resolved at COMMIT (the operator/commit path resolves the string → a uom row via
the shared resolver). The draft line therefore needs somewhere to hold the raw
extracted unit string for the operator to review before that resolution. The spec's
§3.2 column list (extracted_name, confidence, ...) omitted it; this adds it.

Additive, nullable; the existing table-level INSERT grant to service_worker covers
the new column. Risk: Low.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026_receipt_line_unit"
down_revision: str | None = "0025_receipt_extraction_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE receipt_lines ADD COLUMN extracted_unit text")


def downgrade() -> None:
    op.execute("ALTER TABLE receipt_lines DROP COLUMN IF EXISTS extracted_unit")
