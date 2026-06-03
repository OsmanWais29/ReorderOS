"""Sprint 5 Phase 0b — additive nullable columns on existing tables.

Revision ID: 0015_sprint5_additive_columns
Revises: 0014_recipes_modifiers_units
Create Date: 2026-06-02 00:00:00.000000

Per sprint-5-phase-map.md Phase 0b and v5 §6 "Migration N (additive)". This is the
ADDITIVE half of the recipe/depletion column work: columns are added nullable (or
with a constant DEFAULT that satisfies its own single-column CHECK), so NO existing
row can violate anything. The TIGHTENING half — NOT NULL, the depletion
status/reason consistency CHECK, the canonical-unit CHECK, and UNIQUE(recipe_id,
version_number) — is the isolated data-validating migration 0016 (Phase 0c).

`sale_line_items.is_refunded` is intentionally NOT added here — it already exists
from migration 0006 (verified 2026-05-31).

`sale_line_items.depletion_status` is added with NO default (not even 'pending'):
a default would stamp cutover-window rows — inserted by the pre-Phase-9 worker that
knows nothing about the column — as permanently 'pending' and flood the F5.1
stuck-pending monitor. SET DEFAULT 'pending' lands in Phase 9 (when the new handler
owns the lifecycle and sets it explicitly at INSERT); SET NOT NULL only after that
+ backfill. See the column comment for the full reasoning.

Risk classification (Migration Risk Standard §1.2):
  - Data validity:            Low — new columns; existing rows take a default/NULL
                                    that satisfies every constraint added here.
  - Availability impact:      Low — constant DEFAULTs (PG11+) are metadata-only,
                                    no table rewrite, no long lock.
  - Application compatibility: Low — additive; current code ignores the columns.
  - Data propagation risk:    Low — no replication-affecting change.
  - Reversibility:            Low — columns dropped in downgrade (see note on the
                                    recipe_versions.name NOT NULL restore).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015_sprint5_additive_columns"
down_revision: str | None = "0014_recipes_modifiers_units"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ═════════════════════════════════════════════════════════════════════════
    # recipe_versions — promote the 0003 stub toward the versioned model.
    #   recipe_id      → FK to recipes (0014); NULL on existing rows (none in prod).
    #   version_number → monotonic per recipe; NOT NULL + UNIQUE deferred to 0016.
    #   yield_quantity → divisor in the depletion formula; DEFAULT 1 so existing
    #                    rows satisfy the inline CHECK(>0). NOT NULL deferred.
    #   name           → relaxed to nullable; the versioned model no longer uses
    #                    it (kept, not dropped — v5 §6).
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("ALTER TABLE recipe_versions ADD COLUMN recipe_id uuid REFERENCES recipes(id)")
    op.execute("ALTER TABLE recipe_versions ADD COLUMN version_number integer")
    op.execute("""
        ALTER TABLE recipe_versions
            ADD COLUMN yield_quantity numeric(14, 4) DEFAULT 1
            CONSTRAINT recipe_versions_yield_positive CHECK (yield_quantity > 0)
    """)
    op.execute("ALTER TABLE recipe_versions ALTER COLUMN name DROP NOT NULL")

    # ═════════════════════════════════════════════════════════════════════════
    # recipe_ingredients — add the ingredient unit (bare/nullable here). The
    # canonical-unit CHECK and NOT NULL land in 0016 (v5 §6 Migration N+1).
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("ALTER TABLE recipe_ingredients ADD COLUMN unit text")

    # ═════════════════════════════════════════════════════════════════════════
    # sale_line_items — depletion lifecycle tracking.
    #   depletion_status → added WITHOUT a default, on purpose. 0015 applies before
    #     the new depletion handler (Phase 9). Between the two, the OLD worker keeps
    #     inserting sale_line_items rows knowing nothing about depletion_status. A
    #     DEFAULT 'pending' would stamp every cutover-window row 'pending' forever
    #     (old worker has no transition logic) → the F5.1 stuck-pending monitor
    #     (idx_sli_depletion_pending) would fire on all of them, burying the real
    #     "handler crashed mid-depletion" signal under false positives. With no
    #     default those rows get NULL — outside the monitored set
    #     (WHERE depletion_status = 'pending') — which is correct: they are
    #     pre-lifecycle, not stuck. The enum CHECK stays (NULL passes an IN-CHECK).
    #     Sequenced later: SET DEFAULT 'pending' lands in Phase 9 (when the handler
    #     owns the lifecycle); SET NOT NULL only AFTER that + backfill/preflight.
    #     The Phase 9 handler sets depletion_status='pending' EXPLICITLY at INSERT —
    #     the default is a safety net, never the mechanism.
    #   depletion_reason → nullable; CHECK allows NULL (the pending/depleted case).
    #                      The status↔reason consistency CHECK is in 0016.
    #   is_refunded      → SKIPPED, already exists since 0006.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        ALTER TABLE sale_line_items
            ADD COLUMN depletion_status text
            CONSTRAINT sale_line_items_depletion_status_valid
            CHECK (depletion_status IN
                   ('pending', 'depleted', 'unmapped', 'skipped', 'failed'))
    """)
    op.execute("""
        ALTER TABLE sale_line_items
            ADD COLUMN depletion_reason text
            CONSTRAINT sale_line_items_depletion_reason_valid
            CHECK (depletion_reason IS NULL OR depletion_reason IN
                   ('recipe_draft', 'recipe_skipped', 'no_recipe', 'invalid_recipe',
                    'missing_conversion', 'computation_error', 'sale_ineligible',
                    'line_refunded'))
    """)
    # Partial index: the F5.1 "stuck pending" monitoring query scans pending rows.
    op.execute("""
        CREATE INDEX idx_sli_depletion_pending
            ON sale_line_items (tenant_id, created_at)
            WHERE depletion_status = 'pending'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_sli_depletion_pending")
    op.execute("ALTER TABLE sale_line_items DROP COLUMN IF EXISTS depletion_reason")
    op.execute("ALTER TABLE sale_line_items DROP COLUMN IF EXISTS depletion_status")

    op.execute("ALTER TABLE recipe_ingredients DROP COLUMN IF EXISTS unit")

    op.execute("ALTER TABLE recipe_versions DROP COLUMN IF EXISTS yield_quantity")
    op.execute("ALTER TABLE recipe_versions DROP COLUMN IF EXISTS version_number")
    op.execute("ALTER TABLE recipe_versions DROP COLUMN IF EXISTS recipe_id")
    # Restore the 0003 NOT NULL on name. 0015 dropped that constraint, so rows MAY
    # have been created with name = NULL while it was applied (the new model doesn't
    # use name). Backfill those NULLs before re-adding NOT NULL so the downgrade is
    # correct regardless of data state — a downgrade must not break under conditions
    # the upgrade explicitly made possible. (Rollback restores the constraint, not
    # historical name values — Migration Risk Standard §6.)
    op.execute("""
        UPDATE recipe_versions
           SET name = COALESCE(name, 'legacy recipe version')
         WHERE name IS NULL
    """)
    op.execute("ALTER TABLE recipe_versions ALTER COLUMN name SET NOT NULL")
