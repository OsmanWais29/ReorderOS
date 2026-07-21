"""Stock Item Insights — PR-A1 (actuals only).

Every number here derives from inventory_movements / on_hand() / POS ingest
state. No LLM, no fabrication. Forecast, days-of-cover, trend, and reorder are
NOT built here — they return NOT_YET_CERTIFIED, gated on the completeness
watermark (which cannot advance yet; see balance_projection + migration 0034).

Sections produced: item state, ledger reconciliation (Mode A; Mode B reports
RECONCILIATION_UNAVAILABLE), POS diagnostics (INDEPENDENT, evidence-aware
dimensions — never one 'healthy' boolean, never 'ok' without evidence; the
coverage funnel is split into recipe_mapping / conversion_coverage /
depletion_execution / end_to_end_coverage so a conversion or depletion failure is
never mislabeled 'unmapped'), observed consumption (ONE confidence object; always
a floor in A1 because provider completeness is unproven), affected menu items
(id-keyed, tenant-safe, with repair routes and truncation), menu-item
contributors, deterministic reasons, cost. RBAC: the latest unit cost is visible
to all; supplier identity + cost history are gated by `can_view_aggregated_cost`.

The caller MUST invoke build_item_insights inside one read-only REPEATABLE READ
transaction with a DB-derived as_of, so all queries read one snapshot.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.inventory.balance_projection import balance_before_mode_a, current_balance
from app.modules.pos.schedule import RECONCILIATION_STALE_AFTER_SECONDS

WINDOW_DAYS = {"7d": 7, "14d": 14, "30d": 30}

# Mode-A ledger category mapping — partitions EVERY movement type on_hand() counts
# (all except sale signals), so the displayed rows sum to the exact window balance
# change. A type missing from this map surfaces as a non-zero reconciliation_delta.
_LEDGER_CATEGORY = {
    "opening_balance": "received",
    "receive": "received",
    "transfer_in": "received",
    "sale_depletion": "pos_consumption",
    "sale_depletion_reversal": "refund_reversals",
    "count_adjust": "adjustments",
    "adjustment": "adjustments",
    "waste": "adjustments",
    "transfer_out": "adjustments",
}
_CATEGORY_ORDER = ["received", "pos_consumption", "refund_reversals", "adjustments"]


class ItemNotFound(Exception):
    """The item does not exist for this tenant (→ 404 ITEM_NOT_FOUND)."""


@dataclass
class _Item:
    name: str
    inventory_mode: str
    storage_unit: str
    par_level: Decimal | None
    last_count_at: datetime | None
    last_count_quantity: Decimal | None


def _d(v: object) -> Decimal:
    return Decimal(str(v))


def _s(v: Decimal | None) -> str | None:
    """Canonical quantity string: fixed-point, trailing zeros trimmed, no exponent.

    Keeps arithmetic-derived quantities stable regardless of numeric scale
    ('200.0' and '200' both render '200') so reconciliation compares cleanly."""
    if v is None:
        return None
    txt = format(v, "f")
    if "." in txt:
        txt = txt.rstrip("0").rstrip(".")
    return txt or "0"


async def _load_item(s: AsyncSession, tid: UUID, iid: UUID) -> _Item:
    row = (
        (
            await s.execute(
                text("""
                SELECT ii.name, ii.inventory_mode, u.name AS storage_unit,
                       ii.par_level, ii.last_count_at, ii.last_count_quantity
                  FROM inventory_items ii
                  JOIN units_of_measure u ON u.id = ii.storage_unit_id
                 WHERE ii.tenant_id = :t AND ii.id = :i AND ii.active = true
            """),
                {"t": tid, "i": iid},
            )
        )
        .mappings()
        .fetchone()
    )
    if row is None:
        raise ItemNotFound
    return _Item(
        name=row["name"],
        inventory_mode=row["inventory_mode"],
        storage_unit=row["storage_unit"],
        par_level=_d(row["par_level"]) if row["par_level"] is not None else None,
        last_count_at=row["last_count_at"],
        last_count_quantity=(
            _d(row["last_count_quantity"]) if row["last_count_quantity"] is not None else None
        ),
    )


async def _ledger(
    s: AsyncSession,
    tid: UUID,
    iid: UUID,
    item: _Item,
    window_start: datetime,
    on_hand: Decimal | None,
) -> dict[str, Any]:
    """Mode-A reconciled ledger; Mode B → RECONCILIATION_UNAVAILABLE."""
    rows_raw = (
        (
            await s.execute(
                text("""
                SELECT movement_type, COALESCE(SUM(delta), 0) AS qty, COUNT(*) AS n
                  FROM inventory_movements
                 WHERE tenant_id = :t AND inventory_item_id = :i
                   AND created_at >= :ws AND created_at < :ao
                 GROUP BY movement_type
            """),
                {"t": tid, "i": iid, "ws": window_start, "ao": _AS_OF.get()},
            )
        )
        .mappings()
        .all()
    )

    cat_qty: dict[str, Decimal] = {c: Decimal("0") for c in _CATEGORY_ORDER}
    cat_events: dict[str, int] = {c: 0 for c in _CATEGORY_ORDER}
    uncategorized = False
    for r in rows_raw:
        mt = r["movement_type"]
        if mt in ("sale_signal", "sale_signal_reversal"):
            continue
        cat = _LEDGER_CATEGORY.get(mt)
        if cat is None:
            uncategorized = True
            continue
        cat_qty[cat] += _d(r["qty"])
        cat_events[cat] += int(r["n"])

    display_rows = [
        {"kind": c, "quantity": _s(cat_qty[c]), "events": cat_events[c]} for c in _CATEGORY_ORDER
    ]

    if item.inventory_mode != "recipe_deducted":
        # Time-bounded count-anchor balance is not built in PR-A1.
        return {
            "mode": item.inventory_mode,
            "anchor_basis": "count" if item.last_count_at else "historical_window",
            "anchor_at": item.last_count_at.isoformat() if item.last_count_at else None,
            "anchor_quantity": _s(item.last_count_quantity),
            "reconciled": False,
            "state": "RECONCILIATION_UNAVAILABLE",
            "reason": {"code": "MODE_B_RECONCILIATION_PENDING"},
            "rows": display_rows,
            "current_on_hand": _s(on_hand),
        }

    balance_start = await balance_before_mode_a(
        s, tenant_id=tid, inventory_item_id=iid, before=window_start
    )
    rows_sum = sum(cat_qty.values(), Decimal("0"))
    window_change = (
        (on_hand - balance_start) if (on_hand is not None and balance_start is not None) else None
    )
    delta = (window_change - rows_sum) if window_change is not None else None
    reconciled = delta == Decimal("0") and not uncategorized

    result = {
        "mode": item.inventory_mode,
        "anchor_basis": "ledger_sum",
        "balance_at_window_start": _s(balance_start),
        "anchor_at": None,
        "anchor_quantity": None,
        "reconciled": reconciled,
        "state": "OK" if reconciled else "DATA_INCONSISTENT",
        "rows": display_rows if reconciled else [],
        "window_sum": _s(rows_sum),
        "current_on_hand": _s(on_hand),
        "reconciliation_delta": _s(delta),
    }
    return result


# Named thresholds (not magic numbers). Processing lease TTL mirrors the POS
# worker's CLAIM_TTL_SECONDS=300. Event-activity idle horizon marks a feed
# 'quiet' (e.g. a closed restaurant) — NOT broken.
_PROCESSING_LEASE_SECONDS = 300
_EVENT_QUIET_HOURS = 24
# Reconciliation freshness is derived from the ACTUAL worker schedule (shared
# constant, imported below) — NOT the loose event-quiet horizon.

# Eligible sale line = one the production depletion rules SHOULD have depleted:
# order locked + payment PAID/PARTIALLY_REFUNDED, line not voided, not refunded.
# This is the shared denominator for the coverage funnel — only genuinely
# ineligible lines are excluded.
_ELIGIBLE_PREDICATE = (
    "o.state = 'locked' AND o.payment_state IN ('PAID','PARTIALLY_REFUNDED') "
    "AND sli.is_voided = false AND sli.is_refunded = false"
)

# depletion_reason → stable failure code. Anything unrecognized → UNKNOWN_FAILURE
# (never silently classified as a depletion failure).
_REASON_CODE = {
    "no_recipe": "NO_RECIPE",
    "recipe_draft": "NO_RECIPE",
    "recipe_skipped": "NO_RECIPE",
    "invalid_recipe": "INVALID_RECIPE",
    "missing_conversion": "MISSING_CONVERSION",
    "computation_error": "DEPLETION_FAILED",
}
_REPAIR = {
    "NO_RECIPE": "review_recipe",
    "INVALID_RECIPE": "review_recipe",
    # A missing conversion could be ANY ingredient in the recipe — we cannot name
    # which one, so we route to recipe inspection, never "fix THIS conversion".
    "MISSING_CONVERSION": "inspect_recipe_conversions",
    "DEPLETION_FAILED": None,
    "PROCESSING_PENDING": None,
    "UNKNOWN_FAILURE": None,
}
_AFFECTED_LIMIT = 20

# Grouped affected-lines SQL (predicate is a hardcoded literal). Consumed by both
# the count and the LIMIT query so pagination happens in SQL, not Python.
_AFFECTED_GROUPED_SQL = (
    "SELECT sli.menu_item_id AS menu_item_id, "  # noqa: S608 — predicate is a literal
    "COALESCE(mi.name, sli.name_at_sale) AS name, "
    "sli.depletion_reason AS reason, sli.depletion_status AS status, "
    "COUNT(*) AS lines, COALESCE(SUM(sli.quantity), 0) AS sold, "
    "COALESCE(SUM(sli.net_revenue_cents), 0) AS revenue "
    "FROM sale_line_items sli JOIN orders o ON o.id = sli.order_id "
    "LEFT JOIN menu_items mi ON mi.id = sli.menu_item_id AND mi.tenant_id = sli.tenant_id "
    "WHERE sli.tenant_id = :t AND o.closed_at >= :ws AND o.closed_at < :ao "
    f"AND {_ELIGIBLE_PREDICATE} AND sli.depletion_status <> 'depleted' "
    "GROUP BY sli.menu_item_id, COALESCE(mi.name, sli.name_at_sale), "
    "sli.depletion_reason, sli.depletion_status"
)


def _pct(num: int, den: int) -> str | None:
    return None if den == 0 else str((_d(num) / _d(den) * 100).quantize(Decimal("0.1")))


def _stage_status(*, denominator: int, passed: int, failed: int, pending: int, unknown: int) -> str:
    """Evidence-only stage status, reported at the WORST severity present so a
    known failure is NEVER hidden by pending/unknown rows. Severity order:
    failures > unknown > in_progress > ok. 'ok' requires positive evidence that
    EVERY denominator row passed (passed == denominator) — never inferred by
    subtracting failures. All co-occurring counts are kept on the dimension."""
    if denominator == 0:
        return "unavailable"
    if failed > 0:
        return "failures"
    if unknown > 0:
        return "unknown"
    if pending > 0:
        return "in_progress"
    return "ok" if passed == denominator else "unknown"


async def _pos_diagnostics(s: AsyncSession, tid: UUID, window_start: datetime) -> dict[str, Any]:
    """POS health as INDEPENDENT, evidence-aware dimensions — never one 'healthy'
    boolean, and never 'ok' without positive per-row evidence. Everything here is
    TENANT-scoped pipeline health, NOT ingredient-level completeness for the item.
    The coverage funnel is split so a conversion/depletion failure is never
    mislabeled 'unmapped'."""
    now = _AS_OF.get()
    conn = (
        (
            await s.execute(
                text("""
                SELECT connection_id, vendor, state, last_reconciliation_at,
                       orders_complete_through
                  FROM tenant_pos_connections
                 WHERE tenant_id = :t
                 ORDER BY (state = 'active') DESC, updated_at DESC
                 LIMIT 1
            """),
                {"t": tid},
            )
        )
        .mappings()
        .fetchone()
    )

    # ── connection ──
    if conn is None:
        connection = {"status": "disconnected", "provider": None, "state": "not_connected"}
        conn_state, provider, cid = None, None, None
    else:
        conn_state, provider, cid = conn["state"], conn["vendor"], conn["connection_id"]
        connection = {
            "status": (
                "connected"
                if conn_state == "active"
                else "error"
                if conn_state == "error"
                else "disconnected"
            ),
            "provider": provider,
            "state": conn_state,
        }
    connected = conn is not None and conn_state == "active"

    # ── inbox: CURRENT health scoped to THIS connection (finding 3). Events from
    #    old/revoked connections are counted separately, never inflating current
    #    processing health. Both received AND processed timestamps are restricted
    #    to ORDER events ('O'), so a catalog event can't look like sales data.
    inbox = (
        (
            await s.execute(
                text("""
                SELECT
                    COUNT(*) FILTER (WHERE connection_id = :cid AND state = 'pending') AS pending,
                    COUNT(*) FILTER (WHERE connection_id = :cid AND state = 'processing')
                        AS processing,
                    COUNT(*) FILTER (WHERE connection_id = :cid AND state = 'failed') AS failed,
                    COUNT(*) FILTER (WHERE connection_id = :cid AND state = 'dead_letter')
                        AS dead_letter,
                    MIN(received_at)
                        FILTER (WHERE connection_id = :cid AND state = 'pending') AS oldest_pending,
                    MIN(processing_started_at)
                        FILTER (WHERE connection_id = :cid AND state = 'processing')
                        AS oldest_processing,
                    MAX(received_at) FILTER (WHERE connection_id = :cid
                        AND vendor_object_type = 'O') AS last_received,
                    MAX(processed_at) FILTER (WHERE connection_id = :cid
                        AND vendor_object_type = 'O') AS last_processed,
                    COUNT(*) FILTER (WHERE (connection_id IS DISTINCT FROM :cid)
                        AND state IN ('pending','processing','failed','dead_letter'))
                        AS historical_unresolved
                  FROM pos_event_inbox
                 WHERE tenant_id = :t
            """),
                {"t": tid, "cid": cid},
            )
        )
        .mappings()
        .one()
    )
    last_received = inbox["last_received"]
    last_processed = inbox["last_processed"]
    pending = int(inbox["pending"])
    processing = int(inbox["processing"])
    failed = int(inbox["failed"])
    dead_letter = int(inbox["dead_letter"])
    oldest_pending = inbox["oldest_pending"]
    oldest_processing = inbox["oldest_processing"]
    historical_unresolved = int(inbox["historical_unresolved"])

    # ── event_activity: whether SALES EVENTS are arriving. 'quiet' ≠ broken. ──
    if not connected:
        ev_status = "unavailable"
    elif last_received is None:
        ev_status = "none"
    elif (now - last_received).total_seconds() > _EVENT_QUIET_HOURS * 3600:
        ev_status = "quiet"
    else:
        ev_status = "active"
    event_activity = {
        "status": ev_status,
        "latest_sales_data_received_at": _iso(last_received),
    }

    # ── reconciliation_health: reported SEPARATELY; distinguishes never-run /
    #    recent / stale via the timestamp. NEVER a green 'healthy' badge — in A1
    #    it cannot certify completeness.
    recon_stale_after_s = RECONCILIATION_STALE_AFTER_SECONDS
    if not connected:
        recon_status = "unavailable"
    elif conn is None or conn["last_reconciliation_at"] is None:
        recon_status = "never_run"
    else:
        age_s = (now - conn["last_reconciliation_at"]).total_seconds()
        recon_status = "recent" if age_s <= recon_stale_after_s else "stale"
    reconciliation_health = {
        "status": recon_status,  # unavailable | never_run | recent | stale
        "latest_background_reconciliation_check_at": (
            _iso(conn["last_reconciliation_at"]) if conn is not None else None
        ),
        "stale_after_seconds": recon_stale_after_s,
        "certifies_completeness": False,
    }

    # ── processing: worker keeping up (current connection only). ──
    proc_stalled = (
        oldest_processing is not None
        and (now - oldest_processing).total_seconds() > _PROCESSING_LEASE_SECONDS
    )
    if not connected:
        proc_status = "unavailable"
    elif proc_stalled:
        proc_status = "stalled"
    elif pending > 0 or processing > 0 or failed > 0 or dead_letter > 0:
        proc_status = "backlogged"
    else:
        proc_status = "current"
    processing_dim = {
        "status": proc_status,
        "latest_sales_data_processed_at": _iso(last_processed),
        "pending_event_count": pending,
        "processing_event_count": processing,
        "failed_event_count": failed,
        # 'dead letter' is internal queue jargon — surfaced under a neutral name.
        "permanently_failed_event_count": dead_letter,
        "oldest_pending_at": _iso(oldest_pending),
        "oldest_processing_at": _iso(oldest_processing),
        # Old/revoked-connection events that are still unresolved, kept SEPARATE
        # from current health so they never make a fresh connection look broken.
        "historical_unresolved_event_count": historical_unresolved,
    }

    # ── coverage funnel over ELIGIBLE lines. Per-stage counts are EXPLICIT
    #    (pass/fail/pending/unknown) — success is never inferred by subtraction.
    cov = (
        (
            await s.execute(
                text(f"""
                SELECT
                    COUNT(*) AS eligible_lines,
                    COALESCE(SUM(sli.net_revenue_cents), 0) AS eligible_rev,
                    COUNT(*) FILTER (WHERE sli.depletion_status = 'depleted') AS depleted_lines,
                    COALESCE(SUM(sli.net_revenue_cents)
                             FILTER (WHERE sli.depletion_status = 'depleted'), 0) AS depleted_rev,
                    -- explicit recipe-stage evidence
                    COUNT(*) FILTER (WHERE sli.depletion_status = 'depleted'
                        OR sli.depletion_reason IN ('missing_conversion','computation_error'))
                        AS recipe_pass,
                    COUNT(*) FILTER (WHERE sli.depletion_reason
                        IN ('no_recipe','recipe_draft','recipe_skipped','invalid_recipe'))
                        AS recipe_fail,
                    COUNT(*) FILTER (WHERE sli.depletion_status = 'pending') AS pending_lines,
                    -- explicit conversion-stage evidence (subset of recipe_pass)
                    COUNT(*) FILTER (WHERE sli.depletion_status = 'depleted'
                        OR sli.depletion_reason = 'computation_error') AS conversion_pass,
                    COUNT(*) FILTER (WHERE sli.depletion_reason = 'missing_conversion')
                        AS missing_conversion,
                    -- reason breakdown
                    COUNT(*) FILTER (WHERE sli.depletion_reason
                        IN ('no_recipe','recipe_draft','recipe_skipped')) AS no_recipe,
                    COUNT(*) FILTER (WHERE sli.depletion_reason = 'invalid_recipe')
                        AS invalid_recipe,
                    COUNT(*) FILTER (WHERE sli.depletion_reason = 'computation_error')
                        AS computation_error
                  FROM sale_line_items sli
                  JOIN orders o ON o.id = sli.order_id
                 WHERE sli.tenant_id = :t AND o.closed_at >= :ws AND o.closed_at < :ao
                   AND {_ELIGIBLE_PREDICATE}
            """),  # noqa: S608 — _ELIGIBLE_PREDICATE is a hardcoded literal
                {"t": tid, "ws": window_start, "ao": now},
            )
        )
        .mappings()
        .one()
    )
    eligible = int(cov["eligible_lines"])
    eligible_rev = int(cov["eligible_rev"])
    depleted = int(cov["depleted_lines"])
    depleted_rev = int(cov["depleted_rev"])
    recipe_pass = int(cov["recipe_pass"])
    recipe_fail = int(cov["recipe_fail"])
    pending_lines = int(cov["pending_lines"])
    conversion_pass = int(cov["conversion_pass"])
    missing_conversion = int(cov["missing_conversion"])
    no_recipe = int(cov["no_recipe"])
    invalid_recipe = int(cov["invalid_recipe"])
    computation_error = int(cov["computation_error"])

    # Recipe stage (denominator = eligible).
    recipe_unknown = eligible - recipe_pass - recipe_fail - pending_lines
    # Historical window coverage (frozen sale outcomes) is kept SEPARATE from the
    # current catalog mapping (finding 3): fixing a recipe today changes future
    # sales, never these frozen historical rows.
    recipe_mapping = {
        "status": _stage_status(
            denominator=eligible,
            passed=recipe_pass,
            failed=recipe_fail,
            pending=pending_lines,
            unknown=recipe_unknown,
        ),
        "historical_window": {
            "eligible_sale_line_count": eligible,
            "with_recipe_count": recipe_pass,
            "coverage_pct": _pct(recipe_pass, eligible),
            "no_recipe_count": no_recipe,
            "invalid_recipe_count": invalid_recipe,
            "pending_count": pending_lines,
            "unknown_count": recipe_unknown,
            "note": "frozen sale outcomes — unchanged by later recipe fixes",
        },
    }
    # Conversion stage (denominator = recipe_pass; pending already excluded).
    conversion_unknown = recipe_pass - conversion_pass - missing_conversion
    conversion_coverage = {
        "status": _stage_status(
            denominator=recipe_pass,
            passed=conversion_pass,
            failed=missing_conversion,
            pending=0,
            unknown=conversion_unknown,
        ),
        "with_recipe_count": recipe_pass,
        "converted_count": conversion_pass,
        "coverage_pct": _pct(conversion_pass, recipe_pass),
        "missing_conversion_count": missing_conversion,
        "unknown_count": conversion_unknown,
    }
    # Depletion stage (denominator = conversion_pass).
    depletion_unknown = conversion_pass - depleted - computation_error
    depletion_execution = {
        "status": _stage_status(
            denominator=conversion_pass,
            passed=depleted,
            failed=computation_error,
            pending=0,
            unknown=depletion_unknown,
        ),
        "convertible_count": conversion_pass,
        "depleted_count": depleted,
        "coverage_pct": _pct(depleted, conversion_pass),
        "depletion_failure_count": computation_error,
        "unknown_count": depletion_unknown,
    }

    # End-to-end coverage. LINE coverage is authoritative; revenue coverage is
    # N/A when its denominator is non-positive or the ratio falls outside [0,100]
    # (fully-discounted / mixed-sign lines) — it never blocks 'complete' on its own.
    line_pct = _pct(depleted, eligible)
    rev_pct: str | None = None
    rev_applicable = eligible_rev > 0
    if rev_applicable:
        raw = _d(depleted_rev) / _d(eligible_rev) * 100
        if Decimal("0") <= raw <= Decimal("100"):
            rev_pct = str(raw.quantize(Decimal("0.1")))
    if line_pct is None:
        e2e_effective = None
    elif rev_pct is None:
        e2e_effective = line_pct  # revenue N/A → line authoritative
    else:
        e2e_effective = str(min(_d(line_pct), _d(rev_pct)))
    # Severity order (finding 1): a known failure is never hidden by pending —
    # failures > in_progress > complete/none/partial.
    total_failures = no_recipe + invalid_recipe + missing_conversion + computation_error
    end_to_end_coverage = {
        "scope": "tenant",
        "status": (
            "unavailable"
            if eligible == 0
            else "failures"
            if total_failures > 0
            else "in_progress"
            if pending_lines > 0
            else "complete"
            if (e2e_effective is not None and _d(e2e_effective) >= Decimal("100"))
            else "none"
            if depleted == 0
            else "partial"
        ),
        "failure_count": total_failures,
        "pending_line_count": pending_lines,
        "eligible_sale_line_count": eligible,
        "depleted_sale_line_count": depleted,
        "line_coverage_pct": line_pct,
        "eligible_net_revenue_cents": eligible_rev,
        "depleted_net_revenue_cents": depleted_rev,
        "revenue_coverage_pct": rev_pct,
        "revenue_coverage_applicable": rev_applicable and rev_pct is not None,
        "effective_coverage_pct": e2e_effective,  # forecast requires == 100
        "reason_breakdown": {
            "NO_RECIPE": no_recipe,
            "INVALID_RECIPE": invalid_recipe,
            "MISSING_CONVERSION": missing_conversion,
            "DEPLETION_FAILED": computation_error,
            "PROCESSING_PENDING": pending_lines,
        },
    }

    menu = (
        (
            await s.execute(
                text("""
                SELECT COUNT(*) FILTER (WHERE recipe_version_id IS NOT NULL) AS mapped,
                       COUNT(*) FILTER (WHERE recipe_version_id IS NULL) AS unmapped
                  FROM menu_items WHERE tenant_id = :t AND active = true
            """),
                {"t": tid},
            )
        )
        .mappings()
        .one()
    )
    recipe_mapping["current_catalog"] = {
        "menu_items_mapped": int(menu["mapped"]),
        "menu_items_unmapped": int(menu["unmapped"]),
        "note": "current catalog state — does not retroactively fix historical sales",
    }

    affected = await _affected_menu_items(s, tid, window_start)

    completeness = {
        "status": "unproven",
        "candidate_orders_complete_through": (
            _iso(conn["orders_complete_through"]) if conn is not None else None
        ),
        "trusted": False,
        "reason": {"code": "COMPLETENESS_UNPROVEN_PROVIDER_LIMITATION"},
    }

    # ── forecast eligibility (tenant-scoped): needs end_to_end == 100%, zero
    #    conversion/depletion failures, ZERO pending AND ZERO processing events
    #    (any age), and proven completeness (impossible in A1).
    blockers: list[dict[str, Any]] = []
    if not connected:
        blockers.append({"code": "POS_DISCONNECTED"})
    if pending > 0:
        blockers.append({"code": "PENDING_EVENTS", "count": pending})
    if processing > 0:
        blockers.append({"code": "PROCESSING_EVENTS", "count": processing})
    if failed > 0 or dead_letter > 0:
        blockers.append({"code": "FAILED_EVENTS", "count": failed + dead_letter})
    if proc_stalled:
        blockers.append({"code": "POS_PROCESSING_STALLED"})
    if pending_lines > 0:
        blockers.append({"code": "PENDING_SALE_LINES", "count": pending_lines})
    if e2e_effective is None or _d(e2e_effective) < Decimal("100"):
        blockers.append({"code": "END_TO_END_COVERAGE_INCOMPLETE", "effective_pct": e2e_effective})
    if missing_conversion > 0:
        blockers.append({"code": "CONVERSION_FAILURES", "count": missing_conversion})
    if computation_error > 0 or invalid_recipe > 0:
        blockers.append({"code": "DEPLETION_FAILURES", "count": computation_error + invalid_recipe})
    blockers.append({"code": "COMPLETENESS_UNPROVEN"})  # unconditional A1 gate
    forecast_eligibility = {"status": "blocked", "blockers": blockers}

    order_counts = (
        (
            await s.execute(
                text(f"""
                SELECT
                    COUNT(DISTINCT o.id) AS orders_seen,
                    COUNT(DISTINCT o.id) FILTER (WHERE {_ELIGIBLE_PREDICATE}) AS eligible_orders
                  FROM orders o
                  LEFT JOIN sale_line_items sli ON sli.order_id = o.id
                 WHERE o.tenant_id = :t AND o.closed_at >= :ws AND o.closed_at < :ao
            """),  # noqa: S608 — _ELIGIBLE_PREDICATE is a hardcoded literal
                {"t": tid, "ws": window_start, "ao": now},
            )
        )
        .mappings()
        .one()
    )

    return {
        "provider": provider,
        # This whole section is TENANT pipeline health, NOT ingredient-level
        # completeness for the selected item (finding 4).
        "scope": "tenant",
        "orders_seen_in_window": int(order_counts["orders_seen"]),
        "eligible_orders_in_window": int(order_counts["eligible_orders"]),
        "coverage_denominator": "eligible_sale_lines",
        "dimensions": {
            "connection": connection,
            "event_activity": event_activity,
            "reconciliation_health": reconciliation_health,
            "processing": processing_dim,
            "recipe_mapping": recipe_mapping,
            "conversion_coverage": conversion_coverage,
            "depletion_execution": depletion_execution,
            "end_to_end_coverage": end_to_end_coverage,
            "affected_menu_items": affected,
            "completeness": completeness,
            "forecast_eligibility": forecast_eligibility,
        },
    }


async def _affected_menu_items(
    s: AsyncSession, tid: UUID, window_start: datetime
) -> dict[str, Any]:
    """Eligible sale lines that did NOT deplete, grouped by (menu_item_id, name)
    so distinct items with the same name never collapse. Tenant-predicated join.
    Limits IN SQL (LIMIT n+1) and reports true group count separately. Never
    attributes an ingredient to an unmapped item."""
    ao = _AS_OF.get()
    params = {"t": tid, "ws": window_start, "ao": ao}
    total = (
        await s.execute(
            text(f"SELECT COUNT(*) FROM ({_AFFECTED_GROUPED_SQL}) g"),  # noqa: S608 — literal
            params,
        )
    ).scalar_one()
    rows = (
        (
            await s.execute(
                text(f"{_AFFECTED_GROUPED_SQL} ORDER BY revenue DESC LIMIT :lim"),
                {**params, "lim": _AFFECTED_LIMIT},
            )
        )
        .mappings()
        .all()
    )
    items = []
    for r in rows:
        if r["status"] == "pending":
            code = "PROCESSING_PENDING"
        elif r["reason"] is None or r["reason"] not in _REASON_CODE:
            code = "UNKNOWN_FAILURE"  # never silently a depletion failure
        else:
            code = _REASON_CODE[r["reason"]]
        items.append(
            {
                "menu_item_id": str(r["menu_item_id"]) if r["menu_item_id"] else None,
                "menu_item": r["name"],
                "affected_sale_line_count": int(r["lines"]),
                "units_sold": _s(_d(r["sold"])),
                "revenue_cents": int(r["revenue"]),
                "reason_code": code,
                "repair": _REPAIR.get(code),
            }
        )
    return {
        "scope": "tenant",
        "items": items,
        "total_count": int(total),
        "has_more": int(total) > _AFFECTED_LIMIT,
    }


async def _consumption(
    s: AsyncSession, tid: UUID, iid: UUID, mode: str, window_start: datetime, tz: str
) -> dict[str, Any]:
    """Observed daily consumption in storage units, bucketed by local day.

    Mode A: sale_depletion (+ reversal); Mode B: sale_signal (+ reversal). Days are
    OBSERVED, not completeness-certified (watermark null) — labeled accordingly.
    """
    dep_types = (
        ("sale_depletion", "sale_depletion_reversal")
        if mode == "recipe_deducted"
        else ("sale_signal", "sale_signal_reversal")
    )
    rows = (
        (
            await s.execute(
                text("""
                SELECT timezone(:tz, m.created_at)::date AS d,
                       COALESCE(SUM(-m.delta), 0) AS consumed,
                       COUNT(DISTINCT o.id) AS orders,
                       COUNT(DISTINCT sli.id) AS mapped_lines
                  FROM inventory_movements m
                  LEFT JOIN sale_line_items sli
                         ON sli.id = m.source_id AND m.source_type = 'sale_line_item'
                  LEFT JOIN orders o ON o.id = sli.order_id
                 WHERE m.tenant_id = :t AND m.inventory_item_id = :i
                   AND m.movement_type IN :types
                   AND m.created_at >= :ws AND m.created_at < :ao
                 GROUP BY 1 ORDER BY 1
            """).bindparams(bindparam("types", expanding=True)),
                {
                    "t": tid,
                    "i": iid,
                    "types": list(dep_types),
                    "ws": window_start,
                    "ao": _AS_OF.get(),
                    "tz": tz,
                },
            )
        )
        .mappings()
        .all()
    )

    today_local = (
        await s.execute(text("SELECT timezone(:tz, :ao)::date"), {"ao": _AS_OF.get(), "tz": tz})
    ).scalar_one()

    daily = []
    total = Decimal("0")
    observed_days = 0
    peak = Decimal("0")
    for r in rows:
        consumed = _d(r["consumed"])
        is_partial = r["d"] == today_local
        daily.append(
            {
                "date": r["d"].isoformat(),
                "consumed": _s(consumed),
                "orders": int(r["orders"]),
                "mapped_sale_lines": int(r["mapped_lines"]),
                "is_partial": is_partial,
            }
        )
        if not is_partial:
            total += consumed
            observed_days += 1
            if consumed > peak:
                peak = consumed

    avg_observed = (
        _s((total / observed_days).quantize(Decimal("0.0001"))) if observed_days else None
    )
    return {
        "bucket_timezone": tz,
        "completeness": "uncertified",
        "completeness_reason": {"code": "NOT_YET_CERTIFIED"},
        "daily": daily,
        "summary": {
            "basis": "observed_day",
            "avg_per_observed_day": avg_observed,
            "peak_observed_day": _s(peak) if observed_days else None,
            "observed_days": observed_days,
            "trend": {"available": False, "state": "NOT_YET_CERTIFIED"},
        },
        # `confidence` is the SINGLE source of truth for how complete this
        # consumption is (finding 5) — set by the assembler once it knows coverage.
        "confidence": None,
    }


async def _contributors(
    s: AsyncSession, tid: UUID, iid: UUID, mode: str, window_start: datetime
) -> list[dict[str, Any]]:
    dep_type = "sale_depletion" if mode == "recipe_deducted" else "sale_signal"
    rows = (
        (
            await s.execute(
                text("""
                SELECT mi.id AS menu_item_id, mi.name AS menu_item, rv.version_number AS rv,
                       COALESCE(SUM(sli.quantity), 0) AS units_sold,
                       COALESCE(SUM(-m.delta), 0) AS consumed
                  FROM inventory_movements m
                  JOIN sale_line_items sli ON sli.id = m.source_id
                  JOIN orders o ON o.id = sli.order_id
                  LEFT JOIN menu_items mi ON mi.id = sli.menu_item_id
                  LEFT JOIN recipe_versions rv ON rv.id = sli.recipe_version_id
                 WHERE m.tenant_id = :t AND m.inventory_item_id = :i
                   AND m.source_type = 'sale_line_item' AND m.movement_type = :dt
                   AND m.created_at >= :ws AND m.created_at < :ao
                 GROUP BY mi.id, mi.name, rv.version_number
                 ORDER BY consumed DESC
            """),
                {"t": tid, "i": iid, "dt": dep_type, "ws": window_start, "ao": _AS_OF.get()},
            )
        )
        .mappings()
        .all()
    )

    grand = sum((_d(r["consumed"]) for r in rows), Decimal("0"))
    # Group by menu item, then per frozen recipe version.
    by_item: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = str(r["menu_item_id"]) if r["menu_item_id"] else "unmapped"
        consumed = _d(r["consumed"])
        units = _d(r["units_sold"])
        entry = by_item.setdefault(
            key,
            {
                "menu_item_id": str(r["menu_item_id"]) if r["menu_item_id"] else None,
                "menu_item": r["menu_item"] or "Unmapped sales",
                "mapping": "confirmed" if r["menu_item_id"] else "unmapped",
                "total_consumed": Decimal("0"),
                "groups": [],
            },
        )
        entry["total_consumed"] += consumed
        qty_per_sale = (consumed / units).quantize(Decimal("0.0001")) if units else None
        entry["groups"].append(
            {
                "recipe_version": int(r["rv"]) if r["rv"] is not None else None,
                "units_sold": _s(units),
                "qty_per_sale": _s(qty_per_sale),
                "consumed": _s(consumed),
                "explained": r["rv"] is not None,
            }
        )

    out = []
    for entry in sorted(by_item.values(), key=lambda e: e["total_consumed"], reverse=True):
        share = (
            (entry["total_consumed"] / grand * 100).quantize(Decimal("0.1"))
            if grand
            else Decimal("0")
        )
        out.append(
            {
                "menu_item_id": entry["menu_item_id"],
                "menu_item": entry["menu_item"],
                "mapping": entry["mapping"],
                "total_consumed": _s(entry["total_consumed"]),
                "share_pct": str(share),
                "groups": entry["groups"],
            }
        )
    return out


async def _reasons(
    s: AsyncSession,
    tid: UUID,
    iid: UUID,
    window_start: datetime,
    pos: dict[str, Any],
    contributors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deterministic, traceable reasons. Completeness-independent only in PR-A1
    (trend-based reasons are deferred to A2)."""
    reasons: list[dict[str, Any]] = []

    if contributors and contributors[0]["mapping"] == "confirmed":
        top = contributors[0]
        g = top["groups"][0]
        reasons.append(
            {
                "code": "TOP_MENU_CONSUMER",
                "menu_item": top["menu_item"],
                "menu_item_id": top["menu_item_id"],
                "units_sold": g["units_sold"],
                "qty_per_sale": g["qty_per_sale"],
                "total_consumed": top["total_consumed"],
                "source": "sales",
                "drill": {"type": "recipe", "destination": "/onboarding/recipes"},
            }
        )

    last_receive = (
        await s.execute(
            text("""
                SELECT MAX(created_at) FROM inventory_movements
                 WHERE tenant_id = :t AND inventory_item_id = :i AND movement_type = 'receive'
            """),
            {"t": tid, "i": iid},
        )
    ).scalar_one()
    if last_receive is not None:
        days_since = (_AS_OF.get() - last_receive).days
        if days_since >= 7:
            reasons.append(
                {
                    "code": "NO_RECENT_RECEIPT",
                    "days_since": days_since,
                    "source": "receipts",
                    "drill": None,
                }
            )

    adj = (
        (
            await s.execute(
                text("""
                SELECT id, delta, created_at FROM inventory_movements
                 WHERE tenant_id = :t AND inventory_item_id = :i
                   AND movement_type = 'count_adjust' AND created_at >= :ws AND created_at < :ao
                 ORDER BY created_at DESC LIMIT 1
            """),
                {"t": tid, "i": iid, "ws": window_start, "ao": _AS_OF.get()},
            )
        )
        .mappings()
        .fetchone()
    )
    if adj is not None:
        reasons.append(
            {
                "code": "MANUAL_ADJUSTMENT",
                "delta": str(_d(adj["delta"])),
                "at": adj["created_at"].isoformat(),
                "source": "count_event",
                "drill": None,
            }
        )

    dims = pos["dimensions"]
    recipe = dims["recipe_mapping"]
    processing = dims["processing"]
    connection = dims["connection"]
    e2e = dims["end_to_end_coverage"]
    eff = e2e["effective_coverage_pct"]
    if eff is not None and _d(eff) < 100:
        reasons.append(
            {
                "code": "UNMAPPED_SOLD_ITEMS",
                "unmapped_menu_items": recipe["current_catalog"]["menu_items_unmapped"],
                "effective_coverage_pct": eff,
                "source": "pos",
                "drill": {"type": "recipe", "destination": "/onboarding/recipes"},
            }
        )
    if connection["status"] != "connected":
        reasons.append({"code": "POS_DISCONNECTED", "source": "pos", "drill": None})
    if processing["pending_event_count"] > 0:
        reasons.append(
            {
                "code": "POS_BACKLOG",
                "pending_event_count": processing["pending_event_count"],
                "oldest_pending_at": processing["oldest_pending_at"],
                "source": "pos",
                "drill": None,
            }
        )
    dep_fail = dims["depletion_execution"]["depletion_failure_count"]
    if dep_fail > 0:
        reasons.append(
            {
                "code": "DEPLETION_FAILURES",
                "depletion_failure_count": dep_fail,
                "source": "pos",
                "drill": None,
            }
        )
    return reasons


