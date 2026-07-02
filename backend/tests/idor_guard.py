"""Tenant-isolation lint guard (Option B backstop for the revoke-IDOR class).

WHAT THIS IS
────────────
A *runtime* guard, not a static linter. It attaches a ``before_cursor_execute``
listener to the **app engine only** (``app.core.database.get_engine`` — never the
service engine, which is legitimately cross-tenant) and inspects every SELECT /
UPDATE / DELETE the app emits while the test suite runs. Any statement that
touches a tenant-scoped table **without a ``tenant_id`` predicate** and is not on
the reviewed ALLOWLIST is a violation.

WHY THE READ (SELECT) IS THE PRIMARY CASE FOR THIS CODEBASE
───────────────────────────────────────────────────────────
The revoke-IDOR (``0d7973c``) was fixed by adding ``tenant_id`` to the *SELECT*
(the load), then ``session.delete(inv)`` deletes by PK. That DELETE is
byte-identical before and after the fix — a write-only guard cannot tell the
buggy code from the fixed code. The discriminating statement is the
**SELECT-by-non-tenant-key** (id / token) that lacks a ``tenant_id`` filter.
So this guard covers SELECT/UPDATE/DELETE; the SELECT side is load-bearing.

COVERAGE HONESTY (no-silent-caps)
─────────────────────────────────
A runtime guard only sees test-EXERCISED paths. A new repo function with an
IDOR that no test calls is invisible here. "Guard green" means "no IDOR on any
path the suite exercises" — NOT "no IDOR possible." Keep test coverage of every
tenant-scoped read/write for this guard to mean anything.

THE ALLOWLIST IS THE AUDIT
──────────────────────────
Each ALLOWLIST entry is a documented security judgment: "this read of a
tenant-scoped table is safe without a tenant_id filter because …". Building it
out IS the tenant-isolation audit the probe motivated.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

# Tables carrying a tenant_id column (information_schema, 2026-06-14). A query
# touching any of these must constrain tenant_id unless ALLOWLISTed.
TENANT_SCOPED_TABLES: frozenset[str] = frozenset(
    {
        "idempotency_keys",
        "ingredient_cost_snapshots",
        "ingredients_master",
        "inventory_count_events",
        "inventory_items",
        "inventory_movements",
        "inventory_yield_factors",
        "invitations",
        "menu_items",
        "modifier_drafts",
        "modifier_ingredients",
        "modifier_llm_suggestions",
        "modifier_versions",
        "modifiers",
        "monitoring_alerts",
        "oauth_states",
        "orders",
        "pos_event_inbox",
        "receipt_lines",
        "receipts",
        "recipe_drafts",
        "recipe_ingredients",
        "recipe_llm_suggestions",
        "recipe_versions",
        "recipes",
        "sale_line_item_modifiers",
        "sale_line_items",
        "storage_zones",
        "tenant_pos_connections",
        "unit_conversions",
        "units_of_measure",
        "user_tenants",
        "vw_depletion_coverage",
    }
)

# Reviewed-safe statements that legitimately touch a tenant-scoped table without a
# tenant_id predicate. Keyed by fingerprint(). Each carries the security judgment.
# Audit 2026-06-14: all 13 app-exercised flags verified to act on an id that was
# already tenant-authorized upstream (entry-guard 404, server-generated read-back, or
# pre-tenant bootstrap) — i.e. defense-in-depth gaps, NOT live IDOR. They are NOT
# hardened with their own tenant_id predicate (deferred — see matrix); the guard's
# job from here is to FAIL on any NEW unscoped query (e.g. a revoke-IDOR regression).
ALLOWLIST: dict[str, str] = {
    # ── invitations: bootstrap + tenant-scoped-load-then-act ──────────────────
    "select invitations.id, invitations.tenant_id, invitations.email, invitations.role, "
    "invitations.token, invitations.accepted_at, invitations.expires_at, invitations.created_by, "
    "invitations.created_at from invitations where invitations.token = $1::varchar for update": "accept-invite bootstrap: invite located by its unguessable secret token before any "
    "tenant context exists; the token is the capability.",
    "update invitations set accepted_at=$1::timestamp with time zone where invitations.id = $2::uuid": "accept-invite: id comes from the token-scoped load above, not a raw request id.",
    "delete from invitations where invitations.id = $1::uuid": "revoke_invitation: id comes from a tenant-scoped SELECT (WHERE id AND tenant_id, 404s "
    "cross-tenant — the 0d7973c fix); DELETE acts on a tenant-authorized id.",
    # ── server-generated read-backs of just-inserted rows (tenant-scoped insert) ─
    "select created_at from inventory_count_events where id = $1": "router read-back of the server-generated id from the tenant-scoped record_count_event "
    "insert; id is not request-supplied.",
    "select id, commit_state, received_at, notes from receipts where id = $1": "router read-back of the server-generated receipt id from the tenant-scoped create_receipt "
    "insert; id is not request-supplied.",
    # ── recipes: entry-guard (menu_items WHERE id AND tenant_id → 404) then by-id write ─
    # Entry-guard removal is caught by test_sprint5_phase3_recipes.test_cross_tenant_returns_404
    # (skip/patch) and test_sprint5_phase4_confirm.test_confirm_unconfirm_cross_tenant_404 — so the
    # by-id write's safety lives in those tests, not (and cannot, by fingerprint) in this allowlist.
    "update recipes set status = 'draft', updated_at = now() where id = $1": "recipe save_draft: recipe_id derived from a tenant-scoped menu_items+recipes load (route "
    "keyed by menu_item_id, 404-gated at entry). Backed by test_cross_tenant_returns_404 (PATCH).",
    "update recipes set status = 'skipped', updated_at = now() where id = $1": "recipe skip_recipe: tenant-scoped entry load first. Backed by test_cross_tenant_returns_404 (skip).",
    "update recipes set status = 'confirmed', updated_at = now() where id = $1": "recipe confirm_recipe (verified menu_items WHERE id AND tenant_id → 404 at entry). "
    "Backed by test_confirm_unconfirm_cross_tenant_404.",
    # ── modifiers: _require_modifier (WHERE id AND tenant_id AND menu_item_id → 404) then by-id ─
    # Single shared chokepoint; its removal fails test_sprint5_phase6_modifiers.
    # test_cross_tenant_and_cross_item_404 (confirm + patch + wrong-parent).
    "update modifiers set status = 'draft', current_version_id = null, updated_at = now() where id = $1": "modifiers unconfirm: _require_modifier 404-gates (id, tenant_id, menu_item_id). "
    "Backed by test_cross_tenant_and_cross_item_404.",
    "update modifiers set status = 'skipped', updated_at = now() where id = $1": "modifiers skip_modifier: _require_modifier 404-gates. Backed by test_cross_tenant_and_cross_item_404.",
    "select draft_ingredients from modifier_drafts where modifier_id = $1": "modifiers: modifier_id 404-gated by _require_modifier upstream. "
    "Backed by test_cross_tenant_and_cross_item_404.",
    "update modifiers set status = 'draft', updated_at = now() where id = $1": "modifiers save_draft: _require_modifier 404-gates. Backed by test_cross_tenant_and_cross_item_404.",
    "update modifiers set current_version_id = $1, status = 'confirmed', updated_at = now() where id = $2": "modifiers confirm_modifier: _require_modifier 404-gates. Backed by test_cross_tenant_and_cross_item_404.",
}

_FROM_RE = re.compile(r"\bfrom\s+([a-z_][a-z0-9_]*)", re.IGNORECASE)
_JOIN_RE = re.compile(r"\bjoin\s+([a-z_][a-z0-9_]*)", re.IGNORECASE)
_UPDATE_RE = re.compile(r"\bupdate\s+(?:only\s+)?([a-z_][a-z0-9_]*)", re.IGNORECASE)
_DELETE_RE = re.compile(r"\bdelete\s+from\s+(?:only\s+)?([a-z_][a-z0-9_]*)", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")

# tenant_id used as a *filter predicate* — NOT merely a selected/returned column.
# ORM selects emit `table.tenant_id,` in the column list; that is not a constraint.
# A constraint is `tenant_id <op> …` or `tenant_id IS …` / `IN` / `BETWEEN` / `= ANY`,
# including the `IS NULL` global-reference-row predicate (a deliberate tenant scope).
_PREDICATE_RE = re.compile(
    r"tenant_id\b\s*(=|<|>|!=|<>|\bin\b|\bis\b|\bbetween\b|\bany\b)",
    re.IGNORECASE,
)


@dataclass
class Violation:
    verb: str
    tables: tuple[str, ...]
    fingerprint: str
    sql: str


@dataclass
class GuardState:
    measure_only: bool = False  # True => collect+report, never fail
    seen: int = 0  # total statements inspected (coverage sanity)
    violations: dict[str, Violation] = field(default_factory=dict)  # fingerprint -> first sighting


_APP_DIR = f"{os.sep}app{os.sep}"
_TESTS_DIR = f"{os.sep}tests{os.sep}"


def _caller_files() -> list[str]:
    """Filenames innermost-first across the async/greenlet boundary.

    AsyncSession routes every execute through SQLAlchemy's ``greenlet_spawn``, so
    the DB event fires inside a WORKER greenlet whose ``f_back`` chain is pure
    SQLAlchemy — the real async caller (app repo / test fn) lives in the suspended
    PARENT greenlet. So we walk this greenlet's frames, then each parent greenlet's
    ``gr_frame`` chain, to recover the true issuing frames."""
    files: list[str] = []
    f = sys._getframe(1)
    while f is not None:
        files.append(f.f_code.co_filename)
        f = f.f_back
    try:
        import greenlet
    except ImportError:
        return files
    g = greenlet.getcurrent()
    while g is not None and getattr(g, "parent", None) is not None:
        g = g.parent
        gf = getattr(g, "gr_frame", None)
        while gf is not None:
            files.append(gf.f_code.co_filename)
            gf = gf.f_back
    return files


def app_origin() -> bool:
    """True iff the statement was issued from APP code (not a test assertion).

    The guard polices the application's own queries, not the suite's verification
    reads. A test that does ``session.execute(text('SELECT … WHERE id=…'))`` to
    assert state is legitimately cross-tenant-capable and must not be flagged.

    Walk caller frames innermost-first (across greenlets) and decide on whichever
    of app/ or tests/ appears first — for an app route the repo frame (under app/)
    precedes the test frame; for a test read the test file is the issuer.

    The guard's own infra frames (this module + the conftest listener) are the
    innermost frames and live under tests/ — skip them or they'd short-circuit
    every decision to False.

    FAILS OPEN: a statement whose walk finds neither an app/ nor tests/ frame
    returns False (not policed). For this codebase's request→router→repo→session
    shape that does not strand an app query, but a future reader should know an
    undetermined-origin query is silently skipped."""
    for fn in _caller_files():
        if fn.endswith("idor_guard.py") or fn.endswith("conftest.py"):
            continue
        if _APP_DIR in fn:
            return True
        if _TESTS_DIR in fn:  # test file / tests/helpers/
            return False
    return False  # pure framework/infra origin — not app code, don't police


def fingerprint(sql: str) -> str:
    """Stable signature: lowercase, whitespace-collapsed, params already $1/%(x)s."""
    return _WS_RE.sub(" ", sql.strip().lower())


def _verb(sql_l: str) -> str | None:
    for v in ("select", "update", "delete", "insert"):
        if sql_l.lstrip("(").startswith(v):
            return v
    return None


def _referenced_tables(sql_l: str, verb: str) -> set[str]:
    tables: set[str] = set()
    if verb == "update":
        tables.update(m.group(1).lower() for m in _UPDATE_RE.finditer(sql_l))
    elif verb == "delete":
        tables.update(m.group(1).lower() for m in _DELETE_RE.finditer(sql_l))
    # FROM/JOIN apply to SELECT (and DELETE…USING / UPDATE…FROM, harmless extras)
    tables.update(m.group(1).lower() for m in _FROM_RE.finditer(sql_l))
    tables.update(m.group(1).lower() for m in _JOIN_RE.finditer(sql_l))
    return tables


def inspect(sql: str) -> Violation | None:
    """Return a Violation if `sql` reads/writes a tenant-scoped table with no
    tenant_id predicate and is not ALLOWLISTed; else None. INSERT is exempt
    (its tenant_id lives in VALUES, not a WHERE)."""
    sql_l = sql.lower()
    verb = _verb(sql_l)
    if verb is None or verb == "insert":
        return None
    touched = _referenced_tables(sql_l, verb) & TENANT_SCOPED_TABLES
    if not touched:
        return None
    if _PREDICATE_RE.search(sql_l):  # tenant_id constrained in a predicate (not just selected)
        return None
    fp = fingerprint(sql)
    if fp in ALLOWLIST:
        return None
    return Violation(verb=verb, tables=tuple(sorted(touched)), fingerprint=fp, sql=sql.strip())
