"""Sprint 5 Phase 2 follow-up — menu_items uniqueness + modifiers grant tightening.

Revision ID: 0018_catalog_hardening
Revises: 0017_sprint5_view_and_grants

(Revision id kept <=32 chars — alembic_version.version_num is varchar(32).)
Create Date: 2026-06-04 00:00:00.000000

Two hardening changes surfaced while building catalog sync (Phase 2):

1. UNIQUE(tenant_id, pos_item_id) WHERE pos_item_id IS NOT NULL on menu_items.
   Catalog sync upserts menu_items by (tenant_id, pos_item_id) and runs from two
   triggers that can fire concurrently during onboarding (the OAuth background
   task and the /sync-menu retry). With no unique, a race lets two syncs both
   "see" an item as absent and both INSERT it — producing DUPLICATE menu_items
   for one Clover item. That splits recipe association from sale mapping: the
   operator's recipe attaches to one duplicate, sales may resolve to the other
   and come back 'unmapped' despite being configured — the silent
   "configured-but-never-depletes" failure this sprint exists to prevent. The
   partial predicate lets manually-added items (no pos_item_id) coexist, matching
   the modifiers partial-unique pattern from 0014. It also makes the sync upsert
   ON CONFLICT-able.

2. REVOKE UPDATE ON modifiers FROM service_worker (least privilege).
   Nothing in Sprint 5 updates modifiers as service_worker: catalog sync is
   INSERT ... ON CONFLICT DO NOTHING; operator confirmation (which sets
   status/current_version_id) runs as app_user; the depletion walker only reads
   modifiers. The table-wide UPDATE grant from 0014 was therefore unused AND
   inconsistent with the column-scoped discipline on menu_items/sale_line_items
   (0017). Dropping it makes clobbering operator-owned modifier state impossible
   for service_worker, not merely unexercised. app_user keeps its UPDATE.

Risk classification (Migration Risk Standard §1.2):
  - Data validity:            Medium — the unique index validates existing data;
                                       the §3 preflight counts duplicates and
                                       aborts. (None expected: prod menu_items
                                       empty; preflight runs anyway per standard.)
  - Availability impact:      Low — CREATE UNIQUE INDEX on a small table; a REVOKE.
  - Application compatibility: Low — additive index; the revoked grant is exercised
                                       by no Sprint-5 code path (verified §4.5).
  - Data propagation risk:    Low.
  - Reversibility:            Low — drop the index; re-grant UPDATE on downgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018_catalog_hardening"
down_revision: str | None = "0017_sprint5_view_and_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── §3 preflight: abort if any duplicate (tenant_id, pos_item_id) exists ───
    op.execute("""
        DO $$
        DECLARE
            n bigint;
        BEGIN
            SELECT count(*) INTO n FROM (
                SELECT tenant_id, pos_item_id
                FROM menu_items
                WHERE pos_item_id IS NOT NULL
                GROUP BY tenant_id, pos_item_id
                HAVING count(*) > 1
            ) d;
            IF n > 0 THEN
                RAISE EXCEPTION
                    'Migration 0018 preflight FAILED: % duplicate (tenant_id, pos_item_id) '
                    'menu_items rows. Deduplicate before adding the unique index.', n;
            END IF;
        END $$;
    """)

    # 1. Race-safe uniqueness for Clover-sourced items (manual items, NULL
    #    pos_item_id, are unaffected by the partial predicate).
    op.execute("""
        CREATE UNIQUE INDEX menu_items_tenant_pos_item_uniq
            ON menu_items (tenant_id, pos_item_id)
            WHERE pos_item_id IS NOT NULL
    """)

    # 2. Least privilege: service_worker never UPDATEs modifiers in Sprint 5.
    op.execute("REVOKE UPDATE ON modifiers FROM service_worker")


def downgrade() -> None:
    op.execute("GRANT UPDATE ON modifiers TO service_worker")
    op.execute("DROP INDEX IF EXISTS menu_items_tenant_pos_item_uniq")
