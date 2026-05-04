"""auth: tenants, users, user_tenants, invitations + RLS — Sprint 2 exit gate.

Revision ID: 0002_auth_tenants
Revises: 0001_baseline
Create Date: 2026-05-04 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_auth_tenants"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgcrypto provides gen_random_bytes() used by the invitations token default.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ── Tables ───────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE users (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workos_id     TEXT UNIQUE NOT NULL,
            email         TEXT UNIQUE NOT NULL,
            email_verified BOOLEAN NOT NULL DEFAULT FALSE,
            first_name    TEXT,
            last_name     TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE tenants (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug       TEXT UNIQUE NOT NULL,
            name       TEXT NOT NULL,
            plan       TEXT NOT NULL DEFAULT 'starter',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE user_tenants (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id    UUID NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
            tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            role       TEXT NOT NULL CHECK (role IN ('owner', 'manager', 'staff')),
            active     BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_user_tenants UNIQUE (user_id, tenant_id)
        )
    """)

    op.execute("""
        CREATE TABLE invitations (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            email       TEXT NOT NULL,
            role        TEXT NOT NULL CHECK (role IN ('owner', 'manager', 'staff')),
            token       TEXT UNIQUE NOT NULL
                            DEFAULT encode(gen_random_bytes(32), 'hex'),
            accepted_at TIMESTAMPTZ,
            expires_at  TIMESTAMPTZ NOT NULL
                            DEFAULT (NOW() + INTERVAL '7 days'),
            created_by  UUID REFERENCES users(id),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # ── RLS: enable + force on tenant-scoped tables ───────────────────────────
    for tbl in ("tenants", "user_tenants", "invitations"):
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")

    # ── tenants policies ──────────────────────────────────────────────────────
    op.execute("""
        CREATE POLICY tenant_select ON tenants FOR SELECT
        USING (
            id::text = NULLIF(current_setting('app.tenant_id', true), '')
        )
    """)
    op.execute("""
        CREATE POLICY tenant_insert ON tenants FOR INSERT
        WITH CHECK (
            current_setting('app.rls_mode', true) = 'register'
        )
    """)
    op.execute("""
        CREATE POLICY tenant_update ON tenants FOR UPDATE
        USING (
            id::text = NULLIF(current_setting('app.tenant_id', true), '')
        )
        WITH CHECK (
            id::text = NULLIF(current_setting('app.tenant_id', true), '')
        )
    """)

    # ── user_tenants policies ─────────────────────────────────────────────────
    op.execute("""
        CREATE POLICY user_tenant_select ON user_tenants FOR SELECT
        USING (
            user_id::text  = NULLIF(current_setting('app.user_id', true), '')
            OR
            tenant_id::text = NULLIF(current_setting('app.tenant_id', true), '')
        )
    """)
    op.execute("""
        CREATE POLICY user_tenant_insert ON user_tenants FOR INSERT
        WITH CHECK (
            current_setting('app.rls_mode', true) IN ('register', 'accept_invite')
        )
    """)
    op.execute("""
        CREATE POLICY user_tenant_update ON user_tenants FOR UPDATE
        USING (
            tenant_id::text = NULLIF(current_setting('app.tenant_id', true), '')
        )
        WITH CHECK (
            tenant_id::text = NULLIF(current_setting('app.tenant_id', true), '')
        )
    """)

    # ── invitations policies ──────────────────────────────────────────────────
    op.execute("""
        CREATE POLICY invitation_select ON invitations FOR SELECT
        USING (
            tenant_id::text = NULLIF(current_setting('app.tenant_id', true), '')
            OR (
                current_setting('app.rls_mode', true) = 'accept_invite'
                AND email = NULLIF(current_setting('app.invite_email', true), '')
            )
        )
    """)
    op.execute("""
        CREATE POLICY invitation_insert ON invitations FOR INSERT
        WITH CHECK (
            tenant_id::text = NULLIF(current_setting('app.tenant_id', true), '')
        )
    """)
    op.execute("""
        CREATE POLICY invitation_update ON invitations FOR UPDATE
        USING (
            current_setting('app.rls_mode', true) = 'accept_invite'
            AND email = NULLIF(current_setting('app.invite_email', true), '')
        )
        WITH CHECK (
            current_setting('app.rls_mode', true) = 'accept_invite'
            AND email = NULLIF(current_setting('app.invite_email', true), '')
        )
    """)

    # ── app_user role for RLS integration tests ───────────────────────────────
    # Superusers (e.g. reorderos in docker) bypass FORCE RLS.  Tests that need
    # to prove RLS blocks queries SET ROLE to this NOLOGIN role, which is always
    # subject to RLS policies.
    op.execute("""
        DO $$
        BEGIN
            CREATE ROLE app_user NOLOGIN;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END;
        $$
    """)
    op.execute(
        "GRANT USAGE ON SCHEMA public TO app_user"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON tenants, users, user_tenants, invitations TO app_user"
    )


def downgrade() -> None:
    # Drop policies first (they reference table names)
    for policy, tbl in [
        ("invitation_update", "invitations"),
        ("invitation_insert", "invitations"),
        ("invitation_select", "invitations"),
        ("user_tenant_update", "user_tenants"),
        ("user_tenant_insert", "user_tenants"),
        ("user_tenant_select", "user_tenants"),
        ("tenant_update", "tenants"),
        ("tenant_insert", "tenants"),
        ("tenant_select", "tenants"),
    ]:
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {tbl}")

    op.execute("DROP TABLE IF EXISTS invitations CASCADE")
    op.execute("DROP TABLE IF EXISTS user_tenants CASCADE")
    op.execute("DROP TABLE IF EXISTS tenants CASCADE")
    op.execute("DROP TABLE IF EXISTS users CASCADE")
    op.execute("DROP EXTENSION IF EXISTS pgcrypto")
