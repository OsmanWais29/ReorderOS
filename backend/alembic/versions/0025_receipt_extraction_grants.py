"""Sprint 6 S3 — service_worker grants for the extraction worker.

Revision ID: 0025_receipt_extraction_grants
Revises: 0024_sprint6_receipts_tighten
Create Date: 2026-06-28 00:00:00.000000

The extraction worker runs as `service_worker`. It claims jobs cross-tenant from
receipt_extraction_jobs (USING(true), granted in 0023), then — after SET'ing
app.tenant_id to the job's tenant — reads the receipt and writes the extraction
result back. receipts / receipt_lines are app-path tables (0003): their RLS
policies are PUBLIC and tenant-scoped (`app.tenant_id`), so service_worker passes
them once the GUC is set, but it has NO table GRANTs. This migration adds exactly
the grants the worker needs — nothing more.

  - receipts: SELECT (read photo_object_key / review_started_at / status) and a
    COLUMN-SCOPED UPDATE of only the extraction-owned columns (it must never touch
    commit_state / source / confirmed_at / line-match fields — those are operator/
    commit territory). Column-scoping mirrors 0017's menu_items grant.
  - receipt_lines: SELECT + INSERT (the worker writes extracted lines). NO UPDATE/
    DELETE: re-extraction line-superseding is done at enqueue on the request path,
    not by the worker — so the worker needs no update/delete policy on receipt_lines.

receipt_extraction_jobs and tenant_extraction_rate_limits already grant
service_worker SELECT/INSERT/UPDATE with USING(true) (0023), so claim + quota charge
need nothing here.

Risk: Low — additive grants only; no data, no schema change. Reversible via REVOKE.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0025_receipt_extraction_grants"
down_revision: str | None = "0024_sprint6_receipts_tighten"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Only the extraction-owned receipts columns the worker may write.
_RECEIPTS_UPDATE_COLS = (
    "extraction_status",
    "extraction_confidence",
    "review_visibility_status",
    "suppression_reason",
    "quota_blocked",
    "quota_blocked_until",
    "manual_entry_required",
    "supplier_name",
    "invoice_number",
    "invoice_date",
    "subtotal_cents",
    "tax_cents",
    "total_cents",
    "updated_at",  # the worker stamps updated_at on every receipts write
)


def upgrade() -> None:
    cols = ", ".join(_RECEIPTS_UPDATE_COLS)
    op.execute("GRANT SELECT ON receipts TO service_worker")
    op.execute(f"GRANT UPDATE ({cols}) ON receipts TO service_worker")
    op.execute("GRANT SELECT, INSERT ON receipt_lines TO service_worker")


def downgrade() -> None:
    cols = ", ".join(_RECEIPTS_UPDATE_COLS)
    op.execute("REVOKE SELECT, INSERT ON receipt_lines FROM service_worker")
    op.execute(f"REVOKE UPDATE ({cols}) ON receipts FROM service_worker")
    op.execute("REVOKE SELECT ON receipts FROM service_worker")
