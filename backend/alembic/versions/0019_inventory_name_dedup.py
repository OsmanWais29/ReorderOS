"""Sprint 5 Phase 4 prerequisite — race-safe case/space-insensitive inventory dedup.

Revision ID: 0019_inventory_name_dedup
Revises: 0018_catalog_hardening
Create Date: 2026-06-04 00:00:00.000000

(Revision id <=32 chars — alembic_version.version_num is varchar(32).)

Recipe confirm (Phase 4, v5 §8) dedups each ingredient to an inventory_items row
by NORMALIZED name. Without a unique constraint, two concurrent confirms that both
reference a not-yet-existing ingredient each create it → duplicate-by-case items,
defeating dedup (the same race class as the Phase 2 menu_items duplicate). This
adds UNIQUE(tenant_id, lower(btrim(name))) so the second insert conflicts and the
confirm falls back to linking the first row via ON CONFLICT.

NORMALIZATION = lower(btrim(name)). It MUST be identical across all four sites:
this index, the §3 preflight below, the service-layer dedup (INSERT ON CONFLICT +
SELECT), and the confirm-time duplicate-ingredient rejection. Any divergence lets
a value pass one check and collide at another.

Risk classification (Migration Risk Standard §1.2):
  - Data validity:            Medium — validates existing rows; §3 preflight counts
                                       normalized-name dupes and aborts. (Prod
                                       inventory_items empty; preflight runs anyway.)
  - Availability impact:      Low — CREATE UNIQUE INDEX on a small table.
  - Application compatibility: Low — additive; first writer that dedups is Phase 4.
  - Data propagation risk:    Low.
  - Reversibility:            Low — drop the index.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019_inventory_name_dedup"
down_revision: str | None = "0018_catalog_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── §3 preflight: abort if any (tenant_id, normalized name) duplicates exist ──
    op.execute("""
        DO $$
        DECLARE
            n bigint;
        BEGIN
            SELECT count(*) INTO n FROM (
                SELECT tenant_id, lower(btrim(name))
                FROM inventory_items
                GROUP BY tenant_id, lower(btrim(name))
                HAVING count(*) > 1
            ) d;
            IF n > 0 THEN
                RAISE EXCEPTION
                    'Migration 0019 preflight FAILED: % duplicate (tenant_id, '
                    'normalized name) inventory_items rows. Deduplicate first.', n;
            END IF;
        END $$;
    """)

    op.execute("""
        CREATE UNIQUE INDEX inventory_items_tenant_norm_name_uniq
            ON inventory_items (tenant_id, lower(btrim(name)))
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS inventory_items_tenant_norm_name_uniq")
