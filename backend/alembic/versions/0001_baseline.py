"""baseline (empty) — Sprint 1 exit gate.

Sprint 2 will add tenants/users/RLS in 0002.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-03 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op. Proves Alembic is wired and the DB is reachable."""


def downgrade() -> None:
    """No-op."""
