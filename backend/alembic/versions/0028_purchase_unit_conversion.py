"""Sprint 6 — purchase-unit conversion workflow (additive).

Revision ID: 0028_purchase_unit_conversion
Revises: 0027_receipt_commit_trigger
Create Date: 2026-07-15 00:00:00.000000

The live Lauzon smoke proved invoices arrive in purchase units (CS, SAC, EA)
while inventory/depletion runs in canonical storage units (L, kg, ea). The
operator must confirm a pack conversion (1 CS = 16 L) before commit — never a
silent guess. This adds:

receipt_lines (all nullable/additive):
  - purchase_quantity / purchase_unit: the invoice's ORIGINAL qty + U/M, stashed
    when the operator confirms a conversion (received_quantity/extracted_unit
    hold the invoice values until then — existing rows unaffected).
  - received_unit: canonical unit received_quantity is denominated in AFTER an
    operator confirms (NULL = not yet confirmed; commit falls back to legacy
    extracted_unit handling).
  - conversion_factor: storage units per 1 purchase unit (16 for 1 CS = 16 L).
  - conversion_source: extracted_suggestion | operator_confirmed | remembered |
    identity — how the numbers were arrived at.
  - conversion_confirmed_at / conversion_confirmed_by: the operator gate.
  - pack_count / pack_size_qty / pack_size_unit / actual_weight_qty /
    actual_weight_unit: extraction packaging hints ("4x4L", "ACTUAL WT 10.18 KG")
    used to PREFILL the suggestion — data, never authority.

tenant_item_purchase_conversions: remembered per-item pack conversions so the
next receipt from the same supplier prefills 1 CS = 16 L (still shown for
confirmation, never silently applied). ENABLE+FORCE RLS, app_user tenant policy.

Table-level grants already cover the new receipt_lines columns (0025 gave
service_worker INSERT on the whole table; app_user path predates column lists).
Risk: Low (additive only).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0028_purchase_unit_conversion"
down_revision: str | None = "0027_receipt_commit_trigger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_T1 = "tenant_id = current_setting('app.tenant_id', true)::uuid"


def upgrade() -> None:
    op.execute("ALTER TABLE receipt_lines ADD COLUMN purchase_quantity numeric")
    op.execute("ALTER TABLE receipt_lines ADD COLUMN purchase_unit text")
    op.execute("ALTER TABLE receipt_lines ADD COLUMN received_unit text")
    op.execute("ALTER TABLE receipt_lines ADD COLUMN conversion_factor numeric")
    op.execute(
        "ALTER TABLE receipt_lines ADD COLUMN conversion_source text "
        "CONSTRAINT receipt_lines_conversion_source_valid CHECK ("
        "conversion_source IS NULL OR conversion_source IN "
        "('extracted_suggestion','operator_confirmed','remembered','identity'))"
    )
    op.execute("ALTER TABLE receipt_lines ADD COLUMN conversion_confirmed_at timestamptz")
    op.execute("ALTER TABLE receipt_lines ADD COLUMN conversion_confirmed_by uuid")
    op.execute("ALTER TABLE receipt_lines ADD COLUMN pack_count numeric")
    op.execute("ALTER TABLE receipt_lines ADD COLUMN pack_size_qty numeric")
    op.execute("ALTER TABLE receipt_lines ADD COLUMN pack_size_unit text")
    op.execute("ALTER TABLE receipt_lines ADD COLUMN actual_weight_qty numeric")
    op.execute("ALTER TABLE receipt_lines ADD COLUMN actual_weight_unit text")

    op.execute(
        """
        CREATE TABLE tenant_item_purchase_conversions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            inventory_item_id uuid NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE,
            purchase_unit text NOT NULL,
            storage_unit text NOT NULL,
            factor numeric NOT NULL CHECK (factor > 0),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT tenant_item_purchase_conversions_unique
                UNIQUE (tenant_id, inventory_item_id, purchase_unit)
        )
        """
    )
    op.execute("ALTER TABLE tenant_item_purchase_conversions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_item_purchase_conversions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tipc_tenant ON tenant_item_purchase_conversions FOR ALL TO app_user "
        f"USING ({_T1}) WITH CHECK ({_T1})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON tenant_item_purchase_conversions TO app_user")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenant_item_purchase_conversions")
    for col in (
        "purchase_quantity",
        "purchase_unit",
        "received_unit",
        "conversion_factor",
        "conversion_source",
        "conversion_confirmed_at",
        "conversion_confirmed_by",
        "pack_count",
        "pack_size_qty",
        "pack_size_unit",
        "actual_weight_qty",
        "actual_weight_unit",
    ):
        op.execute(f"ALTER TABLE receipt_lines DROP COLUMN IF EXISTS {col}")
