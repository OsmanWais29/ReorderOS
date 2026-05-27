"""Security hardening — fix 10 issues found in migrations 0002–0008 audit.

Changes, in priority order:

  1. REVOKE DELETE on users from app_user.
       users has no RLS policies; a DELETE grant with no policy means any
       app_user can delete any user record unconditionally.

  2. REVOKE DELETE on tenants from app_user.
       tenants has FORCE RLS with no DELETE policy, so the grant is already
       blocked in practice — revoke it for clarity.

  3. Fix monitoring_alerts INSERT/UPDATE policies.
       The original policies had no TO clause and WITH CHECK(true), so any
       app_user could insert or update alerts belonging to other tenants.
       Replace with role-specific policies:
         app_user  → T1-scoped INSERT + UPDATE (tenant isolation)
         service_worker → USING(true)/WITH CHECK(true) (trusted infra writes)

  4. Fix orders.state CHECK constraint — remove dead 'OPEN' value.
       The worker always normalises state to lowercase ('open', 'locked').
       The uppercase 'OPEN' value has never been written and never will be.
       Removing it prevents future accidental uppercase writes.

  5. Add FK: sale_line_items.recipe_version_id → recipe_versions.
       The column was created in 0006 as a bare UUID with no REFERENCES.
       Without the FK, writes with a non-existent recipe_version_id silently
       corrupt _emit_inventory_effects() downstream.

  6. Add partial unique index: invitations(tenant_id, email) WHERE accepted_at IS NULL.
       Without it, the same email can receive multiple pending invitations for
       the same tenant.

  7. Add CHECK: receipt_lines.received_quantity > 0.
       A negative received_quantity silently subtracts inventory on receipt
       commit, corrupting on_hand without any validation error.

  8. REVOKE DELETE on idempotency_keys from app_user.
       FORCE RLS + no DELETE policy already blocks the operation, but the
       dead grant is misleading.

  9. REVOKE SELECT on pos_waitlist from app_user.
       pos_waitlist has no RLS and no tenant_id. SELECT returns every email
       in the waitlist to any authenticated user. The table is write-only
       from the user's perspective.

 10. Add UNIQUE index on pos_waitlist(email).
       Without it the same email can be submitted to the waitlist repeatedly.

Revision ID: 0013_security_hardening
Revises: 0012_menu_item_recipe_link
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_security_hardening"
down_revision: str | None = "0012_menu_item_recipe_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_T1 = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_T1_TEXT = "tenant_id::text = NULLIF(current_setting('app.tenant_id', true), '')"


def upgrade() -> None:
    # ── 1. Revoke DELETE on users (no RLS — grant was unconditional) ──────────
    op.execute("REVOKE DELETE ON users FROM app_user")

    # ── 2. Revoke DELETE on tenants (FORCE RLS + no policy = dead grant) ──────
    op.execute("REVOKE DELETE ON tenants FROM app_user")

    # ── 3. Fix monitoring_alerts RLS policies ─────────────────────────────────
    #
    # Drop the two role-agnostic permissive policies (no TO clause) that
    # allowed app_user to write alerts for any tenant_id.
    op.execute("DROP POLICY IF EXISTS monitoring_alerts_insert ON monitoring_alerts")
    op.execute("DROP POLICY IF EXISTS monitoring_alerts_update ON monitoring_alerts")

    # app_user: tenant-scoped INSERT — cannot insert alerts for other tenants.
    op.execute(f"""
        CREATE POLICY monitoring_alerts_insert ON monitoring_alerts
            FOR INSERT TO app_user
            WITH CHECK ({_T1_TEXT})
    """)

    # service_worker: permissive INSERT — writes both tenant-scoped alerts
    # (dead-letter events) and system-level alerts (tenant_id IS NULL).
    op.execute("""
        CREATE POLICY monitoring_alerts_insert_sw ON monitoring_alerts
            FOR INSERT TO service_worker
            WITH CHECK (true)
    """)

    # app_user: tenant-scoped UPDATE — cannot modify other tenants' alerts.
    op.execute(f"""
        CREATE POLICY monitoring_alerts_update ON monitoring_alerts
            FOR UPDATE TO app_user
            USING ({_T1_TEXT})
            WITH CHECK ({_T1_TEXT})
    """)

    # service_worker: permissive UPDATE — UPSERTs resolve/escalate any alert.
    op.execute("""
        CREATE POLICY monitoring_alerts_update_sw ON monitoring_alerts
            FOR UPDATE TO service_worker
            USING (true)
            WITH CHECK (true)
    """)

    # ── 4. Fix orders.state CHECK — remove dead uppercase 'OPEN' value ────────
    #
    # The worker normalises state via .lower() before every write.
    # 'OPEN' has never been written; keeping it invites accidental divergence.
    op.execute("ALTER TABLE orders DROP CONSTRAINT orders_state_valid")
    op.execute("""
        ALTER TABLE orders
            ADD CONSTRAINT orders_state_valid
            CHECK (state IN ('open', 'locked'))
    """)

    # ── 5. Add FK: sale_line_items.recipe_version_id → recipe_versions ────────
    #
    # The column has been nullable since 0006; the FK only fires for non-NULL
    # values. Existing NULL rows are unaffected.
    op.execute("""
        ALTER TABLE sale_line_items
            ADD CONSTRAINT sli_recipe_fk
            FOREIGN KEY (recipe_version_id) REFERENCES recipe_versions(id)
    """)

    # ── 6. Partial unique: one pending invitation per (tenant, email) ──────────
    op.execute("""
        CREATE UNIQUE INDEX invitations_pending_unique
            ON invitations (tenant_id, email)
            WHERE accepted_at IS NULL
    """)

    # ── 7. Enforce positive received_quantity on receipt_lines ────────────────
    op.execute("""
        ALTER TABLE receipt_lines
            ADD CONSTRAINT receipt_lines_qty_positive
            CHECK (received_quantity > 0)
    """)

    # ── 8. Revoke dead DELETE grant on idempotency_keys ───────────────────────
    #
    # FORCE RLS + no DELETE policy already blocks the operation.
    op.execute("REVOKE DELETE ON idempotency_keys FROM app_user")

    # ── 9. Revoke SELECT on pos_waitlist (no RLS — exposes all email rows) ────
    op.execute("REVOKE SELECT ON pos_waitlist FROM app_user")

    # ── 10. Unique index on pos_waitlist.email ────────────────────────────────
    #
    # Deduplicate first: keep the earliest row per email, delete the rest.
    # On a fresh schema this is a no-op; on a dev/test DB it removes any
    # duplicate submissions that accumulated before this constraint existed.
    op.execute("""
        DELETE FROM pos_waitlist
        WHERE id NOT IN (
            SELECT DISTINCT ON (email) id
            FROM pos_waitlist
            ORDER BY email, created_at ASC
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX pos_waitlist_email_unique
            ON pos_waitlist (email)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS pos_waitlist_email_unique")
    op.execute("GRANT SELECT ON pos_waitlist TO app_user")
    op.execute("GRANT DELETE ON idempotency_keys TO app_user")

    op.execute("""
        ALTER TABLE receipt_lines
            DROP CONSTRAINT IF EXISTS receipt_lines_qty_positive
    """)

    op.execute("DROP INDEX IF EXISTS invitations_pending_unique")

    op.execute("""
        ALTER TABLE sale_line_items
            DROP CONSTRAINT IF EXISTS sli_recipe_fk
    """)

    op.execute("ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_state_valid")
    op.execute("""
        ALTER TABLE orders
            ADD CONSTRAINT orders_state_valid
            CHECK (state IN ('open', 'OPEN', 'locked'))
    """)

    op.execute("DROP POLICY IF EXISTS monitoring_alerts_update_sw ON monitoring_alerts")
    op.execute("DROP POLICY IF EXISTS monitoring_alerts_update ON monitoring_alerts")
    op.execute("DROP POLICY IF EXISTS monitoring_alerts_insert_sw ON monitoring_alerts")
    op.execute("DROP POLICY IF EXISTS monitoring_alerts_insert ON monitoring_alerts")

    op.execute("""
        CREATE POLICY monitoring_alerts_insert ON monitoring_alerts FOR INSERT
        WITH CHECK (true)
    """)
    op.execute("""
        CREATE POLICY monitoring_alerts_update ON monitoring_alerts FOR UPDATE
        USING (true)
        WITH CHECK (true)
    """)

    op.execute("GRANT DELETE ON tenants TO app_user")
    op.execute("GRANT DELETE ON users TO app_user")
