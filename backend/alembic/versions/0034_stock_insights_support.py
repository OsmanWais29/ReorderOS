"""Stock Item Insights (PR-A1) support columns.

Two additive, nullable columns:

  tenants.timezone (text, null)
    IANA zone for daily-consumption bucketing. NOT backfilled — unset tenants
    fall back to UTC and the insights response reports timezone_source='fallback'.
    Validated at the API layer against zoneinfo.available_timezones() (the IANA
    set is not enumerable in a CHECK constraint), never in the database.

  tenant_pos_connections.orders_complete_through (timestamptz, null)
    The ingestion completeness watermark. PR-A1 NEVER writes it: the Clover
    client cannot capture a provider server-time cutoff (it reads no Date
    header / server-time endpoint — clover_client.py) and its offset pagination
    filtered only by modifiedTime>= has no upper bound, total count, or stable
    cursor, so bounded completeness is not provable. Per the PR-A1 gate the
    column exists as the contract slot but stays NULL; forecast/trend stay
    NOT_YET_CERTIFIED until a provider-cutoff + bounded-fetch mechanism is built
    and validated against real Clover (a later PR).

Risk classification (Migration Risk Standard §1.2):
  - Data validity: Low — additive nullable columns, no backfill.
  - Availability:  Low — two ADD COLUMN, no rewrite, no lock of hot paths.
  - Compatibility: Low — additive; readers tolerate NULL by design.
  - Reversibility: Full — DROP COLUMN.

Revision ID: 0034_stock_insights_support
Revises: 0033_adjustment_disposition
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0034_stock_insights_support"
down_revision: str | None = "0033_adjustment_disposition"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenants ADD COLUMN timezone text")
    op.execute(
        "ALTER TABLE tenant_pos_connections ADD COLUMN orders_complete_through timestamptz"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE tenant_pos_connections DROP COLUMN IF EXISTS orders_complete_through"
    )
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS timezone")
