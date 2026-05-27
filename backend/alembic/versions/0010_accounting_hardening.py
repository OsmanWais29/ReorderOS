"""Accounting hardening — Phase 1 of Sprint 3-4 improvement plan.

Changes:
  1. inventory_movements.yield_factor_applied (NUMERIC, nullable)
       Snapshots the yield factor used at depletion write time.
       Depletion replay reads this column; changing inventory_yield_factors
       after the fact no longer mutates historical movement deltas.

  2. inventory_movements.accounted_at (TIMESTAMPTZ, nullable)
       Reserved for future replay/dead-letter tooling.  NULL today means
       "use created_at as the accounting boundary."  When event-repair
       tooling recreates rows it will set accounted_at to the original
       acceptance time so created_at can reflect the new insertion.
       No production code reads this column until that tooling exists.

  3. inventory_count_events.reconciliation_cutoff_created_at (TIMESTAMPTZ, nullable)
       Stores the system ingestion watermark captured at the moment a count
       is finalised.  on_hand() for Mode B items filters sale_signals by
         created_at > reconciliation_cutoff_created_at
       instead of
         recorded_at > last_count_at
       NULL on pre-0010 rows; service falls back to recorded_at filter for
       those rows (backwards compatible).

  4. inventory_movements.movement_type CHECK constraint expansion
       Adds 'sale_depletion_reversal' alongside existing 'sale_signal_reversal'.
       Using an explicit reversal type preserves audit provenance — analytics,
       reconciliation tooling, and anomaly detection can distinguish a reversal
       from a generic adjustment.

  5. idx_inv_movements_created_at index
       Supports the late-signal alert query:
         WHERE created_at > reconciliation_cutoff AND recorded_at < last_count_at

See docs/inventory_accounting_semantics.md for the full accounting philosophy
these changes implement.

Revision ID: 0010_accounting_hardening
Revises: 0009_retire_db_business_logic
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_accounting_hardening"
down_revision: str | None = "0009_retire_db_business_logic"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── 1. New columns on inventory_movements ────────────────────────────────

    op.execute("""
        ALTER TABLE inventory_movements
            ADD COLUMN yield_factor_applied NUMERIC,
            ADD COLUMN accounted_at         TIMESTAMPTZ
    """)

    # ── 2. Reconciliation watermark on inventory_count_events ─────────────────

    op.execute("""
        ALTER TABLE inventory_count_events
            ADD COLUMN reconciliation_cutoff_created_at TIMESTAMPTZ
    """)

    # ── 3. Expand movement_type CHECK constraint ──────────────────────────────
    #
    # Drop the auto-named inline constraint from 0003_inventory_ledger, then
    # recreate it with 'sale_depletion_reversal' added.  The constraint name
    # (inventory_movements_movement_type_check) was verified against the live
    # schema before writing this migration.

    op.execute("""
        ALTER TABLE inventory_movements
            DROP CONSTRAINT inventory_movements_movement_type_check
    """)

    op.execute("""
        ALTER TABLE inventory_movements
            ADD CONSTRAINT inventory_movements_movement_type_check
            CHECK (movement_type = ANY (ARRAY[
                'opening_balance',
                'receive',
                'sale_depletion',
                'sale_depletion_reversal',
                'sale_signal',
                'sale_signal_reversal',
                'count_adjust',
                'waste',
                'transfer_in',
                'transfer_out',
                'adjustment'
            ]))
    """)

    # ── 4. Index for late-signal alert query ──────────────────────────────────

    op.execute("""
        CREATE INDEX idx_inv_movements_created_at
            ON inventory_movements (tenant_id, inventory_item_id, created_at DESC)
    """)


def downgrade() -> None:
    # Drop index
    op.execute("DROP INDEX IF EXISTS idx_inv_movements_created_at")

    # Revert CHECK constraint to pre-0010 set (no sale_depletion_reversal)
    op.execute("""
        ALTER TABLE inventory_movements
            DROP CONSTRAINT inventory_movements_movement_type_check
    """)
    op.execute("""
        ALTER TABLE inventory_movements
            ADD CONSTRAINT inventory_movements_movement_type_check
            CHECK (movement_type = ANY (ARRAY[
                'opening_balance',
                'receive',
                'sale_depletion',
                'sale_signal',
                'sale_signal_reversal',
                'count_adjust',
                'waste',
                'transfer_in',
                'transfer_out',
                'adjustment'
            ]))
    """)

    # Drop new columns
    op.execute("""
        ALTER TABLE inventory_movements
            DROP COLUMN IF EXISTS yield_factor_applied,
            DROP COLUMN IF EXISTS accounted_at
    """)

    op.execute("""
        ALTER TABLE inventory_count_events
            DROP COLUMN IF EXISTS reconciliation_cutoff_created_at
    """)
