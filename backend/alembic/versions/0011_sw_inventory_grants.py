"""Grant service_worker access to inventory tables.

The POS worker calls record_sale_inventory_effect() inside the same
session/transaction as order + sale_line_item writes (which already run as
service_worker with T1 RLS).  The inventory tables were created in 0003
with app_user-only policies.  This migration adds the missing grants and RLS
policies so service_worker can:

  - SELECT inventory_items        (on_hand computation, item mode lookup)
  - SELECT/INSERT inventory_movements   (idempotency check + depletion/signal writes)
  - SELECT inventory_count_events       (late-signal boundary check)
  - SELECT inventory_yield_factors      (yield factor snapshot at write time)
  - SELECT recipe_ingredients           (_emit_inventory_effects ingredient lookup)

All policies use T1 (tenant-scoped) USING clause — the worker always calls
  SELECT set_config('app.tenant_id', :tid, true)
before any inventory write, matching the pattern already used for orders and
sale_line_items.

Revision ID: 0011_service_worker_inventory_grants
Revises: 0010_accounting_hardening
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_sw_inventory_grants"
down_revision: str | None = "0010_accounting_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_T1 = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    # ── inventory_items ───────────────────────────────────────────────────────
    op.execute(f"""
        CREATE POLICY inventory_items_select_sw ON inventory_items
            FOR SELECT TO service_worker
            USING ({_T1})
    """)
    op.execute("GRANT SELECT ON inventory_items TO service_worker")

    # ── inventory_movements ───────────────────────────────────────────────────
    op.execute(f"""
        CREATE POLICY inventory_movements_select_sw ON inventory_movements
            FOR SELECT TO service_worker
            USING ({_T1})
    """)
    op.execute(f"""
        CREATE POLICY inventory_movements_insert_sw ON inventory_movements
            FOR INSERT TO service_worker
            WITH CHECK ({_T1})
    """)
    op.execute("GRANT SELECT, INSERT ON inventory_movements TO service_worker")

    # ── inventory_count_events ────────────────────────────────────────────────
    op.execute(f"""
        CREATE POLICY inventory_count_events_select_sw ON inventory_count_events
            FOR SELECT TO service_worker
            USING ({_T1})
    """)
    op.execute("GRANT SELECT ON inventory_count_events TO service_worker")

    # ── inventory_yield_factors ───────────────────────────────────────────────
    op.execute(f"""
        CREATE POLICY inventory_yield_factors_select_sw ON inventory_yield_factors
            FOR SELECT TO service_worker
            USING ({_T1})
    """)
    op.execute("GRANT SELECT ON inventory_yield_factors TO service_worker")

    # ── recipe_ingredients ────────────────────────────────────────────────────
    op.execute(f"""
        CREATE POLICY recipe_ingredients_select_sw ON recipe_ingredients
            FOR SELECT TO service_worker
            USING ({_T1})
    """)
    op.execute("GRANT SELECT ON recipe_ingredients TO service_worker")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS recipe_ingredients_select_sw ON recipe_ingredients")
    op.execute("REVOKE SELECT ON recipe_ingredients FROM service_worker")

    op.execute("DROP POLICY IF EXISTS inventory_yield_factors_select_sw ON inventory_yield_factors")
    op.execute("REVOKE SELECT ON inventory_yield_factors FROM service_worker")

    op.execute("DROP POLICY IF EXISTS inventory_count_events_select_sw ON inventory_count_events")
    op.execute("REVOKE SELECT ON inventory_count_events FROM service_worker")

    op.execute("DROP POLICY IF EXISTS inventory_movements_insert_sw ON inventory_movements")
    op.execute("DROP POLICY IF EXISTS inventory_movements_select_sw ON inventory_movements")
    op.execute("REVOKE SELECT, INSERT ON inventory_movements FROM service_worker")

    op.execute("DROP POLICY IF EXISTS inventory_items_select_sw ON inventory_items")
    op.execute("REVOKE SELECT ON inventory_items FROM service_worker")