async def _cost(s: AsyncSession, tid: UUID, iid: UUID, can_view_aggregated: bool) -> dict[str, Any]:
    """RBAC: the LATEST unit cost is visible to everyone (staff need it for
    inventory operations). Supplier identity and cost HISTORY are manager+ only.
    Estimated purchase cost is not produced in A1 (reorder is NOT_YET_CERTIFIED)."""
    row = (
        (
            await s.execute(
                text("""
                SELECT cs.unit_cost_cents_exact, cs.effective_from,
                       r.supplier_name, r.id AS receipt_id
                  FROM ingredient_cost_snapshots cs
                  LEFT JOIN receipt_lines rl ON rl.id = cs.source_receipt_line_id
                  LEFT JOIN receipts r ON r.id = rl.receipt_id
                 WHERE cs.tenant_id = :t AND cs.inventory_item_id = :i
                 ORDER BY cs.effective_from DESC LIMIT 1
            """),
                {"t": tid, "i": iid},
            )
        )
        .mappings()
        .fetchone()
    )
    if row is None:
        return {"available": False, "reason": {"code": "NO_COST_HISTORY"}}
    result: dict[str, Any] = {
        "available": True,
        # Cost exact keeps its full DB precision (e.g. "120.0000") — NOT trimmed;
        # it is the 4-dp costing signal, unlike storage quantities. Visible to all.
        "latest_unit_cost_cents_exact": (
            str(row["unit_cost_cents_exact"]) if row["unit_cost_cents_exact"] is not None else None
        ),
        "as_of": _iso(row["effective_from"]),
    }
    if not can_view_aggregated:
        # Staff: latest cost only. Supplier identity + history are manager+.
        result["aggregated"] = {"available": False, "reason": {"code": "MANAGER_ONLY"}}
        return result
    history = (
        (
            await s.execute(
                text("""
                SELECT cs.unit_cost_cents_exact, cs.effective_from, r.id AS receipt_id
                  FROM ingredient_cost_snapshots cs
                  LEFT JOIN receipt_lines rl ON rl.id = cs.source_receipt_line_id
                  LEFT JOIN receipts r ON r.id = rl.receipt_id
                 WHERE cs.tenant_id = :t AND cs.inventory_item_id = :i
                 ORDER BY cs.effective_from DESC LIMIT 20
            """),
                {"t": tid, "i": iid},
            )
        )
        .mappings()
        .all()
    )
    result["aggregated"] = {
        "available": True,
        "supplier_name": row["supplier_name"],
        "receipt_id": str(row["receipt_id"]) if row["receipt_id"] else None,
        "history": [
            {
                "unit_cost_cents_exact": (
                    str(h["unit_cost_cents_exact"])
                    if h["unit_cost_cents_exact"] is not None
                    else None
                ),
                "effective_from": _iso(h["effective_from"]),
                "receipt_id": str(h["receipt_id"]) if h["receipt_id"] else None,
            }
            for h in history
        ],
    }
    return result


