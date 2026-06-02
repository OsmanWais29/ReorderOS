"""Sprint 5 Phase 0a — new tables for recipe config + modifier depletion + units.

Revision ID: 0014_recipes_modifiers_units
Revises: 0013_security_hardening
Create Date: 2026-06-01 00:00:00.000000

Per sprint-5-phase-map.md Phase 0a. This migration is **metadata-only** under the
Migration Risk Standard §2.1 (new tables, indexes, RLS, grants — no validation of
existing data), so it is safe to batch. All inline NOT NULL / CHECK constraints sit
on brand-new empty tables and therefore validate nothing existing.

Risk classification (Migration Risk Standard §1.2):
  - Data validity:            Low  — all new tables, zero existing rows touched.
  - Availability impact:      Low  — CREATE TABLE/INDEX only; no locks on live tables.
  - Application compatibility: Low  — additive; no app yet reads/writes these tables.
  - Data propagation risk:    Low  — no replication-affecting change.
  - Reversibility:            Low  — full DROP in downgrade(); no data loss possible.

Scope (10 new tables):
  recipes, recipe_drafts, recipe_llm_suggestions,
  modifiers, modifier_versions, modifier_ingredients, modifier_drafts,
  modifier_llm_suggestions, sale_line_item_modifiers,
  unit_conversions (three-tier global/tenant/item; global tier seeded inline).

RLS posture: ENABLE + tenant policies, NO FORCE — matching the business-table
convention established in 0003/0006/0011 (FORCE is reserved for the core-identity
tables from 0002). If the team later hardens to FORCE, that should be one
cross-table migration, not piecemeal here. (Flagged for review.)
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from alembic import op

revision: str = "0014_recipes_modifiers_units"
down_revision: str | None = "0013_security_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tenant-isolation predicate — identical to the project-wide convention
# (0011/0013). NULLIF(...,'') turns an unset/cleared session var into NULL so
# the ::uuid cast never raises and unset context safely matches zero rows.
_T1 = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"

# Canonical unit allowlist (v5 §6). Enforced at the DB layer here, mirrored by
# app/modules/inventory/depletion/units.py and the API/UI validators (gate 36).
# 'oz' is intentionally absent — use 'oz_weight' or 'fl_oz'.
_UNITS: tuple[str, ...] = (
    "g", "kg", "oz_weight", "lb",        # weight
    "ml", "L", "fl_oz", "cup", "tsp", "tbsp",  # volume
    "ea", "dozen",                       # count
)
_UNIT_CHECK = "(" + ", ".join(f"'{u}'" for u in _UNITS) + ")"

# Base-unit factors per dimension (value of 1 unit expressed in the dimension
# base). unit_conversions is seeded with every same-dimension ordered pair, so
# the convert() service is a single lookup. Cross-dimension pairs are absent →
# convert() raises ConversionError (v5: g→ml is a domain error).
_BASE_FACTORS: dict[str, dict[str, Decimal]] = {
    "weight": {
        "g": Decimal("1"),
        "kg": Decimal("1000"),
        "oz_weight": Decimal("28.349523125"),
        "lb": Decimal("453.59237"),
    },
    "volume": {
        "ml": Decimal("1"),
        "L": Decimal("1000"),
        "fl_oz": Decimal("29.5735295625"),
        "cup": Decimal("236.5882365"),
        "tsp": Decimal("4.92892159375"),
        "tbsp": Decimal("14.78676478125"),
    },
    "count": {
        "ea": Decimal("1"),
        "dozen": Decimal("12"),
    },
}


def upgrade() -> None:  # noqa: PLR0915
    # ═════════════════════════════════════════════════════════════════════════
    # RECIPES — one row per menu item (parent of versioned recipe_versions).
    # status holds the draft/confirmed/skipped lifecycle; menu_items.recipe_version_id
    # (from 0012) points at the confirmed version. UNIQUE(tenant_id, menu_item_id)
    # enforces "one recipe per menu item per tenant".
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE recipes (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id     uuid NOT NULL REFERENCES tenants(id),
            menu_item_id  uuid NOT NULL REFERENCES menu_items(id),
            status        text NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft', 'confirmed', 'skipped')),
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_recipes_menu_item UNIQUE (tenant_id, menu_item_id)
        )
    """)
    op.execute("CREATE INDEX idx_recipes_tenant_status ON recipes (tenant_id, status)")
    op.execute("ALTER TABLE recipes ENABLE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY recipes_tenant_isolation ON recipes
            FOR ALL TO app_user
            USING ({_T1}) WITH CHECK ({_T1})
    """)
    # No DELETE — recipes move between statuses, they are not deleted.
    op.execute("GRANT SELECT, INSERT, UPDATE ON recipes TO app_user")

    # ═════════════════════════════════════════════════════════════════════════
    # RECIPE_DRAFTS — working area for the editable draft (field-blur auto-save,
    # v5 §1). draft_ingredients is the JSONB working list; on confirm it becomes
    # recipe_ingredients rows under a new recipe_versions row, then the draft is
    # deleted (v5 §7 step 6). parent_recipe_version_id is set on un-confirm to
    # snapshot which confirmed version was reopened (v5 §1).
    # One active draft per recipe.
    #
    # INVARIANT (fail-gate level, decision 4): the depletion engine must NEVER
    # read draft tables. Drafts are mutable scratch space; confirm validates and
    # materializes them into IMMUTABLE recipe_versions/modifier_versions, which
    # are the only thing depletion reads. The Phase 14 CI guard is extended to
    # fail if anything under depletion/ imports or queries *_drafts.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE recipe_drafts (
            id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id                uuid NOT NULL REFERENCES tenants(id),
            recipe_id                uuid NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            parent_recipe_version_id uuid REFERENCES recipe_versions(id),
            draft_ingredients        jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at               timestamptz NOT NULL DEFAULT now(),
            updated_at               timestamptz NOT NULL DEFAULT now(),
            created_by               uuid REFERENCES users(id),
            CONSTRAINT uq_recipe_drafts_recipe UNIQUE (tenant_id, recipe_id)
        )
    """)
    op.execute("ALTER TABLE recipe_drafts ENABLE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY recipe_drafts_tenant_isolation ON recipe_drafts
            FOR ALL TO app_user
            USING ({_T1}) WITH CHECK ({_T1})
    """)
    # DELETE needed — confirm() removes the draft after promoting it.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON recipe_drafts TO app_user")

    # ═════════════════════════════════════════════════════════════════════════
    # RECIPE_LLM_SUGGESTIONS — append-only record of LLM base-recipe inference
    # (v5 §2). Depletion never reads this; only the operator-confirmed draft path
    # promotes a suggestion. No UPDATE/DELETE grant → append-only.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE recipe_llm_suggestions (
            id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id             uuid NOT NULL REFERENCES tenants(id),
            menu_item_id          uuid NOT NULL REFERENCES menu_items(id),
            suggested_ingredients jsonb NOT NULL,
            model_version         text NOT NULL,
            confidence            text NOT NULL
                                  CHECK (confidence IN ('confident', 'likely', 'uncertain')),
            inferred_at           timestamptz NOT NULL DEFAULT now(),
            created_at            timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX idx_recipe_llm_suggestions_menu_item
            ON recipe_llm_suggestions (tenant_id, menu_item_id, created_at DESC)
    """)
    op.execute("ALTER TABLE recipe_llm_suggestions ENABLE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY recipe_llm_suggestions_tenant_isolation ON recipe_llm_suggestions
            FOR ALL TO app_user
            USING ({_T1}) WITH CHECK ({_T1})
    """)
    op.execute("GRANT SELECT, INSERT ON recipe_llm_suggestions TO app_user")

    # ═════════════════════════════════════════════════════════════════════════
    # MODIFIERS — POS-detected modifier catalog (v5 §4). Populated by the Clover
    # catalog sync (runs as service_worker) and confirmed by the operator
    # (app_user) — hence DUAL-role T1 RLS, same as orders/sale_line_items.
    # current_version_id mirrors menu_items.recipe_version_id: it points at the
    # confirmed modifier_versions row (NULL while draft/skipped).
    # Partial unique (tenant_id, menu_item_id, pos_modifier_id) dedups re-syncs.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE modifiers (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id          uuid NOT NULL REFERENCES tenants(id),
            menu_item_id       uuid NOT NULL REFERENCES menu_items(id),
            pos_modifier_id    text,
            name               text NOT NULL,
            modifier_type      text NOT NULL DEFAULT 'additive'
                               CHECK (modifier_type IN ('additive', 'subtractive', 'substitution')),
            status             text NOT NULL DEFAULT 'draft'
                               CHECK (status IN ('draft', 'confirmed', 'skipped')),
            current_version_id uuid,  -- FK added after modifier_versions exists (below)
            created_at         timestamptz NOT NULL DEFAULT now(),
            updated_at         timestamptz NOT NULL DEFAULT now()
        )
    """)
    # Re-sync dedup: a Clover modifier maps to exactly one row per menu item.
    op.execute("""
        CREATE UNIQUE INDEX uq_modifiers_pos
            ON modifiers (tenant_id, menu_item_id, pos_modifier_id)
            WHERE pos_modifier_id IS NOT NULL
    """)
    op.execute("CREATE INDEX idx_modifiers_menu_item ON modifiers (tenant_id, menu_item_id)")
    op.execute("ALTER TABLE modifiers ENABLE ROW LEVEL SECURITY")
    # Dual-role T1: catalog sync (service_worker) SETs app.tenant_id before write.
    op.execute(f"""
        CREATE POLICY modifiers_tenant_isolation ON modifiers
            FOR ALL TO app_user, service_worker
            USING ({_T1}) WITH CHECK ({_T1})
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE ON modifiers TO app_user")
    op.execute("GRANT SELECT, INSERT, UPDATE ON modifiers TO service_worker")

    # ═════════════════════════════════════════════════════════════════════════
    # MODIFIER_VERSIONS — immutable confirmed modifier versions (mirror of
    # recipe_versions). yield_quantity feeds the modifier depletion formula
    # (v5 §11). service_worker needs SELECT for the depletion walk (like
    # recipe_ingredients in 0011). Append-only: no UPDATE/DELETE.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE modifier_versions (
            id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id      uuid NOT NULL REFERENCES tenants(id),
            modifier_id    uuid NOT NULL REFERENCES modifiers(id),
            version_number integer NOT NULL,
            yield_quantity numeric(14, 4) NOT NULL DEFAULT 1 CHECK (yield_quantity > 0),
            valid_from     timestamptz NOT NULL DEFAULT now(),
            created_at     timestamptz NOT NULL DEFAULT now(),
            created_by     uuid REFERENCES users(id),
            CONSTRAINT uq_modifier_versions_number UNIQUE (modifier_id, version_number),
            -- FK target for modifiers' composite current-version pointer below.
            CONSTRAINT uq_modifier_versions_modifier_id UNIQUE (modifier_id, id)
        )
    """)
    # Wire modifiers.current_version_id → modifier_versions as a COMPOSITE FK on
    # (modifiers.id, current_version_id) → (modifier_versions.modifier_id, id).
    # This makes it impossible for current_version_id to point at a version that
    # belongs to a DIFFERENT modifier — a DB-level guard (decision 3, chosen over
    # a service-layer assertion). current_version_id is NULL while the modifier
    # is draft/skipped; MATCH SIMPLE skips the FK check when it is NULL.
    op.execute("""
        ALTER TABLE modifiers
            ADD CONSTRAINT modifiers_current_version_fk
            FOREIGN KEY (id, current_version_id)
            REFERENCES modifier_versions(modifier_id, id)
    """)
    op.execute("ALTER TABLE modifier_versions ENABLE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY modifier_versions_tenant_isolation ON modifier_versions
            FOR ALL TO app_user
            USING ({_T1}) WITH CHECK ({_T1})
    """)
    op.execute(f"""
        CREATE POLICY modifier_versions_select_sw ON modifier_versions
            FOR SELECT TO service_worker
            USING ({_T1})
    """)
    op.execute("GRANT SELECT, INSERT ON modifier_versions TO app_user")
    op.execute("GRANT SELECT ON modifier_versions TO service_worker")

    # ═════════════════════════════════════════════════════════════════════════
    # MODIFIER_INGREDIENTS — ingredients of a confirmed modifier version (mirror
    # of recipe_ingredients, incl. the canonical unit CHECK inline since the
    # table is new/empty). service_worker SELECT for the depletion walk.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute(f"""
        CREATE TABLE modifier_ingredients (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenants(id),
            modifier_version_id uuid NOT NULL REFERENCES modifier_versions(id),
            inventory_item_id   uuid NOT NULL REFERENCES inventory_items(id),
            quantity            numeric(14, 4) NOT NULL CHECK (quantity > 0),
            unit                text NOT NULL CHECK (unit IN {_UNIT_CHECK}),
            created_at          timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX idx_modifier_ingredients_version
            ON modifier_ingredients (modifier_version_id)
    """)
    op.execute("ALTER TABLE modifier_ingredients ENABLE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY modifier_ingredients_tenant_isolation ON modifier_ingredients
            FOR ALL TO app_user
            USING ({_T1}) WITH CHECK ({_T1})
    """)
    op.execute(f"""
        CREATE POLICY modifier_ingredients_select_sw ON modifier_ingredients
            FOR SELECT TO service_worker
            USING ({_T1})
    """)
    op.execute("GRANT SELECT, INSERT ON modifier_ingredients TO app_user")
    op.execute("GRANT SELECT ON modifier_ingredients TO service_worker")

    # ═════════════════════════════════════════════════════════════════════════
    # MODIFIER_DRAFTS — working area for modifier edits (mirror of recipe_drafts).
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE modifier_drafts (
            id                         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id                  uuid NOT NULL REFERENCES tenants(id),
            modifier_id                uuid NOT NULL REFERENCES modifiers(id) ON DELETE CASCADE,
            parent_modifier_version_id uuid REFERENCES modifier_versions(id),
            draft_ingredients          jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at                 timestamptz NOT NULL DEFAULT now(),
            updated_at                 timestamptz NOT NULL DEFAULT now(),
            created_by                 uuid REFERENCES users(id),
            CONSTRAINT uq_modifier_drafts_modifier UNIQUE (tenant_id, modifier_id)
        )
    """)
    op.execute("ALTER TABLE modifier_drafts ENABLE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY modifier_drafts_tenant_isolation ON modifier_drafts
            FOR ALL TO app_user
            USING ({_T1}) WITH CHECK ({_T1})
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON modifier_drafts TO app_user")

    # ═════════════════════════════════════════════════════════════════════════
    # MODIFIER_LLM_SUGGESTIONS — append-only LLM modifier inference (v5 §2).
    # modifier_id nullable: the suggestion may be produced before/independent of
    # a matched modifiers row (matched later by name). Append-only.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE modifier_llm_suggestions (
            id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id             uuid NOT NULL REFERENCES tenants(id),
            menu_item_id          uuid NOT NULL REFERENCES menu_items(id),
            modifier_id           uuid REFERENCES modifiers(id),
            modifier_name         text NOT NULL,
            suggested_ingredients jsonb NOT NULL,
            model_version         text NOT NULL,
            confidence            text NOT NULL
                                  CHECK (confidence IN ('confident', 'likely', 'uncertain')),
            inferred_at           timestamptz NOT NULL DEFAULT now(),
            created_at            timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX idx_modifier_llm_suggestions_menu_item
            ON modifier_llm_suggestions (tenant_id, menu_item_id, created_at DESC)
    """)
    op.execute("ALTER TABLE modifier_llm_suggestions ENABLE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY modifier_llm_suggestions_tenant_isolation ON modifier_llm_suggestions
            FOR ALL TO app_user
            USING ({_T1}) WITH CHECK ({_T1})
    """)
    op.execute("GRANT SELECT, INSERT ON modifier_llm_suggestions TO app_user")

    # ═════════════════════════════════════════════════════════════════════════
    # SALE_LINE_ITEM_MODIFIERS — per-sale snapshot of depletion-relevant
    # modifiers applied to a line (v5 §6). Written by the depletion worker at
    # sale time (service_worker), read by the walker. Dual-role T1 RLS like
    # sale_line_items.
    #
    # modifier_version_id is NOT NULL because this table stores ONLY confirmed,
    # additive modifiers (edit 6). When the worker processes a Clover sale it
    # INTENTIONALLY SKIPS — writes NO row for — modifiers that are:
    #   - unconfirmed (modifiers.status != 'confirmed'), or
    #   - non-additive (subtractive / substitution).
    # Example: a sale with "Extra shot" (confirmed additive) + "No foam"
    # (subtractive) yields ONE row, for "Extra shot" only.
    #
    # CONSEQUENCE (documented so a future dev doesn't misread it): the rows here
    # are a SUBSET of the modifiers Clover reported, not a complete modifier
    # record. A full raw-modifier audit (every observed modifier, incl.
    # unsupported ones) is a future sprint with its own table.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE sale_line_item_modifiers (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenants(id),
            sale_line_item_id   uuid NOT NULL REFERENCES sale_line_items(id),
            modifier_id         uuid NOT NULL REFERENCES modifiers(id),
            modifier_version_id uuid NOT NULL REFERENCES modifier_versions(id),
            quantity            numeric(14, 4) NOT NULL DEFAULT 1 CHECK (quantity > 0),
            pos_modifier_id     text,
            created_at          timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX sale_line_item_modifiers_sli_idx
            ON sale_line_item_modifiers (sale_line_item_id)
    """)
    op.execute("ALTER TABLE sale_line_item_modifiers ENABLE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY slim_tenant_isolation ON sale_line_item_modifiers
            FOR ALL TO app_user, service_worker
            USING ({_T1}) WITH CHECK ({_T1})
    """)
    op.execute("GRANT SELECT ON sale_line_item_modifiers TO app_user")
    op.execute("GRANT SELECT, INSERT ON sale_line_item_modifiers TO service_worker")

    # ═════════════════════════════════════════════════════════════════════════
    # UNIT_CONVERSIONS — three-tier conversion table (global / tenant / item).
    #
    # Surrogate PK + nullable tenant_id/inventory_item_id so the SAME unit pair
    # can carry DIFFERENT factors at different scopes. This is required because
    # weight↔volume conversions are density-dependent and per-ingredient:
    #   1 cup flour ≈ 120 g, 1 cup sugar ≈ 200 g, 1 cup water ≈ 237 g.
    # A composite PK(from_unit, to_unit) would allow only ONE cup→g globally and
    # structurally block per-ingredient density — which v5 §6 explicitly requires
    # ("a weight→volume conversion must have a non-NULL inventory_item_id").
    #
    # Three tiers; the conversions service resolves MOST-SPECIFIC-WINS:
    #   global (tenant NULL, item NULL) — dimensionally-pure: g↔kg, ml↔L, ...
    #   tenant (tenant SET,  item NULL) — a restaurant's house convention
    #   item   (tenant SET,  item SET ) — cup flour → g for THIS item
    # Precedence (item → tenant → global) lives in the Phase 1 conversions
    # service, NOT in this schema. Seeds below populate the GLOBAL tier only.
    # factor multiplies a from_unit qty to yield the to_unit qty:
    #   to_qty = from_qty * factor.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute(f"""
        CREATE TABLE unit_conversions (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         uuid REFERENCES tenants(id),
            inventory_item_id uuid REFERENCES inventory_items(id),
            from_unit         text NOT NULL CHECK (from_unit IN {_UNIT_CHECK}),
            to_unit           text NOT NULL CHECK (to_unit IN {_UNIT_CHECK}),
            dimension         text NOT NULL
                              CHECK (dimension IN ('weight', 'volume', 'count')),
            factor            numeric(18, 8) NOT NULL CHECK (factor > 0),
            created_at        timestamptz NOT NULL DEFAULT now(),
            -- An item-specific override always belongs to a tenant
            -- (inventory_items are tenant-scoped).
            CONSTRAINT unit_conversions_item_requires_tenant
                CHECK (inventory_item_id IS NULL OR tenant_id IS NOT NULL)
        )
    """)
    # One conversion per unit pair PER TIER (partial uniques, one per tier).
    op.execute("""
        CREATE UNIQUE INDEX unit_conversions_global_uniq
            ON unit_conversions (from_unit, to_unit)
            WHERE tenant_id IS NULL AND inventory_item_id IS NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX unit_conversions_tenant_uniq
            ON unit_conversions (tenant_id, from_unit, to_unit)
            WHERE tenant_id IS NOT NULL AND inventory_item_id IS NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX unit_conversions_item_uniq
            ON unit_conversions (inventory_item_id, from_unit, to_unit)
            WHERE inventory_item_id IS NOT NULL
    """)
    # Supports the walker's precedence lookup (item → tenant → global).
    op.execute("""
        CREATE INDEX unit_conversions_lookup
            ON unit_conversions (from_unit, to_unit, inventory_item_id, tenant_id)
    """)

    # Seed the GLOBAL tier (tenant_id NULL, inventory_item_id NULL): every
    # same-dimension ordered pair incl. identity, computed from the base map.
    # Cross-dimension pairs are intentionally absent → ConversionError unless a
    # tenant/item density override is later supplied.
    rows: list[str] = []
    for dimension, units in _BASE_FACTORS.items():
        for from_unit, from_base in units.items():
            for to_unit, to_base in units.items():
                factor = from_base / to_base
                rows.append(f"('{from_unit}', '{to_unit}', '{dimension}', {factor:f})")
    op.execute(
        "INSERT INTO unit_conversions (from_unit, to_unit, dimension, factor) VALUES "
        + ", ".join(rows)
    )

    # RLS — a tenant reads GLOBAL rows + its OWN overrides, never another
    # tenant's. (unit_conversions now carries tenant_id, so it needs RLS unlike
    # a pure-global reference table.)
    op.execute("ALTER TABLE unit_conversions ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY unit_conversions_read ON unit_conversions
            FOR SELECT TO app_user
            USING (tenant_id IS NULL
                   OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)
    op.execute("""
        CREATE POLICY unit_conversions_read_sw ON unit_conversions
            FOR SELECT TO service_worker
            USING (tenant_id IS NULL
                   OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)
    # SELECT only in Sprint 5: global seeds are migration-owned; tenant/item
    # override WRITES land with their UI in a later sprint (grants added then).
    op.execute("GRANT SELECT ON unit_conversions TO app_user")
    op.execute("GRANT SELECT ON unit_conversions TO service_worker")


def downgrade() -> None:
    # Reverse dependency order. CASCADE not needed — drop children before parents.
    op.execute("DROP TABLE IF EXISTS unit_conversions")
    op.execute("DROP TABLE IF EXISTS sale_line_item_modifiers")
    op.execute("DROP TABLE IF EXISTS modifier_llm_suggestions")
    op.execute("DROP TABLE IF EXISTS modifier_drafts")
    op.execute("DROP TABLE IF EXISTS modifier_ingredients")
    # modifiers ↔ modifier_versions have a circular FK (current_version_id);
    # drop the FK first so the tables can be dropped in either order.
    op.execute("ALTER TABLE modifiers DROP CONSTRAINT IF EXISTS modifiers_current_version_fk")
    op.execute("DROP TABLE IF EXISTS modifier_versions")
    op.execute("DROP TABLE IF EXISTS modifiers")
    op.execute("DROP TABLE IF EXISTS recipe_llm_suggestions")
    op.execute("DROP TABLE IF EXISTS recipe_drafts")
    op.execute("DROP TABLE IF EXISTS recipes")
