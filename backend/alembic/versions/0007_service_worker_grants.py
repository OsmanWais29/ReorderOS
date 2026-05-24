"""Sprint 4 — service_worker grants on monitoring_alerts.

service_worker._dead_letter() INSERTs/UPDATEs monitoring_alerts when an
inbox event exhausts its retries. The table was created in 0003 (Sprint 3)
before the service_worker role existed, so no grant was included there.

Revision ID: 0007_service_worker_grants
Revises: 0006_clover_integration
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_service_worker_grants"
down_revision: str | None = "0006_clover_integration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON monitoring_alerts TO service_worker"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE ON monitoring_alerts FROM service_worker"
    )