def _status(item: _Item, on_hand: Decimal | None) -> tuple[str, list[dict[str, Any]]]:
    """Deterministic status. 'critical' requires forecast (days_of_cover) which is
    NOT_YET_CERTIFIED in PR-A1, so it never fires here."""
    reasons: list[dict[str, Any]] = []
    if on_hand is None:
        return "unknown", [{"code": "ON_HAND_UNKNOWN"}]
    if on_hand <= 0:
        return "out", [{"code": "OUT_OF_STOCK", "on_hand": _s(on_hand)}]
    if item.par_level is not None and on_hand <= item.par_level:
        return "low", [
            {"code": "AT_OR_BELOW_PAR", "on_hand": _s(on_hand), "par_level": _s(item.par_level)}
        ]
    reasons.append({"code": "ABOVE_PAR", "on_hand": _s(on_hand), "par_level": _s(item.par_level)})
    return "ok", reasons


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


# as_of is threaded via a ContextVar so every helper reads the one snapshot bound
# (each request is its own asyncio task with a copied context — no cross-leak).
_AS_OF: ContextVar[datetime] = ContextVar("_AS_OF")


async def build_item_insights(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    item_id: UUID,
    window_key: str,
    as_of: datetime,
    timezone_name: str,
    timezone_source: str,
    can_view_aggregated_cost: bool,
    target_cover_days: int,
    target_source: str,
) -> dict[str, Any]:
    """Assemble the PR-A1 insights payload. Caller owns the REPEATABLE READ txn
    and passes the DB-derived as_of. Forecast + reorder return NOT_YET_CERTIFIED.

    can_view_aggregated_cost: staff always see the latest unit cost; this gates
    ONLY supplier identity + cost history (manager+)."""
    _AS_OF.set(as_of)
    days = WINDOW_DAYS[window_key]
    window_start = as_of - timedelta(days=days)

    item = await _load_item(session, tenant_id, item_id)
    on_hand = await current_balance(session, tenant_id=tenant_id, inventory_item_id=item_id)
    last_movement = (
        await session.execute(
            text(
                "SELECT MAX(created_at) FROM inventory_movements "
                "WHERE tenant_id=:t AND inventory_item_id=:i"
            ),
            {"t": tenant_id, "i": item_id},
        )
    ).scalar_one()

    status, status_reasons = _status(item, on_hand)
    pct_of_par: str | None = None
    if on_hand is not None and item.par_level is not None and item.par_level != Decimal("0"):
        pct_of_par = str((on_hand / item.par_level * 100).quantize(Decimal("0.1")))

    ledger = await _ledger(session, tenant_id, item_id, item, window_start, on_hand)
    pos = await _pos_diagnostics(session, tenant_id, window_start)
    consumption = await _consumption(
        session, tenant_id, item_id, item.inventory_mode, window_start, timezone_name
    )
    # ONE consumption confidence object. It is a TENANT-WIDE PROXY (finding 4):
    # tenant end-to-end coverage bounds this item's completeness from below but
    # can never PROVE this ingredient's completeness — so scope is tenant_proxy
    # and ingredient_level_completeness stays 'unproven'. Because provider
    # completeness is unproven in A1, observed consumption is ALWAYS a floor.
    eff = pos["dimensions"]["end_to_end_coverage"]["effective_coverage_pct"]
    conf_reasons = [{"code": "PROVIDER_COMPLETENESS_UNPROVEN"}]
    if eff is None or _d(eff) < Decimal("100"):
        conf_reasons.append({"code": "TENANT_COVERAGE_INCOMPLETE", "effective_coverage_pct": eff})
    consumption["confidence"] = {
        "status": "floor",  # unavailable only when there is nothing observed at all
        "scope": "tenant_proxy",
        "ingredient_level_completeness": "unproven",
        "effective_coverage_pct": eff,
        "reasons": conf_reasons,
    }
    if not consumption["daily"]:
        consumption["confidence"]["status"] = "unavailable"
    contributors = await _contributors(
        session, tenant_id, item_id, item.inventory_mode, window_start
    )
    reasons = await _reasons(session, tenant_id, item_id, window_start, pos, contributors)
    cost = await _cost(session, tenant_id, item_id, can_view_aggregated_cost)

    return {
        "snapshot": {"as_of": as_of.isoformat(), "isolation": "repeatable_read"},
        "window": {
            "key": window_key,
            "from": window_start.isoformat(),
            "to": as_of.isoformat(),
            "bucket_timezone": timezone_name,
            "timezone_source": timezone_source,
        },
        "item": {
            "id": str(item_id),
            "name": item.name,
            "storage_unit": item.storage_unit,
            "inventory_mode": item.inventory_mode,
            "on_hand": _s(on_hand),
            "par_level": _s(item.par_level),
            "pct_of_par": pct_of_par,
            "status": status,
            "status_reasons": status_reasons,
            "last_movement_at": _iso(last_movement),
        },
        "ledger": ledger,
        "pos": pos,
        "consumption": consumption,
        "contributors": contributors,
        "reasons": reasons,
        "forecast": {
            "available": False,
            "state": "NOT_YET_CERTIFIED",
            "blockers": [{"code": "NOT_YET_CERTIFIED"}],
        },
        "reorder": {
            "mode": "unavailable",
            "state": "NOT_YET_CERTIFIED",
            "target_cover_days": target_cover_days,
            "target_source": target_source,
            "blockers": [{"code": "NOT_YET_CERTIFIED"}],
        },
        "cost": cost,
    }
