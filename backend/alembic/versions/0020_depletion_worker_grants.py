"""Sprint 5 Phase 9 — service_worker read grants for the new depletion walker.

Revision ID: 0020_depletion_worker_grants
Revises: 0019_inventory_name_dedup
Create Date: 2026-06-07 00:00:00.000000

(Revision id <=32 chars — alembic_version.version_num is varchar(32).)

The Phase 9 walker computes depletion from the confirmed recipe_version: it reads
recipe_versions.yield_quantity and resolves each ingredient's storage unit via
units_of_measure. The retired writer never read these (it used the per-item
storage_to_recipe_factor), so service_worker was never granted SELECT on them — the
worker fails with InsufficientPrivilegeError until these grants land. (Caught by the
e2e worker test, which runs as the real service_worker role; the depletion unit tests
run as superuser and bypass grants.)

These are immutable confirmed / reference data that depletion legitimately consumes —
unlike recipes (mutable operator draft state), which the worker must NOT read and is
deliberately not granted. The existing per-table RLS tenant policies still apply; the
worker sets app.tenant_id, so reads stay tenant-scoped.

Risk (Migration Risk Standard §1.2): metadata only (GRANTs) — Low on every dimension.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020_depletion_worker_grants"
down_revision: str | None = "0019_inventory_name_dedup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON recipe_versions TO service_worker")
    op.execute("GRANT SELECT ON units_of_measure TO service_worker")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON recipe_versions FROM service_worker")
    op.execute("REVOKE SELECT ON units_of_measure FROM service_worker")
