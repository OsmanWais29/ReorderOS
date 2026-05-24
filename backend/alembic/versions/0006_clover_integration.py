"""Sprint 4 — Clover Integration MVP.

Revision ID: 0006_clover_integration
Revises: 0005_count_event_idempotency
Create Date: 2026-05-17 00:00:00.000000

Phases:
  Phase 0 — service_worker role (IaC embedded in migration for CI parity)
  Phase 1 — tenant_pos_connections
  Phase 2 — pos_event_inbox
  Phase 3 — menu_items stub + orders + sale_line_items (replaces Sprint 3 stub)
  Phase 4 — lookup function + oauth_states + service role grants
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_clover_integration"
down_revision: str | None = "0005_count_event_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:  # noqa: PLR0915
    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 0 — service_worker role
    #
    # LOGIN is required: the worker process connects via SERVICE_DATABASE_URL
    # as service_worker. NOLOGIN would make every worker connection fail with
    # "role is not permitted to log in" — a silent killer that lets the
    # migration succeed and all grants succeed but the worker can never run.
    #
    # Local dev password: 'service_worker' (matches SERVICE_DATABASE_URL
    # in .env). On DO App Platform, override via env-var provisioning.
    # ═════════════════════════════════════════════════════════════════════════

    op.execute("""
        DO $$
        BEGIN
            CREATE ROLE service_worker LOGIN PASSWORD 'service_worker';
        EXCEPTION WHEN duplicate_object THEN NULL;
        END;
        $$
    """)
    op.execute("GRANT CONNECT ON DATABASE reorderos TO service_worker")
    op.execute("GRANT USAGE ON SCHEMA public TO service_worker")

    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 1 — tenant_pos_connections
    #
    # One row per merchant connection. Stores encrypted OAuth tokens and
    # tracks connection state. Dual RLS:
    #   app_user   → tenant-scoped (T1), owns INSERT during OAuth callback
    #   service_worker → USING(true) for cross-tenant token refresh and
    #                    reconciliation cursor updates
    # ═════════════════════════════════════════════════════════════════════════

    op.execute("""
        CREATE TABLE tenant_pos_connections (
            connection_id               uuid PRIMARY KEY,
            tenant_id                   uuid NOT NULL REFERENCES tenants(id),
            vendor                      text NOT NULL,
            merchant_id                 text NOT NULL,
            environment                 text NOT NULL,

            access_token_enc            text NOT NULL,
            access_token_expires_at     timestamptz NOT NULL,
            refresh_token_enc           text NOT NULL,
            refresh_token_expires_at    timestamptz NOT NULL,
            prev_refresh_token_enc      text,
            last_token_refresh_at       timestamptz,

            configured_permissions      text,

            webhook_secret_enc          text,
            last_webhook_at             timestamptz,
            last_reconciliation_at      timestamptz,
            last_reconciliation_cursor  bigint,

            state                       text NOT NULL DEFAULT 'pending',
            state_reason                text,
            refresh_failure_count       int NOT NULL DEFAULT 0,
            initial_sync_started_at     timestamptz,
            initial_sync_completed_at   timestamptz,
            revoked_at                  timestamptz,

            connected_by_user_id        uuid REFERENCES users(id),
            created_at                  timestamptz NOT NULL DEFAULT now(),
            updated_at                  timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT tenant_pos_vendor_valid
                CHECK (vendor IN ('clover')),
            CONSTRAINT tenant_pos_environment_valid
                CHECK (environment IN ('sandbox', 'production', 'eu', 'latam')),
            CONSTRAINT tenant_pos_state_valid
                CHECK (state IN ('pending', 'active', 'revoked', 'error')),
            CONSTRAINT tenant_pos_token_expiry_order
                CHECK (refresh_token_expires_at >= access_token_expires_at)
        )
    """)

    # Partial unique: only one ACTIVE connection per (tenant, vendor).
    # Allows reconnect after revoked — revoked rows don't enter this index.
    op.execute("""
        CREATE UNIQUE INDEX tenant_pos_one_active_per_tenant_vendor
            ON tenant_pos_connections (tenant_id, vendor)
            WHERE state = 'active'
    """)

    # Partial unique: a merchant can only be claimed by one tenant at a time.
    # revoked rows are excluded so a revoked merchant can be re-claimed.
    op.execute("""
        CREATE UNIQUE INDEX tenant_pos_one_tenant_per_merchant
            ON tenant_pos_connections (vendor, merchant_id)
            WHERE state IN ('pending', 'active', 'error')
    """)

    op.execute("""
        CREATE INDEX tenant_pos_tenant_state
            ON tenant_pos_connections (tenant_id, state, updated_at DESC)
    """)

    # Token refresh job scans this index to find expiring connections.
    op.execute("""
        CREATE INDEX tenant_pos_access_expiring
            ON tenant_pos_connections (access_token_expires_at)
            WHERE state = 'active'
    """)

    # RLS — ENABLE only (no FORCE: reorderos is superuser and bypasses anyway)
    op.execute("ALTER TABLE tenant_pos_connections ENABLE ROW LEVEL SECURITY")

    # app_user: tenant-scoped T1 policy.
    # NULLIF(..., '') converts empty string → NULL before the uuid cast.
    # This handles two distinct cases:
    #   - Unset session var: current_setting(..., true) returns NULL → NULLIF
    #     returns NULL → NULL::uuid → RLS blocks all rows safely.
    #   - Explicitly cleared to '': clear_rls_context() writes '' (not NULL).
    #     Without NULLIF, ''::uuid raises "invalid input syntax for type uuid".
    #     With NULLIF, '' → NULL → same safe path as unset.
    op.execute("""
        CREATE POLICY tpc_tenant_isolation ON tenant_pos_connections
            FOR ALL TO app_user
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

    # service_worker: USING(true) — full cross-tenant read/write needed for
    # token refresh (scans all active connections) and reconciliation (updates
    # cursor across all tenants). No INSERT privilege granted at table level.
    op.execute("""
        CREATE POLICY tpc_service_access ON tenant_pos_connections
            FOR ALL TO service_worker
            USING (true)
            WITH CHECK (true)
    """)

    # Grants — app_user can INSERT (OAuth callback) + SELECT + UPDATE.
    # service_worker can only SELECT + UPDATE (token refresh, cursor, state).
    op.execute("""
        GRANT SELECT, INSERT, UPDATE ON tenant_pos_connections TO app_user
    """)
    op.execute("""
        GRANT SELECT, UPDATE ON tenant_pos_connections TO service_worker
    """)


    # ══��══════════════════════════════════════════════════════════════════════
    # PHASE 2 — pos_event_inbox
    #
    # Append-only event store for Clover webhooks and reconciliation pulls.
    # The webhook handler INSERTs here and returns 200 immediately.
    # The inbox worker then claims and processes rows asynchronously.
    #
    # Key design decisions:
    #   - 5-column unique (tenant_id, vendor, vendor_event_id, vendor_event_type,
    #     vendor_ts) allows multiple UPDATEs for the same order with different
    #     timestamps. A 4-column key would collapse them into one row.
    #   - vendor_ts is bigint (Clover milliseconds epoch), NOT a timestamptz,
    #     so no timezone conversion is needed and the value round-trips exactly.
    #   - app_user gets SELECT only — webhook inserts run as service_worker.
    #   - service_worker gets SELECT, INSERT, UPDATE. No DELETE — inbox is
    #     append-only except for worker state transitions (UPDATE).
    # ═════════════════════════════════════════════════════════════════════════

    op.execute("""
        CREATE TABLE pos_event_inbox (
            inbox_id                uuid PRIMARY KEY,
            tenant_id               uuid NOT NULL REFERENCES tenants(id),
            connection_id           uuid REFERENCES tenant_pos_connections(connection_id),

            vendor                  text NOT NULL,
            vendor_event_id         text NOT NULL,
            vendor_object_type      text NOT NULL,
            vendor_event_type       text NOT NULL,
            vendor_ts               bigint NOT NULL,

            raw_payload             jsonb NOT NULL,
            signature_verified      boolean NOT NULL DEFAULT false,

            state                   text NOT NULL DEFAULT 'pending',
            retry_count             integer NOT NULL DEFAULT 0,
            last_error              text,
            next_attempt_at         timestamptz,
            claim_expires_at        timestamptz,

            received_at             timestamptz NOT NULL DEFAULT now(),
            processing_started_at   timestamptz,
            processed_at            timestamptz,

            fetched_payload         jsonb,
            fetched_at              timestamptz,

            source                  text NOT NULL DEFAULT 'webhook',

            CONSTRAINT pos_inbox_vendor_valid
                CHECK (vendor IN ('clover')),
            CONSTRAINT pos_inbox_object_type_valid
                CHECK (vendor_object_type IN ('O','P','I','C','M','E','A')),
            CONSTRAINT pos_inbox_event_type_valid
                CHECK (vendor_event_type IN ('CREATE','UPDATE','DELETE')),
            CONSTRAINT pos_inbox_state_valid
                CHECK (state IN ('pending','processing','processed','failed',
                                 'dead_letter','duplicate_ignored')),
            CONSTRAINT pos_inbox_source_valid
                CHECK (source IN ('webhook','reconciliation','manual')),
            CONSTRAINT pos_inbox_retry_nonneg
                CHECK (retry_count >= 0),
            CONSTRAINT pos_inbox_processed_consistency
                CHECK ((state = 'processed' AND processed_at IS NOT NULL)
                       OR state <> 'processed')
        )
    """)

    # 5-column unique: allows multiple UPDATEs for same order (different ts).
    # Clover sends UPDATE when items are added (ts=1000), payment attached
    # (ts=2000), order locked (ts=3000), refunded (ts=4000). Each must be
    # stored. A 4-column key would reject the 2nd UPDATE as a duplicate.
    op.execute("""
        ALTER TABLE pos_event_inbox
            ADD CONSTRAINT pos_inbox_dedup_unique
            UNIQUE (tenant_id, vendor, vendor_event_id, vendor_event_type, vendor_ts)
    """)

    # Worker pickup: finds pending/failed events that are ready to process.
    op.execute("""
        CREATE INDEX idx_inbox_worker_pickup
            ON pos_event_inbox (tenant_id, next_attempt_at)
            WHERE state IN ('pending', 'failed')
    """)

    # Stale claim recovery: finds events claimed but not processed within TTL.
    op.execute("""
        CREATE INDEX idx_inbox_stale_claims
            ON pos_event_inbox (claim_expires_at)
            WHERE state = 'processing'
    """)

    op.execute("""
        CREATE INDEX idx_inbox_dead_letter
            ON pos_event_inbox (tenant_id, received_at DESC)
            WHERE state = 'dead_letter'
    """)

    # Order event lookup: reconciliation checks if an order's events exist.
    op.execute("""
        CREATE INDEX idx_inbox_order_events
            ON pos_event_inbox (tenant_id, vendor, vendor_event_id)
    """)

    op.execute("ALTER TABLE pos_event_inbox ENABLE ROW LEVEL SECURITY")

    # app_user: T1 isolation with NULLIF guard (same pattern as Phase 1).
    op.execute("""
        CREATE POLICY inbox_tenant_isolation ON pos_event_inbox
            FOR ALL TO app_user
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

    # service_worker: USING(true) — webhook handler inserts before tenant
    # context is resolved; worker claims across all tenants.
    op.execute("""
        CREATE POLICY inbox_service_access ON pos_event_inbox
            FOR ALL TO service_worker
            USING (true)
            WITH CHECK (true)
    """)

    # app_user: SELECT only — inbox writes happen via service_worker.
    op.execute("GRANT SELECT ON pos_event_inbox TO app_user")
    # service_worker: INSERT (webhook), UPDATE (worker state transitions).
    # No DELETE — inbox rows are never removed.
    op.execute("GRANT SELECT, INSERT, UPDATE ON pos_event_inbox TO service_worker")

    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 3 — menu_items stub + orders + sale_line_items
    #
    # Replaces the Sprint 3 sale_line_items stub (4 columns) with the full
    # Sprint 4 schema (20 columns, FK to orders, Clover identifiers).
    #
    # Critical design: BOTH app_user AND service_worker use T1 (tenant-scoped)
    # RLS on orders and sale_line_items. This means service_worker MUST call
    # SET LOCAL app.tenant_id before any write. This is the explicit guard
    # in the fail gates: "service_worker has USING(true) on orders or
    # sale_line_items" → FAIL.
    # ═════════════════════════════════════════════════════════════════════════

    # Drop the Sprint 3 sale_line_items stub. CASCADE drops its RLS policies
    # and grants. The Sprint 4 version is created below with a superset schema.
    op.execute("DROP TABLE IF EXISTS sale_line_items CASCADE")

    # Minimal menu_items stub — provides the FK target for sale_line_items.
    # Full schema (pos_item_id sync, category, price) is Sprint 5.
    op.execute("""
        CREATE TABLE menu_items (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   uuid NOT NULL REFERENCES tenants(id),
            pos_item_id text,
            name        text NOT NULL,
            active      boolean NOT NULL DEFAULT true,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("ALTER TABLE menu_items ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY menu_items_tenant ON menu_items
            FOR ALL TO app_user
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE ON menu_items TO app_user")

    # orders — one row per normalized Clover order (created when state='locked').
    # NOT named 'sales' — the spec's fail gate explicitly catches that mistake.
    op.execute("""
        CREATE TABLE orders (
            id                      uuid PRIMARY KEY,
            tenant_id               uuid NOT NULL REFERENCES tenants(id),
            pos_event_inbox_id      uuid NOT NULL REFERENCES pos_event_inbox(inbox_id),

            clover_order_id         text NOT NULL,
            external_reference_id   text,

            channel_map_id          uuid,
            channel_label           text NOT NULL DEFAULT 'unknown',
            channel_category        text NOT NULL DEFAULT 'unknown',
            order_type_label        text,

            device_id               text,
            payment_app_id          text,
            clover_employee_id      text,

            subtotal_cents          int NOT NULL DEFAULT 0,
            discount_amount_cents   int NOT NULL DEFAULT 0,
            tax_amount_cents        int NOT NULL DEFAULT 0,
            tip_amount_cents        int NOT NULL DEFAULT 0,
            total_amount_cents      int NOT NULL DEFAULT 0,

            state                   text NOT NULL DEFAULT 'locked',
            payment_state           text NOT NULL DEFAULT 'OPEN',
            payment_state_previous  text,

            covers                  int,
            opened_at               timestamptz,
            closed_at               timestamptz,
            processed_at            timestamptz NOT NULL DEFAULT now(),

            motion_zone_id          uuid,
            predicted_covers        int,
            is_peak_period          boolean,

            CONSTRAINT orders_state_valid
                CHECK (state IN ('open', 'OPEN', 'locked')),
            CONSTRAINT orders_payment_state_valid
                CHECK (payment_state IN
                       ('OPEN','PAID','PARTIALLY_REFUNDED','REFUNDED','CREDITED')),
            CONSTRAINT orders_clock_order
                CHECK (processed_at >= closed_at OR closed_at IS NULL),
            CONSTRAINT uq_orders_clover
                UNIQUE (tenant_id, clover_order_id)
        )
    """)

    op.execute("""
        CREATE INDEX idx_orders_tenant_closed
            ON orders (tenant_id, closed_at DESC)
    """)
    op.execute("""
        CREATE INDEX idx_orders_tenant_channel
            ON orders (tenant_id, channel_label, closed_at DESC)
    """)
    op.execute("""
        CREATE INDEX idx_orders_tenant_state
            ON orders (tenant_id, state, closed_at DESC)
    """)
    op.execute("""
        CREATE INDEX idx_orders_tenant_channel_cat
            ON orders (tenant_id, channel_category, closed_at DESC)
    """)
    op.execute("""
        CREATE INDEX idx_orders_third_party
            ON orders (tenant_id, closed_at DESC)
            WHERE channel_category = '3rd_party_delivery'
    """)
    op.execute("""
        CREATE INDEX idx_orders_inbox_id ON orders (pos_event_inbox_id)
    """)
    op.execute("""
        CREATE INDEX idx_orders_clover_employee
            ON orders (tenant_id, clover_employee_id)
    """)
    op.execute("""
        CREATE INDEX idx_orders_payment_state_open
            ON orders (tenant_id, payment_state) WHERE payment_state = 'OPEN'
    """)

    op.execute("ALTER TABLE orders ENABLE ROW LEVEL SECURITY")

    # T1 for BOTH app_user and service_worker — worker must SET LOCAL before
    # writing. NULLIF handles the clear_rls_context() empty-string case.
    op.execute("""
        CREATE POLICY orders_tenant_isolation ON orders
            FOR ALL TO app_user, service_worker
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

    op.execute("GRANT SELECT ON orders TO app_user")
    # service_worker: INSERT + UPDATE (ON CONFLICT DO NOTHING + post-lock updates).
    op.execute("GRANT SELECT, INSERT, UPDATE ON orders TO service_worker")

    # sale_line_items — Sprint 4 full schema, replaces Sprint 3 stub.
    # FK to orders.id (not nullable — every line item belongs to an order).
    # recipe_version_id nullable — populated in Sprint 5 by item-mapping step.
    op.execute("""
        CREATE TABLE sale_line_items (
            id                      uuid PRIMARY KEY,
            tenant_id               uuid NOT NULL REFERENCES tenants(id),
            order_id                uuid NOT NULL REFERENCES orders(id),

            clover_line_item_id     text NOT NULL,
            menu_item_id            uuid REFERENCES menu_items(id),
            recipe_version_id       uuid,

            name_at_sale            text NOT NULL,
            quantity                numeric NOT NULL DEFAULT 1,

            price_cents_at_sale     int NOT NULL,
            discount_amount_cents   int NOT NULL DEFAULT 0,
            net_revenue_cents       int NOT NULL,
            cost_cents_at_sale      int,

            is_refunded             boolean NOT NULL DEFAULT false,
            refunded_quantity       int NOT NULL DEFAULT 0,
            refunded_amount_cents   int NOT NULL DEFAULT 0,
            is_voided               boolean NOT NULL DEFAULT false,

            created_at              timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT uq_sli_clover
                UNIQUE (tenant_id, clover_line_item_id),
            CONSTRAINT sli_refund_nonneg
                CHECK (refunded_quantity >= 0),
            CONSTRAINT sli_refund_max
                CHECK (refunded_quantity <= quantity)
        )
    """)

    op.execute("""
        CREATE INDEX idx_sli_order
            ON sale_line_items (tenant_id, order_id)
    """)
    op.execute("""
        CREATE INDEX idx_sli_menu_item
            ON sale_line_items (tenant_id, menu_item_id, created_at DESC)
    """)
    # Partial index for active (non-refunded, non-voided) sales — used by
    # recipe walk and sales analytics to skip cancelled line items.
    op.execute("""
        CREATE INDEX idx_sli_active_sales
            ON sale_line_items (tenant_id, menu_item_id, created_at DESC)
            WHERE NOT is_refunded AND NOT is_voided
    """)
    op.execute("""
        CREATE INDEX idx_sli_recipe_version
            ON sale_line_items (recipe_version_id)
    """)

    op.execute("ALTER TABLE sale_line_items ENABLE ROW LEVEL SECURITY")

    # T1 for BOTH roles — same policy name pattern as orders.
    op.execute("""
        CREATE POLICY sli_tenant_isolation ON sale_line_items
            FOR ALL TO app_user, service_worker
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    """)

    op.execute("GRANT SELECT ON sale_line_items TO app_user")
    # service_worker: INSERT only in Sprint 4. No UPDATE — line items are
    # immutable once written (Sprint 6 adds refund logic).
    op.execute("GRANT SELECT, INSERT ON sale_line_items TO service_worker")

    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 4 — Service Role Grants + Lookup Function + oauth_states
    #
    # lookup_tenant_by_merchant: the webhook handler receives a Clover webhook
    # with only merchant_id in the header — no tenant context. This function
    # resolves merchant_id → tenant_id by querying tenant_pos_connections.
    # SECURITY DEFINER lets it run as the function owner (superuser) so it
    # bypasses RLS without requiring SET LOCAL app.tenant_id. STABLE tells
    # the planner it won't modify the DB and returns the same result within
    # a transaction. search_path=public guards against schema injection.
    #
    # oauth_states: short-lived CSRF tokens for the OAuth 2.0 flow.
    # No RLS — the state token (primary key, random UUID) is the security
    # mechanism. app_user creates on /initiate; DELETE on /callback.
    # Expired rows cleaned by the token-refresh job every 15 minutes.
    # ═════════════════════════════════════════════════════════════════════════

    op.execute("""
        CREATE OR REPLACE FUNCTION lookup_tenant_by_merchant(
            p_vendor text, p_merchant_id text
        ) RETURNS uuid AS $$
            SELECT tenant_id FROM tenant_pos_connections
            WHERE vendor = p_vendor AND merchant_id = p_merchant_id
              AND state IN ('active', 'error')
            LIMIT 1;
        $$ LANGUAGE sql SECURITY DEFINER STABLE SET search_path = public;
    """)

    # Default PostgreSQL grants EXECUTE to PUBLIC for new functions.
    # Explicitly revoke then grant only to the two roles that need it.
    op.execute("REVOKE ALL ON FUNCTION lookup_tenant_by_merchant(text, text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION lookup_tenant_by_merchant(text, text) TO service_worker")
    op.execute("GRANT EXECUTE ON FUNCTION lookup_tenant_by_merchant(text, text) TO app_user")

    op.execute("""
        CREATE TABLE oauth_states (
            state       text PRIMARY KEY,
            tenant_id   uuid NOT NULL,
            user_id     uuid NOT NULL,
            expires_at  timestamptz NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
    """)

    # No RLS on oauth_states — state token is the security mechanism.
    # Both roles need SELECT (validate), INSERT (initiate), DELETE (consume).
    op.execute("GRANT SELECT, INSERT, DELETE ON oauth_states TO app_user")
    op.execute("GRANT SELECT, INSERT, DELETE ON oauth_states TO service_worker")


def downgrade() -> None:
    # Phase 4 — drop before Phase 3 (function depends on tenant_pos_connections)
    op.execute("DROP TABLE IF EXISTS oauth_states")
    op.execute("DROP FUNCTION IF EXISTS lookup_tenant_by_merchant(text, text)")

    # Phase 3 — drop in reverse dependency order, restore Sprint 3 stub
    op.execute("DROP TABLE IF EXISTS sale_line_items CASCADE")
    op.execute("DROP TABLE IF EXISTS orders CASCADE")
    op.execute("DROP TABLE IF EXISTS menu_items CASCADE")

    # Restore Sprint 3 sale_line_items stub so 0003_inventory_ledger tests pass
    op.execute("""
        CREATE TABLE sale_line_items (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            quantity          NUMERIC NOT NULL,
            recipe_version_id UUID REFERENCES recipe_versions(id),
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("ALTER TABLE sale_line_items ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sale_line_items FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY sale_line_items_select ON sale_line_items FOR SELECT
        USING (tenant_id::text = NULLIF(current_setting('app.tenant_id', true), ''))
    """)
    op.execute("""
        CREATE POLICY sale_line_items_insert ON sale_line_items FOR INSERT
        WITH CHECK (tenant_id::text = NULLIF(current_setting('app.tenant_id', true), ''))
    """)
    op.execute("""
        GRANT SELECT, INSERT, UPDATE, DELETE ON sale_line_items TO app_user
    """)

    # Phase 2
    op.execute("DROP TABLE IF EXISTS pos_event_inbox CASCADE")
    # Phase 1
    op.execute("DROP TABLE IF EXISTS tenant_pos_connections CASCADE")
    # Phase 0
    op.execute("REVOKE USAGE ON SCHEMA public FROM service_worker")
    op.execute("REVOKE CONNECT ON DATABASE reorderos FROM service_worker")
    op.execute("DROP ROLE IF EXISTS service_worker")
