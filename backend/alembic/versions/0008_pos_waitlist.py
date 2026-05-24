"""Sprint 4 — pos_waitlist table.

Stores interested restaurant owners who want to connect a POS system
not yet supported (or are waiting for beta access). No tenant_id — this
is pre-signup data. No RLS — the waitlist is public-facing.

Revision ID: 0008_pos_waitlist
Revises: 0007_service_worker_grants
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_pos_waitlist"
down_revision: str | None = "0007_service_worker_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE pos_waitlist (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            pos_name        text NOT NULL,
            email           text NOT NULL,
            restaurant_name text,
            created_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        GRANT SELECT, INSERT ON pos_waitlist TO app_user
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pos_waitlist")
