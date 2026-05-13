"""inventory ledger core — Sprint 3 exit gate.

Revision ID: 0003_inventory_ledger
Revises: 0002_auth_tenants
Create Date: 2026-05-11 00:00:00.000000

Phases covered:
  Phase 1 — schema, grants, opening-balance guard
  Phase 2 — on_hand() SQL function
  Phase 3 — count-event trigger (mode-branching)
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_inventory_ledger"
down_revision: str | None = "0002_auth_tenants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:  # noqa: PLR0915
    # ── extensions ───────────────────────────────────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ═════════════════════════════════════════════════════════════════════════
    # CORE LOOKUP / REFERENCE TABLES
    # ═════════════════════════════════════════════════════════════════════════

    op.execute("""
        CREATE TABLE units_of_measure (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name         TEXT NOT NULL,
            abbreviation TEXT NOT NULL,
            unit_type    TEXT NOT NULL CHECK (unit_type IN ('weight','volume','count')),
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (tenant_id, name)
        )
    """)

    op.execute("""
        CREATE TABLE storage_zones (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name       TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (tenant_id, name)
        )
    """)

    op.execute("""
        CREATE TABLE ingredients_master (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name       TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (tenant_id, name)
        )
    """)

    # ═════════════════════════════════════════════════════════════════════════
    # INVENTORY ITEMS  (v5 §7.5)
    # ═════════════════════════════════════════════════════════════════════════

    op.execute("""
        CREATE TABLE inventory_items (
            id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id                UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            ingredient_id            UUID REFERENCES ingredients_master(id),
            name                     TEXT NOT NULL,
            inventory_mode           TEXT NOT NULL
                                         CHECK (inventory_mode IN ('recipe_deducted','count_anchored')),
            storage_unit_id          UUID NOT NULL REFERENCES units_of_measure(id),
            purchase_unit_id         UUID REFERENCES units_of_measure(id),
            recipe_unit_id           UUID REFERENCES units_of_measure(id),
            storage_zone_id          UUID REFERENCES storage_zones(id),
            par_level                NUMERIC,
            count_cadence_days       INTEGER,
            count_grace_days         INTEGER,
            last_count_at            TIMESTAMPTZ,
            last_count_quantity      NUMERIC,
            storage_to_recipe_factor NUMERIC NOT NULL DEFAULT 1.0,
            confidence_state         TEXT CHECK (confidence_state IN ('fresh','stale','expired')),
            active                   BOOLEAN NOT NULL DEFAULT TRUE,
            created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            -- v5 §12.4: Mode A items must have NULL cadence/grace.
            CONSTRAINT cadence_coherence CHECK (
                (inventory_mode = 'recipe_deducted'
                    AND count_cadence_days IS NULL
                    AND count_grace_days IS NULL)
                OR inventory_mode = 'count_anchored'
            )
        )
    """)

    op.execute("""
        CREATE INDEX idx_inventory_items_tenant
            ON inventory_items (tenant_id)
    """)

    # ═════════════════════════════════════════════════════════════════════════
    # INVENTORY MOVEMENTS  (append-only ledger)
    # ═════════════════════════════════════════════════════════════════════════

    op.execute("""
        CREATE TABLE inventory_movements (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id          UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            inventory_item_id  UUID NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE,
            movement_type      TEXT NOT NULL CHECK (movement_type IN (
                                   'opening_balance','receive','sale_depletion',
                                   'sale_signal','sale_signal_reversal',
                                   'count_adjust','waste',
                                   'transfer_in','transfer_out','adjustment'
                               )),
            delta              NUMERIC NOT NULL,
            source_type        TEXT,
            source_id          UUID,
            idempotency_key    TEXT,
            recorded_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            notes              TEXT,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (tenant_id, idempotency_key)
        )
    """)

    op.execute("""
        CREATE INDEX idx_inv_movements_item
            ON inventory_movements (tenant_id, inventory_item_id, recorded_at DESC)
    """)
    op.execute("""
        CREATE INDEX idx_inv_movements_type
            ON inventory_movements (tenant_id, inventory_item_id, movement_type)
    """)

    # ═════════════════════════════════════════════════════════════════════════
    # INVENTORY COUNT EVENTS
    # ═════════════════════════════════════════════════════════════════════════

    op.execute("""
        CREATE TABLE inventory_count_events (
            id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id                  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            inventory_item_id          UUID NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE,
            counted_quantity           NUMERIC NOT NULL,
            predicted_on_hand_at_count NUMERIC,
            counted_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            counted_by                 UUID REFERENCES users(id),
            notes                      TEXT,
            created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE INDEX idx_count_events_item
            ON inventory_count_events (tenant_id, inventory_item_id, counted_at DESC)
    """)

    # ═════════════════════════════════════════════════════════════════════════
    # INVENTORY YIELD FACTORS
    # ═════════════════════════════════════════════════════════════════════════

    op.execute("""
        CREATE TABLE inventory_yield_factors (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            inventory_item_id UUID NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE,
            yield_factor      NUMERIC NOT NULL DEFAULT 1.0 CHECK (yield_factor > 0),
            effective_from    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (tenant_id, inventory_item_id)
        )
    """)

    # ═════════════════════════════════════════════════════════════════════════
    # RECIPE STUBS  (minimal for sale-effect service; full schema is Sprint 5+)
    # ═════════════════════════════════════════════════════════════════════════

    op.execute("""
        CREATE TABLE recipe_versions (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name       TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE recipe_ingredients (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            recipe_version_id UUID NOT NULL REFERENCES recipe_versions(id) ON DELETE CASCADE,
            inventory_item_id UUID NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE,
            quantity          NUMERIC NOT NULL,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE sale_line_items (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            quantity          NUMERIC NOT NULL,
            recipe_version_id UUID REFERENCES recipe_versions(id),
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # ═════════════════════════════════════════════════════════════════════════
    # RECEIPTS
    # ═════════════════════════════════════════════════════════════════════════

    op.execute("""
        CREATE TABLE receipts (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            commit_state TEXT NOT NULL DEFAULT 'draft'
                             CHECK (commit_state IN ('draft','committed','cancelled')),
            received_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            committed_at TIMESTAMPTZ,
            notes        TEXT,
            created_by   UUID REFERENCES users(id),
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE receipt_lines (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            receipt_id            UUID NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
            inventory_item_id     UUID NOT NULL REFERENCES inventory_items(id),
            received_quantity     NUMERIC NOT NULL,
            purchase_unit_id      UUID REFERENCES units_of_measure(id),
            unit_cost_cents       INTEGER,
            variance_quantity     NUMERIC,
            variance_amount_cents INTEGER,
            emits_movement_id     UUID REFERENCES inventory_movements(id),
            idempotency_key       TEXT UNIQUE,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE ingredient_cost_snapshots (
            id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id              UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            inventory_item_id      UUID NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE,
            unit_cost_cents        INTEGER NOT NULL,
            purchase_unit_id       UUID REFERENCES units_of_measure(id),
            effective_from         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            source_receipt_line_id UUID REFERENCES receipt_lines(id),
            created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # ═════════════════════════════════════════════════════════════════════════
    # IDEMPOTENCY KEYS  (v5 §5.1)
    # ═════════════════════════════════════════════════════════════════════════

    op.execute("""
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            tenant_id           UUID NOT NULL,
            key                 TEXT NOT NULL,
            user_id             UUID REFERENCES users(id),
            request_fingerprint TEXT NOT NULL,
            response_status     INT,
            response_body       JSONB,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at          TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '24 hours',
            PRIMARY KEY (tenant_id, key)
        )
    """)

    # ═════════════════════════════════════════════════════════════════════════
    # MONITORING ALERTS  (v5 §14.3 verbatim)
    # ═════════════════════════════════════════════════════════════════════════

    op.execute("""
        CREATE TABLE IF NOT EXISTS monitoring_alerts (
            id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id            UUID REFERENCES tenants(id),
            monitor_name         TEXT NOT NULL,
            severity             TEXT NOT NULL CHECK (severity IN ('info','warn','critical')),
            trigger_payload      JSONB NOT NULL DEFAULT '{}',
            first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            alert_count          INT NOT NULL DEFAULT 1,
            resolved_at          TIMESTAMPTZ,
            resolved_by_admin_id UUID,
            resolved_note        TEXT
        )
    """)

    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS monitoring_alerts_active_dedup
            ON monitoring_alerts (tenant_id, monitor_name)
            WHERE resolved_at IS NULL
    """)

    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 1 — OPENING BALANCE GUARD
    # ═════════════════════════════════════════════════════════════════════════

    # Partial unique index: at most one opening_balance per (tenant, item).
    # This raises 23505 when a duplicate opening_balance is attempted directly.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS one_opening_balance_per_item
            ON inventory_movements (tenant_id, inventory_item_id)
            WHERE movement_type = 'opening_balance'
    """)

    # Trigger: raises 23514 when opening_balance is attempted AFTER other
    # (non-opening_balance) movements already exist.  The unique index above
    # handles the duplicate-opening_balance case (23505) separately.
    op.execute("""
        CREATE OR REPLACE FUNCTION fn_opening_balance_must_be_first()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.movement_type = 'opening_balance' AND EXISTS (
                SELECT 1 FROM inventory_movements
                 WHERE tenant_id          = NEW.tenant_id
                   AND inventory_item_id  = NEW.inventory_item_id
                   AND movement_type     != 'opening_balance'
            ) THEN
                RAISE EXCEPTION
                    'opening_balance must be the first movement for (%, %)',
                    NEW.tenant_id, NEW.inventory_item_id
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
    """)

    op.execute("""
        CREATE TRIGGER trg_opening_balance_must_be_first
            BEFORE INSERT ON inventory_movements
            FOR EACH ROW EXECUTE FUNCTION fn_opening_balance_must_be_first()
    """)

    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 1 — RLS + GRANTS
    # ═════════════════════════════════════════════════════════════════════════

    # Enable RLS on all tenant-scoped inventory tables.
    for tbl in (
        "units_of_measure",
        "storage_zones",
        "ingredients_master",
        "inventory_items",
        "inventory_movements",
        "inventory_count_events",
        "inventory_yield_factors",
        "recipe_versions",
        "recipe_ingredients",
        "sale_line_items",
        "receipts",
        "receipt_lines",
        "ingredient_cost_snapshots",
        "idempotency_keys",
        "monitoring_alerts",
    ):
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")

    # Permissive SELECT/INSERT policies (tenant isolation).
    for tbl in (
        "units_of_measure",
        "storage_zones",
        "ingredients_master",
        "inventory_items",
        "inventory_movements",
        "inventory_count_events",
        "inventory_yield_factors",
        "recipe_versions",
        "recipe_ingredients",
        "sale_line_items",
        "receipts",
        "receipt_lines",
        "ingredient_cost_snapshots",
    ):
        op.execute(f"""
            CREATE POLICY {tbl}_select ON {tbl} FOR SELECT
            USING (
                tenant_id::text = NULLIF(current_setting('app.tenant_id', true), '')
            )
        """)
        op.execute(f"""
            CREATE POLICY {tbl}_insert ON {tbl} FOR INSERT
            WITH CHECK (
                tenant_id::text = NULLIF(current_setting('app.tenant_id', true), '')
            )
        """)

    # UPDATE allowed on mutable tables (items, receipts) for app_user.
    for tbl in ("inventory_items", "receipts"):
        op.execute(f"""
            CREATE POLICY {tbl}_update ON {tbl} FOR UPDATE
            USING (
                tenant_id::text = NULLIF(current_setting('app.tenant_id', true), '')
            )
            WITH CHECK (
                tenant_id::text = NULLIF(current_setting('app.tenant_id', true), '')
            )
        """)

    # idempotency_keys: tenant_id stored as UUID (no text cast needed).
    op.execute("""
        CREATE POLICY idempotency_keys_select ON idempotency_keys FOR SELECT
        USING (
            tenant_id::text = NULLIF(current_setting('app.tenant_id', true), '')
        )
    """)
    op.execute("""
        CREATE POLICY idempotency_keys_insert ON idempotency_keys FOR INSERT
        WITH CHECK (
            tenant_id::text = NULLIF(current_setting('app.tenant_id', true), '')
        )
    """)
    op.execute("""
        CREATE POLICY idempotency_keys_update ON idempotency_keys FOR UPDATE
        USING (
            tenant_id::text = NULLIF(current_setting('app.tenant_id', true), '')
        )
    """)

    # monitoring_alerts: global alerts (tenant_id IS NULL) visible to admins;
    # tenant alerts visible to the owning tenant.
    op.execute("""
        CREATE POLICY monitoring_alerts_select ON monitoring_alerts FOR SELECT
        USING (
            tenant_id IS NULL
            OR tenant_id::text = NULLIF(current_setting('app.tenant_id', true), '')
        )
    """)
    op.execute("""
        CREATE POLICY monitoring_alerts_insert ON monitoring_alerts FOR INSERT
        WITH CHECK (true)
    """)
    op.execute("""
        CREATE POLICY monitoring_alerts_update ON monitoring_alerts FOR UPDATE
        USING (true)
    """)

    # Grant app_user access — intentionally NO UPDATE/DELETE on ledger tables.
    op.execute("""
        GRANT SELECT, INSERT
            ON inventory_movements,
               inventory_count_events,
               ingredient_cost_snapshots
            TO app_user
    """)
    op.execute("""
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON units_of_measure,
               storage_zones,
               ingredients_master,
               inventory_items,
               inventory_yield_factors,
               recipe_versions,
               recipe_ingredients,
               sale_line_items,
               receipts,
               receipt_lines,
               idempotency_keys,
               monitoring_alerts
            TO app_user
    """)
    # Explicit REVOKEs as belt-and-suspenders per spec.
    op.execute("REVOKE UPDATE, DELETE ON inventory_movements        FROM app_user")
    op.execute("REVOKE UPDATE, DELETE ON inventory_count_events     FROM app_user")
    op.execute("REVOKE UPDATE, DELETE ON ingredient_cost_snapshots  FROM app_user")

    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 2 — on_hand() FUNCTION
    # ═════════════════════════════════════════════════════════════════════════
    #
    # Design notes:
    #  • Mode A: SUM(delta) over all movements except sale_signal / reversal.
    #  • Mode B: anchor (last_count_quantity) + receipts/count_adjusts strictly
    #    AFTER last_count_at, minus signals (weighted by yield_factor).
    #    Using `>` (strict) so the opening_balance movement recorded AT the
    #    anchor timestamp is excluded and not double-counted.
    #  • COALESCE scalar subquery for yield_factor defaults to 1.0 when no row
    #    exists, avoiding NULL from an empty join.

    op.execute("""
        CREATE OR REPLACE FUNCTION on_hand(
            p_tenant_id          UUID,
            p_inventory_item_id  UUID
        ) RETURNS NUMERIC LANGUAGE SQL STABLE AS $$
          WITH item AS (
              SELECT inventory_mode, last_count_at, last_count_quantity
                FROM inventory_items
               WHERE tenant_id = p_tenant_id
                 AND id = p_inventory_item_id
          ),
          ledger_sum AS (
              SELECT COALESCE(SUM(delta), 0) AS qty
                FROM inventory_movements
               WHERE tenant_id          = p_tenant_id
                 AND inventory_item_id  = p_inventory_item_id
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
                 AND m.movement_type = 'sale_signal'
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
                              (SELECT yield_factor
                                 FROM inventory_yield_factors
                                WHERE tenant_id          = p_tenant_id
                                  AND inventory_item_id  = p_inventory_item_id),
                              1.0))
              ELSE NULL
          END
          FROM item, ledger_sum, receipts_since, signals_since
        $$
    """)

    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 3 — COUNT EVENT TRIGGER
    # ═════════════════════════════════════════════════════════════════════════
    #
    # Mode B: count re-anchors inventory_items (no ledger row needed because
    #   on_hand() walks forward from the anchor, not via ledger sum).
    # Mode A: compute drift vs. predicted; emit count_adjust if nonzero.
    #   SECURITY DEFINER so the trigger can INSERT into the append-only ledger
    #   even when called indirectly by app_user.

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
             WHERE id        = NEW.inventory_item_id
               AND tenant_id = NEW.tenant_id;

            -- Mode B: new count becomes the anchor; no ledger correction needed.
            IF v_mode = 'count_anchored' THEN
                UPDATE inventory_items
                   SET last_count_at       = NEW.counted_at,
                       last_count_quantity = NEW.counted_quantity
                 WHERE id        = NEW.inventory_item_id
                   AND tenant_id = NEW.tenant_id;
                RETURN NEW;
            END IF;

            -- Mode A: emit count_adjust if drift is non-trivial.
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
                NEW.tenant_id,
                NEW.inventory_item_id,
                'count_adjust',
                v_drift,
                'count_event',
                NEW.id,
                'count_adjust:' || NEW.id::text,
                NEW.counted_at,
                'Auto-emitted from inventory_count_events ' || NEW.id::text
            );

            RETURN NEW;
        END;
        $$
    """)

    op.execute("""
        DROP TRIGGER IF EXISTS trg_count_event_emits_adjust ON inventory_count_events
    """)
    op.execute("""
        CREATE TRIGGER trg_count_event_emits_adjust
            AFTER INSERT ON inventory_count_events
            FOR EACH ROW EXECUTE FUNCTION fn_count_event_emits_adjust()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_count_event_emits_adjust ON inventory_count_events")
    op.execute("DROP FUNCTION IF EXISTS fn_count_event_emits_adjust()")
    op.execute("DROP FUNCTION IF EXISTS on_hand(UUID, UUID)")
    op.execute("DROP TRIGGER IF EXISTS trg_opening_balance_must_be_first ON inventory_movements")
    op.execute("DROP FUNCTION IF EXISTS fn_opening_balance_must_be_first()")

    for tbl in (
        "ingredient_cost_snapshots",
        "receipt_lines",
        "receipts",
        "sale_line_items",
        "recipe_ingredients",
        "recipe_versions",
        "inventory_yield_factors",
        "inventory_count_events",
        "inventory_movements",
        "inventory_items",
        "ingredients_master",
        "storage_zones",
        "units_of_measure",
        "idempotency_keys",
        "monitoring_alerts",
    ):
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
