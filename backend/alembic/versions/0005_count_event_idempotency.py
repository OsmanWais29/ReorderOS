"""count event idempotency key + drop orphaned confidence_state column.

Revision ID: 0005_count_event_idempotency
Revises: 0004_cadence_coherence_v2
Create Date: 2026-05-16 00:00:00.000000

Changes
-------
1. Add idempotency_key (TEXT, nullable) to inventory_count_events.
   A partial unique index enforces uniqueness only for non-NULL keys,
   allowing pre-existing rows (which have no key) to coexist safely.
   New rows written by record_count_event() always carry a key.

2. Drop confidence_state from inventory_items.
   The column was never written or read — count_state is computed live
   in the router from last_count_at and count_cadence_days/grace_days.
   Dropping it removes the orphan before Sprint 4 code adds more tables
   that would need to ignore it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_count_event_idempotency"
down_revision: str | None = "0004_cadence_coherence_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add idempotency_key column — nullable so existing rows are unaffected.
    op.execute("""
        ALTER TABLE inventory_count_events
            ADD COLUMN idempotency_key TEXT
    """)

    # Partial unique index: enforces uniqueness for new rows that carry a key.
    # NULL keys (legacy rows) are excluded and do not conflict with each other.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_count_event_idempotency_key
            ON inventory_count_events (tenant_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
    """)

    # RLS: extend existing policies to cover the new column (no new policies
    # needed — the existing SELECT/INSERT policies on inventory_count_events
    # already cover all columns in the table).

    # 2. Drop the orphaned confidence_state column.
    op.execute("""
        ALTER TABLE inventory_items DROP COLUMN IF EXISTS confidence_state
    """)


def downgrade() -> None:
    # Restore confidence_state (nullable — cannot recover original values).
    op.execute("""
        ALTER TABLE inventory_items
            ADD COLUMN confidence_state TEXT
                CHECK (confidence_state IN ('fresh', 'stale', 'expired'))
    """)

    op.execute("DROP INDEX IF EXISTS uq_count_event_idempotency_key")

    op.execute("""
        ALTER TABLE inventory_count_events
            DROP COLUMN IF EXISTS idempotency_key
    """)
