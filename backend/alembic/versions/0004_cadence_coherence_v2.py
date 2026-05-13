"""tighten cadence_coherence CHECK to enforce Mode B operational requirements.

Revision ID: 0004_cadence_coherence_v2
Revises: 0003_inventory_ledger
Create Date: 2026-05-13 00:00:00.000000

v5 constraint rationale
-----------------------
A Mode B item without count_cadence_days/count_grace_days is operationally
broken: count_state can never be computed, the confidence state machine does
not advance, and the nightly integrity job will skip it silently.  The
previous constraint only enforced NULL cadence/grace for Mode A; it left
Mode B items free to omit cadence — creating silent data zombies.

New rules
---------
  Mode A (recipe_deducted):
    count_cadence_days IS NULL AND count_grace_days IS NULL
    (Mode A never counts; cadence tracking is irrelevant.)

  Mode B (count_anchored):
    count_cadence_days IS NOT NULL
    count_grace_days IS NOT NULL
    count_grace_days >= count_cadence_days
    (Grace must cover at least one full cadence so the stale window is
     meaningful: stale = (cadence, cadence+grace], expired = > cadence+grace.)

Data migration
--------------
Existing Mode B rows that violate the new constraint are fixed before the
constraint is added.  We use conservative defaults so that no existing item
is silently skipped by the nightly job after this migration runs.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_cadence_coherence_v2"
down_revision: str | None = "0003_inventory_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Remove the Sprint 3 constraint first so the repair UPDATE below
    #    is not blocked by any existing violation.
    op.execute("ALTER TABLE inventory_items DROP CONSTRAINT IF EXISTS cadence_coherence")

    # 2. Repair existing Mode B rows before adding the tighter constraint.
    #    Two categories of violations:

    # 2a. Mode B items with NULL cadence or grace — set sensible defaults.
    op.execute("""
        UPDATE inventory_items
           SET count_cadence_days = 7,
               count_grace_days   = 7
         WHERE inventory_mode = 'count_anchored'
           AND (count_cadence_days IS NULL OR count_grace_days IS NULL)
    """)

    # 2b. Mode B items where grace < cadence — promote grace up to cadence.
    op.execute("""
        UPDATE inventory_items
           SET count_grace_days = count_cadence_days
         WHERE inventory_mode = 'count_anchored'
           AND count_cadence_days IS NOT NULL
           AND count_grace_days   IS NOT NULL
           AND count_grace_days < count_cadence_days
    """)

    # 3. Add the v5 constraint.
    op.execute("""
        ALTER TABLE inventory_items ADD CONSTRAINT cadence_coherence CHECK (
            (inventory_mode = 'recipe_deducted'
                AND count_cadence_days IS NULL
                AND count_grace_days   IS NULL)
            OR
            (inventory_mode = 'count_anchored'
                AND count_cadence_days IS NOT NULL
                AND count_grace_days   IS NOT NULL
                AND count_grace_days  >= count_cadence_days)
        )
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE inventory_items DROP CONSTRAINT IF EXISTS cadence_coherence")
    op.execute("""
        ALTER TABLE inventory_items ADD CONSTRAINT cadence_coherence CHECK (
            (inventory_mode = 'recipe_deducted'
                AND count_cadence_days IS NULL
                AND count_grace_days IS NULL)
            OR inventory_mode = 'count_anchored'
        )
    """)
