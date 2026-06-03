"""Sprint 5 Phase 0c — tighten constraints (the isolated data-validating migration).

Revision ID: 0016_sprint5_tighten_constraints
Revises: 0015_sprint5_additive_columns
Create Date: 2026-06-03 00:00:00.000000

Per sprint-5-phase-map.md Phase 0c and v5 §6 "Migration N+1". This is THE one
data-validating migration in the Sprint-5 Phase-0 deploy window (Migration Risk
Standard §4.1 isolation rule). It enforces the invariants that 0015 deliberately
left loose:

  recipe_versions   : NOT NULL on recipe_id, version_number, yield_quantity;
                      UNIQUE (recipe_id, version_number)
  recipe_ingredients: NOT NULL on unit; canonical-unit CHECK
  sale_line_items   : depletion_status ↔ depletion_reason consistency CHECK

Two deliberate departures from v5's literal N+1 list:

  • sale_line_items.depletion_status SET NOT NULL is NOT done here. 0016 runs in
    Phase 0, before the Phase 9 handler exists; depletion_status has no default
    (review change 1), and the OLD worker keeps inserting rows that leave it NULL.
    A NOT NULL here would make those inserts fail and break the live worker. It is
    deferred to a post-Phase-9 migration, after SET DEFAULT 'pending' lands and a
    backfill runs. The consistency CHECK below is still valid meanwhile because a
    NULL status makes the CHECK evaluate to UNKNOWN, which passes.

  • sale_line_items.is_refunded SET NOT NULL is a no-op — it has been NOT NULL
    DEFAULT false since 0006. Skipped.

Migration Risk Standard §3 — a preflight runs FIRST: it counts every would-be
violation across all tightenings and RAISEs with a per-rule breakdown if any
exist, so the migration stops before changing schema (§3.3) and the failure is
self-explanatory (§8). On the current prod DB every target table is empty, so the
preflight passes trivially — but it makes the migration safe on any data state.

§4.5 application-impact verification: no current code inserts recipe_versions or
recipe_ingredients — their first writer is the Phase 4 confirm flow, which supplies
all the now-required columns. The sale_line_items consistency CHECK passes for the
NULL-status cutover rows the pre-Phase-9 worker writes, so it does not break the
live worker.

Risk classification (Migration Risk Standard §1.2):
  - Data validity:            Medium — this migration validates existing data;
                                       the §3 preflight counts-and-stops on any
                                       violation (none in prod: tables empty).
  - Availability impact:      Medium — SET NOT NULL / ADD CHECK take a brief
                                       ACCESS EXCLUSIVE lock and scan to validate.
                                       Trivial at pilot scale (tiny tables); a
                                       NOT VALID + VALIDATE split is the documented
                                       zero-downtime path if volume ever warrants.
  - Application compatibility: Low   — first writer of the constrained columns is
                                       Phase 4 code (lands after); no live code
                                       inserts these rows today (§4.5 verified).
  - Data propagation risk:    Low   — no replication-affecting change.
  - Reversibility:            Low   — downgrade drops every constraint and relaxes
                                       the columns back to 0015 state.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016_sprint5_tighten_constraints"
down_revision: str | None = "0015_sprint5_additive_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Canonical unit allowlist — must match modifier_ingredients.unit (0014) and
# app/modules/inventory/depletion/units.py.
_UNITS: tuple[str, ...] = (
    "g", "kg", "oz_weight", "lb",
    "ml", "L", "fl_oz", "cup", "tsp", "tbsp",
    "ea", "dozen",
)
_UNIT_CHECK = "(" + ", ".join(f"'{u}'" for u in _UNITS) + ")"


def upgrade() -> None:
    # ═════════════════════════════════════════════════════════════════════════
    # PREFLIGHT (Migration Risk Standard §3) — detect + count + stop with
    # diagnostics BEFORE any schema change. Accumulates every rule's violation
    # count, then RAISEs once with the full breakdown so the operator sees all
    # problems at once, not one-at-a-time.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute(f"""
        DO $$
        DECLARE
            v_msg text := '';
            n     bigint;
        BEGIN
            SELECT count(*) INTO n FROM recipe_versions WHERE recipe_id IS NULL;
            IF n > 0 THEN v_msg := v_msg || format('  recipe_versions.recipe_id NULL: %s%s', n, chr(10)); END IF;

            SELECT count(*) INTO n FROM recipe_versions WHERE version_number IS NULL;
            IF n > 0 THEN v_msg := v_msg || format('  recipe_versions.version_number NULL: %s%s', n, chr(10)); END IF;

            SELECT count(*) INTO n FROM recipe_versions WHERE yield_quantity IS NULL;
            IF n > 0 THEN v_msg := v_msg || format('  recipe_versions.yield_quantity NULL: %s%s', n, chr(10)); END IF;

            SELECT count(*) INTO n FROM (
                SELECT recipe_id, version_number
                FROM recipe_versions
                WHERE recipe_id IS NOT NULL AND version_number IS NOT NULL
                GROUP BY recipe_id, version_number HAVING count(*) > 1
            ) d;
            IF n > 0 THEN v_msg := v_msg || format('  recipe_versions duplicate (recipe_id, version_number): %s%s', n, chr(10)); END IF;

            SELECT count(*) INTO n FROM recipe_ingredients WHERE unit IS NULL;
            IF n > 0 THEN v_msg := v_msg || format('  recipe_ingredients.unit NULL: %s%s', n, chr(10)); END IF;

            SELECT count(*) INTO n FROM recipe_ingredients
             WHERE unit IS NOT NULL AND unit NOT IN {_UNIT_CHECK};
            IF n > 0 THEN v_msg := v_msg || format('  recipe_ingredients.unit non-canonical: %s%s', n, chr(10)); END IF;

            SELECT count(*) INTO n FROM sale_line_items
             WHERE depletion_status IS NOT NULL
               AND NOT (
                    (depletion_status IN ('pending','depleted') AND depletion_reason IS NULL)
                 OR (depletion_status IN ('unmapped','skipped','failed') AND depletion_reason IS NOT NULL)
               );
            IF n > 0 THEN v_msg := v_msg || format('  sale_line_items status/reason inconsistent: %s%s', n, chr(10)); END IF;

            IF v_msg <> '' THEN
                RAISE EXCEPTION E'Migration 0016 preflight FAILED. Fix these rows before upgrading:\n%', v_msg;
            END IF;
        END $$;
    """)

    # ═════════════════════════════════════════════════════════════════════════
    # recipe_versions — enforce the versioned model.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("ALTER TABLE recipe_versions ALTER COLUMN recipe_id SET NOT NULL")
    op.execute("ALTER TABLE recipe_versions ALTER COLUMN version_number SET NOT NULL")
    op.execute("ALTER TABLE recipe_versions ALTER COLUMN yield_quantity SET NOT NULL")
    op.execute("""
        ALTER TABLE recipe_versions
            ADD CONSTRAINT recipe_versions_unique_version
            UNIQUE (recipe_id, version_number)
    """)

    # ═════════════════════════════════════════════════════════════════════════
    # recipe_ingredients — unit required + canonical (mirrors modifier_ingredients).
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("ALTER TABLE recipe_ingredients ALTER COLUMN unit SET NOT NULL")
    op.execute(f"""
        ALTER TABLE recipe_ingredients
            ADD CONSTRAINT recipe_ingredients_unit_canonical
            CHECK (unit IN {_UNIT_CHECK})
    """)

    # ═════════════════════════════════════════════════════════════════════════
    # sale_line_items — status/reason consistency.
    #   pending/depleted  → reason IS NULL
    #   unmapped/skipped/failed → reason IS NOT NULL
    # NULL depletion_status (pre-Phase-9 cutover rows) → CHECK is UNKNOWN → passes.
    # depletion_status SET NOT NULL is deferred (see module docstring).
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        ALTER TABLE sale_line_items
            ADD CONSTRAINT depletion_status_reason_consistency CHECK (
                (depletion_status IN ('pending', 'depleted') AND depletion_reason IS NULL)
                OR
                (depletion_status IN ('unmapped', 'skipped', 'failed') AND depletion_reason IS NOT NULL)
            )
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE sale_line_items DROP CONSTRAINT IF EXISTS depletion_status_reason_consistency")

    op.execute("ALTER TABLE recipe_ingredients DROP CONSTRAINT IF EXISTS recipe_ingredients_unit_canonical")
    op.execute("ALTER TABLE recipe_ingredients ALTER COLUMN unit DROP NOT NULL")

    op.execute("ALTER TABLE recipe_versions DROP CONSTRAINT IF EXISTS recipe_versions_unique_version")
    op.execute("ALTER TABLE recipe_versions ALTER COLUMN yield_quantity DROP NOT NULL")
    op.execute("ALTER TABLE recipe_versions ALTER COLUMN version_number DROP NOT NULL")
    op.execute("ALTER TABLE recipe_versions ALTER COLUMN recipe_id DROP NOT NULL")
