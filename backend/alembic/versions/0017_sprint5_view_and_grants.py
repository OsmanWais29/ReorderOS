"""Sprint 5 Phase 0d — coverage view + service_worker grants.

Revision ID: 0017_sprint5_view_and_grants
Revises: 0016_sprint5_tighten_constraints
Create Date: 2026-06-03 00:00:00.000000

Per sprint-5-phase-map.md Phase 0d. Metadata-only (Migration Risk Standard §2.1:
view + permissions; no existing-data validation). Final Phase-0 migration.

Contents:
  1. vw_depletion_coverage — count% + revenue% coverage per tenant (v5 §6).
     Created WITH (security_invoker = true) so it runs with the QUERYING role's
     privileges and therefore RESPECTS the RLS on sale_line_items. Without that,
     a view owned by the superuser bypasses RLS and would leak cross-tenant
     coverage to app_user. With it, app_user sees only its own tenant's row.
  2. service_worker UPDATE on sale_line_items — COLUMN-LEVEL on exactly
     (depletion_status, depletion_reason). Table-wide UPDATE would let the worker
     mutate net_revenue_cents / is_refunded / recipe_version_id (the snapshot) and
     other business fields it has no reason to touch. Column grants compose with
     RLS (columns vs rows are orthogonal): the existing sli_tenant_isolation
     policy still scopes updates to the worker's tenant rows.
  3. service_worker writes on menu_items — Phase 2 catalog-sync prerequisite.
     INSERT (table-level) + UPDATE COLUMN-SCOPED to the catalog-owned columns
     (name, active, pos_item_id) — explicitly EXCLUDING recipe_version_id, which
     is operator-owned (set by recipe-confirm). A table-wide UPDATE would let a
     re-sync clobber a confirmed recipe association. A SEPARATE service_worker
     policy (app_user's untouched); tenant-scoped USING + WITH CHECK. No DELETE —
     removal is soft-delete via active=false (an UPDATE), per v5 §4. SELECT is
     already granted by 0012, so 0017 adds only the write privileges (granting/
     revoking SELECT here would corrupt the 0016 baseline on downgrade).

Risk classification (Migration Risk Standard §1.2):
  - Data validity:            Low — view + grants only; validates no existing data.
  - Availability impact:      Low — no locks of consequence; CREATE VIEW / GRANT.
  - Application compatibility: Low — purely additive privileges; no field removed.
  - Data propagation risk:    Low — no replication-affecting change.
  - Reversibility:            Low — downgrade drops the view, the column grant,
                                    the menu_items policy, and the menu_items grants.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017_sprint5_view_and_grants"
down_revision: str | None = "0016_sprint5_tighten_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same tenant-isolation predicate as everywhere else (0011/0013/0014).
_T1 = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    # ═════════════════════════════════════════════════════════════════════════
    # 1. vw_depletion_coverage — operator coverage metric (v5 §6).
    #    security_invoker=true → RLS on sale_line_items applies to the querying
    #    role, so app_user sees only its own tenant's aggregate row.
    #    Divide-by-zero / NULL handling:
    #      - count pct: COUNT(*) FILTER returns 0 (not NULL), so a zero-depleted
    #        tenant reads 0.00; NULLIF(COUNT(*),0) guards the empty-window case.
    #      - revenue pct: a filtered SUM returns NULL when no rows match, so a
    #        tenant with revenue but zero depleted lines would read NULL. That is
    #        wrong (it should be 0%) AND it would dodge the coverage-collapse
    #        alert (`pct < threshold` is UNKNOWN for NULL). COALESCE the numerator
    #        to 0 so it reads 0.00. NULLIF stays on the denominator — NULL is the
    #        honest answer only when total revenue is genuinely zero.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute("""
        CREATE VIEW vw_depletion_coverage
        WITH (security_invoker = true) AS
        SELECT
            tenant_id,
            COUNT(*) FILTER (WHERE depletion_status = 'depleted')        AS depleted_count,
            COUNT(*)                                                     AS total_count,
            ROUND(
                100.0 * COUNT(*) FILTER (WHERE depletion_status = 'depleted')
                / NULLIF(COUNT(*), 0),
                2
            )                                                            AS depleted_count_pct,
            SUM(net_revenue_cents) FILTER (WHERE depletion_status = 'depleted')
                                                                        AS depleted_revenue_cents,
            SUM(net_revenue_cents)                                       AS total_revenue_cents,
            ROUND(
                100.0 * COALESCE(SUM(net_revenue_cents) FILTER (WHERE depletion_status = 'depleted'), 0)
                / NULLIF(SUM(net_revenue_cents), 0),
                2
            )                                                            AS depleted_revenue_pct
        FROM sale_line_items
        WHERE created_at > now() - INTERVAL '30 days'
        GROUP BY tenant_id
    """)
    # Consumer is the operator coverage surface (app_user). Ops F5.x diagnostics
    # run as the admin/superuser role, which bypasses RLS and can inspect any
    # tenant via an explicit WHERE tenant_id = :tid.
    op.execute("GRANT SELECT ON vw_depletion_coverage TO app_user")

    # ═════════════════════════════════════════════════════════════════════════
    # 2. service_worker — column-level UPDATE on the two depletion lifecycle
    #    columns only. The Phase 9 handler flips pending → depleted/failed and
    #    sets depletion_reason; it must NOT touch any other sale_line_items field.
    #    RLS (sli_tenant_isolation, FOR ALL TO app_user, service_worker) already
    #    scopes which ROWS the worker may update to its tenant.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute(
        "GRANT UPDATE (depletion_status, depletion_reason)"
        " ON sale_line_items TO service_worker"
    )

    # ═════════════════════════════════════════════════════════════════════════
    # 3. service_worker — menu_items access for catalog sync (Phase 2 prereq).
    #    Separate policy so app_user's existing menu_items_tenant policy and
    #    grants are untouched. INSERT (new items) + UPDATE (rename / category /
    #    is_active soft-delete) + SELECT (re-sync diff). No DELETE.
    # ═════════════════════════════════════════════════════════════════════════
    op.execute(f"""
        CREATE POLICY menu_items_service_isolation ON menu_items
            FOR ALL TO service_worker
            USING ({_T1}) WITH CHECK ({_T1})
    """)
    # menu_items column ownership (enumerated so future columns get classified):
    #   id                — identity; never updated
    #   tenant_id         — RLS scope; written on INSERT, never re-assigned
    #   pos_item_id       — CATALOG-owned (Clover item id)
    #   name              — CATALOG-owned
    #   active            — CATALOG-owned (soft-delete toggle; sync sets false on removal)
    #   created_at        — immutable
    #   recipe_version_id — OPERATOR-owned: set ONLY by the recipe-confirm flow.
    #                       A table-wide UPDATE would let a catalog re-sync clobber
    #                       a confirmed recipe association, silently un-mapping the
    #                       item — so UPDATE is column-scoped to the catalog set.
    #
    # SELECT pre-exists from 0012 (see above) — add only the write privileges.
    # INSERT is table-level (a new row has no recipe yet, so no clobber risk; and
    # recipe_version_id is nullable → defaults NULL when sync omits it).
    op.execute("GRANT INSERT ON menu_items TO service_worker")
    op.execute("GRANT UPDATE (name, active, pos_item_id) ON menu_items TO service_worker")


def downgrade() -> None:
    # Only remove what 0017 added — leave the 0012 SELECT grant intact.
    op.execute(
        "REVOKE UPDATE (name, active, pos_item_id) ON menu_items FROM service_worker"
    )
    op.execute("REVOKE INSERT ON menu_items FROM service_worker")
    op.execute("DROP POLICY IF EXISTS menu_items_service_isolation ON menu_items")

    op.execute(
        "REVOKE UPDATE (depletion_status, depletion_reason)"
        " ON sale_line_items FROM service_worker"
    )

    # Dropping the view drops its grants with it.
    op.execute("DROP VIEW IF EXISTS vw_depletion_coverage")
