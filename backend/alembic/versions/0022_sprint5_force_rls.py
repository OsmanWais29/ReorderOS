"""FORCE ROW LEVEL SECURITY on every app/worker tenant table missing it.

Revision ID: 0022_sprint5_force_rls
Revises: 0021_refund_reversal_grant
Create Date: 2026-06-25 00:00:00.000000

Closes a cross-tenant isolation gap. 0014 enabled RLS (`ENABLE ROW LEVEL SECURITY`)
+ tenant-isolation policies on the 10 Sprint 5 tables but never paired it with
`FORCE` — unlike 0003, which pairs ENABLE+FORCE on every tenant table. The audit
that prompted this found the gap is broader than Sprint 5:

  - menu_items (0012): ENABLE-only and READ on the app/request path (recipes +
    modifiers repos via get_rls_session).
  - sale_line_items (0006): 0006 INTENDED FORCE, but the FORCE statement lives in
    downgrade() (a copy of the old 0003 shape); upgrade() never forces it, so it
    has been ENABLE-only.
  - orders (0006): same ENABLE-only situation as sale_line_items.

WHEN this is load-bearing (be precise — the earlier "live hole" framing was too
strong): FORCE only changes behavior for a query executed AS the table OWNER (a
non-owner role is already subject to RLS under plain ENABLE; a superuser bypasses
even FORCE). So the exposure exists IFF the production request path connects as a
non-superuser role that OWNS these tables (it ran the migrations) AND does not
SET ROLE to a non-owner — which matches this codebase, since runtime sets the
app.tenant_id GUCs but never SET ROLE. In the test DB the connection role is the
superuser `reorderos` and `app_user` is a non-owner, so FORCE is a no-op there and
its effect cannot be exercised by the suite. CONFIRM the production DATABASE_URL
role's rolsuper/rolbypassrls/ownership to know whether this closed a live hole or
is defense-in-depth. Either way FORCE is correct, matches the 0003 convention, and
cannot reduce isolation.

Deliberately EXCLUDED: pos_event_inbox and tenant_pos_connections use USING(true)
cross-tenant policies BY DESIGN (the webhook/inbox worker must see all tenants'
rows before resolving merchant→tenant). FORCE there would be wrong, and the guard
test allowlists exactly these two.

Why FORCE matters at all
------------------------
Plain `ENABLE` RLS is bypassed by the table OWNER (only). The codebase's stated
model is that the request path relies on FORCE for enforcement (test_rls.py: app_conn
"is subject to FORCE ROW LEVEL SECURITY"; deps.py: GUCs are set "so that FORCE ROW
LEVEL SECURITY policies activate correctly for app_user connections"). 0003 pairs
ENABLE+FORCE on every tenant table; Sprint 5 (0014) did not. This restores that
convention so the guarantee does not depend on which role happens to own the table.

This also extends the test_force_rls_tables_exist guard (the RLS Option-3 "lint
guard now") to assert FORCE on every tenant table except the two documented
USING(true) inbox tables, so a future table that forgets FORCE fails CI.

§4.5 application-impact verification (Migration Risk Standard)
-------------------------------------------------------------
FORCE strengthens isolation; it cannot expose rows that ENABLE+policies already
permitted. All Sprint 5 read/write paths already run with app.tenant_id set (the
recipes/modifiers routers via get_rls_session; the worker via set_config), so every
legitimate query continues to match its tenant policy. No query that was previously
correct becomes blocked. Proven by the full Sprint 5 suite staying green post-FORCE.

Risk classification (Migration Risk Standard §1.2):
  - Data validity:             Low — no data is read or written; catalog flag only.
  - Availability impact:       Low — ALTER ... FORCE takes a brief ACCESS EXCLUSIVE
                                     lock but performs NO table scan (instant on any
                                     size); pilot-scale tables make it negligible.
  - Application compatibility: Low — additive isolation; all app/worker paths set
                                     app.tenant_id and remain within policy.
  - Data propagation risk:     Low — no replication-affecting change.
  - Reversibility:             Low — downgrade NO FORCEs exactly these 10 tables,
                                     restoring the prior ENABLE-only posture.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022_sprint5_force_rls"
down_revision: str | None = "0021_refund_reversal_grant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every tenant table that was ENABLE-only and is reachable by the app/worker under
# a tenant-scoped policy. All already have ENABLE RLS + a tenant-isolation policy;
# this adds the missing FORCE so the table-owning request-path role is subject to
# those policies. Excludes pos_event_inbox + tenant_pos_connections (USING(true) by
# design). Keep this list in sync with the test_force_rls_tables_exist guard.
_TABLES_TO_FORCE = (
    # Sprint 5 (0014) — app request path
    "recipes",
    "recipe_drafts",
    "recipe_llm_suggestions",
    "modifiers",
    "modifier_versions",
    "modifier_drafts",
    "modifier_llm_suggestions",
    "modifier_ingredients",
    "sale_line_item_modifiers",
    "unit_conversions",
    # Pre-Sprint-5 gaps surfaced by the audit
    "menu_items",  # 0012 — live app-path hole (recipes/modifiers repos)
    "sale_line_items",  # 0006 — FORCE was only in downgrade(); worker-only, latent
    "orders",  # 0006 — worker-only, latent; forced for uniformity
)


def upgrade() -> None:
    for tbl in _TABLES_TO_FORCE:
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    for tbl in _TABLES_TO_FORCE:
        op.execute(f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY")
