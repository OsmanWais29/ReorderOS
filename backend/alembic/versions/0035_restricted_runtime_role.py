"""Restricted runtime role prerequisites (security PR).

Enables running the API request path as a non-BYPASSRLS role (instead of doadmin),
so RLS becomes the enforced tenant-isolation control rather than defense-in-depth.

Changes (all idempotent, additive, reversible):

  1. tenant_select policy → MEMBERSHIP-AWARE, scoped TO app_user.
     Old policy allowed only `id = app.tenant_id`, so /auth/me and GET /tenants
     (which list a user's memberships with no single active tenant) returned zero
     rows under a restricted role. New policy also allows a tenant the caller has
     an ACTIVE membership in (keyed on app.user_id). BOTH arms reference the row or
     the caller's own membership — there is NO `rls_mode IN (...)` carve-out (which
     would be unconditionally true and expose the whole table while a bootstrap mode
     is live). The strict single-tenant read path is unchanged.

  1b. user_tenant_select is left row-scoped and byte-identical to pre-0035. The
     bootstrap INSERT ... RETURNING paths bind app.tenant_id / app.user_id precisely
     in application code (tenants.repo.register_tenant, invitations.repo.
     accept_invitation) instead of relying on a mode carve-out.

  2. GRANT SELECT ON alembic_version TO app_user.
     The API startup schema-head check reads alembic_version on the request path;
     app_user had no grant, so startup 500s once DATABASE_URL stops using doadmin.

  NOTE: the login role reorderos_app (NOLOGIN, NOSUPERUSER, NOBYPASSRLS, INHERIT,
  member of app_user) is provisioned OUT OF BAND (staging runbook), never in a
  migration — a downgrade must not be able to drop an infrastructure identity.
  Test fixtures create/drop it locally.

Risk (Migration Risk Standard §1.2):
  - Data validity: Low — one membership-scoped policy arm + one SELECT grant; no
    data change; no unconditional visibility.
  - Availability:  Low — policy swap + grant; no table rewrite/lock, no role create.
  - Compatibility: Low — the strict id=app.tenant_id read path is preserved.
  - Reversibility: Full — restore old policy, revoke grant; touches no runtime role.

Revision ID: 0035_restricted_runtime_role
Revises: 0034_stock_insights_support
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0035_restricted_runtime_role"
down_revision: str | None = "0034_stock_insights_support"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. membership-aware tenant SELECT policy, scoped TO app_user (its members —
    #    e.g. reorderos_app — inherit it). Other roles (service_worker) have no
    #    SELECT grant on tenants, so this does not affect them.
    #
    #    Every arm references the ROW (id) or the caller's OWN membership — there is
    #    deliberately NO `rls_mode IN (...)` arm, which would evaluate true
    #    unconditionally (ignoring the row) and expose every tenant while a bootstrap
    #    mode is live. The bootstrap INSERT ... RETURNING paths instead bind
    #    app.tenant_id / app.user_id precisely (see tenants.repo.register_tenant and
    #    invitations.repo.accept_invitation) so the narrow arms below cover the
    #    RETURNING. This keeps RLS row-scoped at all times — the whole point of the PR.
    op.execute("DROP POLICY IF EXISTS tenant_select ON tenants")
    op.execute("""
        CREATE POLICY tenant_select ON tenants FOR SELECT TO app_user
        USING (
            id::text = NULLIF(current_setting('app.tenant_id', true), '')
            OR EXISTS (
                SELECT 1 FROM user_tenants ut
                WHERE ut.tenant_id = tenants.id
                  AND ut.user_id::text = NULLIF(current_setting('app.user_id', true), '')
                  AND ut.active
            )
        )
    """)

    # 1b. user_tenants SELECT stays row-scoped (own user_id OR own tenant_id). No
    #     rls_mode carve-out: accept-invite binds app.user_id to the invitee before
    #     its INSERT ... RETURNING, so `user_id = app.user_id` covers the read;
    #     register binds app.user_id to the owner. This policy is byte-identical to
    #     the pre-0035 (0002) policy — recreated here only for explicitness; the
    #     app-code fix, not a policy widening, is what makes bootstrap work.
    op.execute("DROP POLICY IF EXISTS user_tenant_select ON user_tenants")
    op.execute("""
        CREATE POLICY user_tenant_select ON user_tenants FOR SELECT
        USING (
            user_id::text  = NULLIF(current_setting('app.user_id', true), '')
            OR tenant_id::text = NULLIF(current_setting('app.tenant_id', true), '')
        )
    """)

    # 2. API startup schema-head check reads alembic_version on the request path
    op.execute("GRANT SELECT ON alembic_version TO app_user")

    # NOTE: the LOGIN role (reorderos_app) is deliberately NOT managed here. Login
    # roles with rotated passwords are provisioned out of band (staging runbook);
    # putting them in Alembic would let a downgrade drop an infrastructure identity.
    # Test fixtures create/drop reorderos_app locally.


def downgrade() -> None:
    op.execute("REVOKE SELECT ON alembic_version FROM app_user")
    # restore pre-0035 user_tenant_select (0002: no bootstrap carve-out)
    op.execute("DROP POLICY IF EXISTS user_tenant_select ON user_tenants")
    op.execute("""
        CREATE POLICY user_tenant_select ON user_tenants FOR SELECT
        USING (
            user_id::text  = NULLIF(current_setting('app.user_id', true), '')
            OR tenant_id::text = NULLIF(current_setting('app.tenant_id', true), '')
        )
    """)
    op.execute("DROP POLICY IF EXISTS tenant_select ON tenants")
    # restore the exact pre-0035 policy (0002: unscoped / public, single-tenant only)
    op.execute("""
        CREATE POLICY tenant_select ON tenants FOR SELECT
        USING (
            id::text = NULLIF(current_setting('app.tenant_id', true), '')
        )
    """)
