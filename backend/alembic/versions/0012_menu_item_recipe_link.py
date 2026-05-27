"""Link menu_items to recipe_versions — completes the POS→inventory pipeline.

The POS worker sets menu_item_id on sale_line_items when a Clover pos_item_id
matches.  Without a link from menu_items to recipe_versions, the worker cannot
snapshot recipe_version_id at sale time, so _emit_inventory_effects() has no
recipe_ingredients to iterate.

This migration adds:
  1. menu_items.recipe_version_id  (nullable FK to recipe_versions)
       Points to the current recipe version for this menu item.
       NULL means the item has no recipe (no inventory depletion on sale).
       Set by operators when they map menu items to recipes.
       The worker reads this at insert time to snapshot recipe_version_id on
       sale_line_items — after that point, changing the FK has no effect on
       historical depletions (snapshot invariant from Section 10 of the ADR).

  2. service_worker SELECT grant on menu_items
       The worker reads menu_items.recipe_version_id inside _insert_line_item().
       Previously service_worker had no grant; menu_item_id was looked up via a
       SELECT that already passed because the query joined via pos_item_id and
       the existing SELECT grant was... actually not granted.  Added here.

Revision ID: 0012_menu_item_recipe_link
Revises: 0011_sw_inventory_grants
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_menu_item_recipe_link"
down_revision: str | None = "0011_sw_inventory_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_T1 = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    # ── 1. Add recipe_version_id FK to menu_items ─────────────────────────────
    op.execute("""
        ALTER TABLE menu_items
            ADD COLUMN recipe_version_id UUID REFERENCES recipe_versions(id)
    """)

    # ── 2. Grant service_worker SELECT on menu_items ──────────────────────────
    #
    # The worker already did SELECT id FROM menu_items WHERE pos_item_id = :pid
    # without an explicit grant.  This worked on some Postgres configs (PUBLIC
    # schema default SELECT) but is not safe to rely on.  Make it explicit.
    op.execute(f"""
        CREATE POLICY menu_items_select_sw ON menu_items
            FOR SELECT TO service_worker
            USING ({_T1})
    """)
    op.execute("GRANT SELECT ON menu_items TO service_worker")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS menu_items_select_sw ON menu_items")
    op.execute("REVOKE SELECT ON menu_items FROM service_worker")
    op.execute("ALTER TABLE menu_items DROP COLUMN IF EXISTS recipe_version_id")
