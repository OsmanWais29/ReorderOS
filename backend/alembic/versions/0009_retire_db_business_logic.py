"""Retire on_hand() SQL function and count-event trigger.

Business logic — Mode A/B branching, drift calculation, anchor update —
belongs in the Python service layer where it can be read, tested, and changed
without a migration.  Both objects are now owned by
app/modules/inventory/services.py.

fn_opening_balance_must_be_first() is a data-integrity guard, not business
logic, and is deliberately kept in the DB.

Revision ID: 0009_retire_db_business_logic
Revises: 0008_pos_waitlist
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_retire_db_business_logic"
down_revision: str | None = "0008_pos_waitlist"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_count_event_emits_adjust ON inventory_count_events")
    op.execute("DROP FUNCTION IF EXISTS fn_count_event_emits_adjust()")
    op.execute("DROP FUNCTION IF EXISTS on_hand(UUID, UUID)")


def downgrade() -> None:
    # Recreate on_hand() SQL function (mirrors 0003_inventory_ledger Phase 2).
    op.execute("""
        CREATE OR REPLACE FUNCTION on_hand(
            p_tenant_id          UUID,
            p_inventory_item_id  UUID
        ) RETURNS NUMERIC LANGUAGE SQL STABLE AS $$
          WITH item AS (
              SELECT inventory_mode, last_count_at, last_count_quantity
                FROM inventory_items
               WHERE tenant_id = p_tenant_id AND id = p_inventory_item_id
          ),
          ledger_sum AS (
              SELECT COALESCE(SUM(delta), 0) AS qty
                FROM inventory_movements
               WHERE tenant_id         = p_tenant_id
                 AND inventory_item_id = p_inventory_item_id
                 AND movement_type NOT IN ('sale_signal','sale_signal_reversal')
          ),
          receipts_since AS (
              SELECT COALESCE(SUM(m.delta), 0) AS qty
                FROM inventory_movements m, item
               WHERE m.tenant_id         = p_tenant_id
                 AND m.inventory_item_id = p_inventory_item_id
                 AND m.recorded_at       > item.last_count_at
                 AND m.movement_type IN ('receive','transfer_in','count_adjust','opening_balance')
          ),
          signals_since AS (
              SELECT COALESCE(SUM(ABS(m.delta)), 0) AS qty
                FROM inventory_movements m, item
               WHERE m.tenant_id         = p_tenant_id
                 AND m.inventory_item_id = p_inventory_item_id
                 AND m.recorded_at       > item.last_count_at
                 AND m.movement_type     = 'sale_signal'
          )
          SELECT CASE
              WHEN item.inventory_mode = 'recipe_deducted'
                  THEN ledger_sum.qty
              WHEN item.inventory_mode = 'count_anchored'
                   AND item.last_count_quantity IS NOT NULL
                  THEN item.last_count_quantity
                       + receipts_since.qty
                       - (signals_since.qty
                          * COALESCE(
                              (SELECT yield_factor FROM inventory_yield_factors
                                WHERE tenant_id         = p_tenant_id
                                  AND inventory_item_id = p_inventory_item_id),
                              1.0))
              ELSE NULL
          END
          FROM item, ledger_sum, receipts_since, signals_since
        $$
    """)

    # Recreate trigger function (mirrors 0003_inventory_ledger Phase 3).
    op.execute("""
        CREATE OR REPLACE FUNCTION fn_count_event_emits_adjust()
        RETURNS TRIGGER LANGUAGE plpgsql
        SECURITY DEFINER SET search_path = public AS $$
        DECLARE
            v_mode      text;
            v_predicted numeric;
            v_drift     numeric;
        BEGIN
            SELECT inventory_mode INTO v_mode
              FROM inventory_items
             WHERE id = NEW.inventory_item_id AND tenant_id = NEW.tenant_id;

            IF v_mode = 'count_anchored' THEN
                UPDATE inventory_items
                   SET last_count_at       = NEW.counted_at,
                       last_count_quantity = NEW.counted_quantity
                 WHERE id = NEW.inventory_item_id AND tenant_id = NEW.tenant_id;
                RETURN NEW;
            END IF;

            v_predicted := NEW.predicted_on_hand_at_count;
            v_drift     := NEW.counted_quantity
                           - COALESCE(v_predicted, NEW.counted_quantity);

            IF ABS(v_drift) < 0.0001 THEN
                RETURN NEW;
            END IF;

            INSERT INTO inventory_movements (
                tenant_id, inventory_item_id, movement_type, delta,
                source_type, source_id, idempotency_key, recorded_at, notes
            ) VALUES (
                NEW.tenant_id, NEW.inventory_item_id, 'count_adjust', v_drift,
                'count_event', NEW.id,
                'count_adjust:' || NEW.id::text,
                NEW.counted_at,
                'Auto-emitted from inventory_count_events ' || NEW.id::text
            );
            RETURN NEW;
        END;
        $$
    """)

    op.execute("""
        CREATE TRIGGER trg_count_event_emits_adjust
            AFTER INSERT ON inventory_count_events
            FOR EACH ROW EXECUTE FUNCTION fn_count_event_emits_adjust()
    """)
