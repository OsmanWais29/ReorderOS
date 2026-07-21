"""Stock Item Insights — PR-A1 (actuals only).

Every number here derives from inventory_movements / on_hand() / POS ingest
state. No LLM, no fabrication. Forecast, days-of-cover, trend, and reorder are
NOT built here — they return NOT_YET_CERTIFIED, gated on the completeness
watermark (which cannot advance yet; see balance_projection + migration 0034).

Sections produced: item state, ledger reconciliation (Mode A; Mode B reports
RECONCILIATION_UNAVAILABLE), POS diagnostics (eight INDEPENDENT dimensions —
never one 'healthy' boolean), observed consumption (floor-labeled below full
coverage), mapping quality (reason breakdown + affected menu items), menu-item
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


# Freshness/backlog thresholds (named constants, not magic numbers). Processing
# lease TTL mirrors the POS worker's CLAIM_TTL_SECONDS=300.
_INGEST_STALE_HOURS = 24
_PROCESSING_LEASE_SECONDS = 300
_PENDING_BACKLOG_SECONDS = 900  # a pending event older than this = a real backlog

# Eligible sale line = one the production depletion rules SHOULD have depleted:
# order locked + payment PAID/PARTIALLY_REFUNDED, line not voided, not refunded.
# This is the denominator for every mapping ratio — only genuinely ineligible
# lines are excluded.
_ELIGIBLE_PREDICATE = (
    "o.state = 'locked' AND o.payment_state IN ('PAID','PARTIALLY_REFUNDED') "
    "AND sli.is_voided = false AND sli.is_refunded = false"
)


def _pct(num: int, den: int) -> str | None:
    return None if den == 0 else str((_d(num) / _d(den) * 100).quantize(Decimal("0.1")))


async def _pos_diagnostics(s: AsyncSession, tid: UUID, window_start: datetime) -> dict[str, Any]:
    """POS health as INDEPENDENT dimensions — never one 'healthy' boolean. A
    connected POS can be processing-stale, mapping-incomplete, or completeness-
    unproven all at once, and each is reported separately with its evidence."""
    now = _AS_OF.get()
    conn = (
        (
            await s.execute(
                text("""
                SELECT vendor, state, last_reconciliation_at, orders_complete_through
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
        conn_state, provider = None, None
    else:
        conn_state, provider = conn["state"], conn["vendor"]
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

    # ── ingestion + processing (inbox) ──
    inbox = (
        (
            await s.execute(
                text("""
                SELECT
                    COUNT(*) FILTER (WHERE state = 'pending') AS pending,
                    COUNT(*) FILTER (WHERE state = 'processing') AS processing,
                    COUNT(*) FILTER (WHERE state = 'failed') AS failed,
                    COUNT(*) FILTER (WHERE state = 'dead_letter') AS dead_letter,
                    MIN(received_at) FILTER (WHERE state = 'pending') AS oldest_pending,
                    MIN(processing_started_at)
                        FILTER (WHERE state = 'processing') AS oldest_processing,
                    MAX(received_at) FILTER (WHERE vendor_object_type = 'O') AS last_received,
                    MAX(processed_at) AS last_processed
                  FROM pos_event_inbox
                 WHERE tenant_id = :t
            """),
                {"t": tid},
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

    if not connected or last_received is None:
        ingest_status = "unavailable"
    elif (now - last_received).total_seconds() > _INGEST_STALE_HOURS * 3600:
        ingest_status = "stale"
    else:
        ingest_status = "current"
    ingestion = {
        "status": ingest_status,
        "latest_sales_data_received_at": _iso(last_received),
        "latest_background_reconciliation_check_at": (
            _iso(conn["last_reconciliation_at"]) if conn is not None else None
        ),
    }

    proc_stalled = (
        oldest_processing is not None
        and (now - oldest_processing).total_seconds() > _PROCESSING_LEASE_SECONDS
    )
    backlogged = (
        oldest_pending is not None
        and (now - oldest_pending).total_seconds() > _PENDING_BACKLOG_SECONDS
    )
    if not connected:
        proc_status = "unavailable"
    elif proc_stalled:
        proc_status = "stalled"
    elif backlogged or failed > 0 or dead_letter > 0:
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
    }

    # ── mapping (denominator = eligible sale lines) + reason breakdown ──
    cov = (
        (
            await s.execute(
                text(f"""
                SELECT
                    COUNT(*) AS eligible_lines,
                    COUNT(*) FILTER (WHERE sli.depletion_status = 'depleted') AS mapped_lines,
                    COALESCE(SUM(sli.net_revenue_cents), 0) AS eligible_rev,
                    COALESCE(SUM(sli.net_revenue_cents)
                             FILTER (WHERE sli.depletion_status = 'depleted'), 0) AS mapped_rev,
                    COUNT(*) FILTER (WHERE sli.depletion_reason
                        IN ('no_recipe','recipe_draft','recipe_skipped')) AS no_recipe,
                    COUNT(*) FILTER (WHERE sli.depletion_reason = 'invalid_recipe')
                        AS invalid_recipe,
                    COUNT(*) FILTER (WHERE sli.depletion_reason = 'missing_conversion')
                        AS missing_conversion,
                    COUNT(*) FILTER (WHERE sli.depletion_reason = 'computation_error')
                        AS depletion_failed,
                    COUNT(*) FILTER (WHERE sli.depletion_status = 'pending') AS processing_pending
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
    eligible_lines = int(cov["eligible_lines"])
    mapped_lines = int(cov["mapped_lines"])
    eligible_rev = int(cov["eligible_rev"])
    mapped_rev = int(cov["mapped_rev"])
    sale_line_pct = _pct(mapped_lines, eligible_lines)
    revenue_pct = _pct(mapped_rev, eligible_rev)
    eff = None
    if sale_line_pct is not None and revenue_pct is not None:
        eff = str(min(_d(sale_line_pct), _d(revenue_pct)))
    missing_conversion = int(cov["missing_conversion"])
    depletion_failed = int(cov["depletion_failed"])
    no_recipe = int(cov["no_recipe"])
    invalid_recipe = int(cov["invalid_recipe"])
    processing_pending = int(cov["processing_pending"])

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

    if eligible_lines == 0:
        map_status = "unavailable"
    elif eff is not None and _d(eff) >= Decimal("100"):
        map_status = "complete"
    elif mapped_lines == 0:
        map_status = "none"
    else:
        map_status = "partial"
    mapping = {
        "status": map_status,
        "eligible_sale_line_count": eligible_lines,
        "mapped_sale_line_count": mapped_lines,
        "sale_line_mapping_pct": sale_line_pct,
        "eligible_net_revenue_cents": eligible_rev,
        "mapped_net_revenue_cents": mapped_rev,
        "revenue_mapping_pct": revenue_pct,
        "effective_mapping_pct": eff,
        "menu_items_mapped": int(menu["mapped"]),
        "menu_items_unmapped": int(menu["unmapped"]),
        "reason_breakdown": {
            "NO_RECIPE": no_recipe,
            "INVALID_RECIPE": invalid_recipe,
            "MISSING_CONVERSION": missing_conversion,
            "DEPLETION_FAILED": depletion_failed,
            "PROCESSING_PENDING": processing_pending,
        },
        "affected_menu_items": await _affected_menu_items(s, tid, window_start),
    }

    conversion = {
        "status": "ok" if missing_conversion == 0 else "failures",
        "missing_conversion_count": missing_conversion,
    }
    depletion = {
        "status": "ok" if (depletion_failed == 0 and invalid_recipe == 0) else "failures",
        "depletion_failure_count": depletion_failed,
        "missing_recipe_count": no_recipe,
        "invalid_recipe_count": invalid_recipe,
    }

    # ── completeness: ALWAYS unproven in PR-A1 (watermark cannot advance) ──
    completeness = {
        "status": "unproven",
        "orders_complete_through": (
            _iso(conn["orders_complete_through"]) if conn is not None else None
        ),
        "reason": {"code": "COMPLETENESS_UNPROVEN_PROVIDER_LIMITATION"},
    }

    # ── forecast eligibility: exact predictions need 100% eligible-line mapping,
    #    zero conversion failures, zero depletion failures, AND proven
    #    completeness. Completeness is unprovable in A1, so this is ALWAYS blocked
    #    — but we report every failing gate truthfully, not just the umbrella one.
    blockers: list[dict[str, Any]] = []
    if not connected:
        blockers.append({"code": "POS_DISCONNECTED"})
    if proc_status in ("stalled", "backlogged"):
        blockers.append({"code": "POS_PROCESSING_" + proc_status.upper()})
    if eff is None or _d(eff) < Decimal("100"):
        blockers.append({"code": "MAPPING_INCOMPLETE", "effective_mapping_pct": eff})
    if missing_conversion > 0:
        blockers.append({"code": "CONVERSION_FAILURES", "count": missing_conversion})
    if depletion_failed > 0 or invalid_recipe > 0:
        blockers.append({"code": "DEPLETION_FAILURES", "count": depletion_failed + invalid_recipe})
    blockers.append({"code": "COMPLETENESS_UNPROVEN"})  # the always-present A1 gate
    forecast_eligibility = {"status": "blocked", "blockers": blockers}

    orders_in_window = (
        await s.execute(
            text(
                "SELECT COUNT(*) FROM orders "
                "WHERE tenant_id = :t AND closed_at >= :ws AND closed_at < :ao"
            ),
            {"t": tid, "ws": window_start, "ao": now},
        )
    ).scalar_one()

    return {
        "provider": provider,
        "orders_in_window": int(orders_in_window),
        "dimensions": {
            "connection": connection,
            "ingestion": ingestion,
            "processing": processing_dim,
            "mapping": mapping,
            "conversion": conversion,
            "depletion": depletion,
            "completeness": completeness,
            "forecast_eligibility": forecast_eligibility,
        },
    }


async def _affected_menu_items(
    s: AsyncSession, tid: UUID, window_start: datetime
) -> list[dict[str, Any]]:
    """Eligible sale lines that did NOT deplete, grouped by menu item + reason, with
    a tenant-safe repair destination. Never claims an unmapped item contains a
    specific ingredient — this is a mapping diagnostic, not ingredient attribution."""
    rows = (
        (
            await s.execute(
                text(f"""
                SELECT COALESCE(mi.name, sli.name_at_sale) AS name,
                       sli.depletion_reason AS reason,
                       sli.depletion_status AS status,
                       COALESCE(SUM(sli.quantity), 0) AS sold,
                       COALESCE(SUM(sli.net_revenue_cents), 0) AS revenue
                  FROM sale_line_items sli
                  JOIN orders o ON o.id = sli.order_id
                  LEFT JOIN menu_items mi ON mi.id = sli.menu_item_id
                 WHERE sli.tenant_id = :t AND o.closed_at >= :ws AND o.closed_at < :ao
                   AND {_ELIGIBLE_PREDICATE}
                   AND sli.depletion_status <> 'depleted'
                 GROUP BY COALESCE(mi.name, sli.name_at_sale),
                          sli.depletion_reason, sli.depletion_status
                 ORDER BY revenue DESC
                 LIMIT 20
            """),  # noqa: S608 — _ELIGIBLE_PREDICATE is a hardcoded literal
                {"t": tid, "ws": window_start, "ao": _AS_OF.get()},
            )
        )
        .mappings()
        .all()
    )
    _REASON_CODE = {
        "no_recipe": "NO_RECIPE",
        "recipe_draft": "NO_RECIPE",
        "recipe_skipped": "NO_RECIPE",
        "invalid_recipe": "INVALID_RECIPE",
        "missing_conversion": "MISSING_CONVERSION",
        "computation_error": "DEPLETION_FAILED",
    }
    _REPAIR = {
        "NO_RECIPE": "fix_recipe",
        "INVALID_RECIPE": "fix_recipe",
        "MISSING_CONVERSION": "fix_unit_conversion",
        "DEPLETION_FAILED": None,
    }
    out = []
    for r in rows:
        if r["status"] == "pending":
            code = "PROCESSING_PENDING"
        else:
            code = _REASON_CODE.get(r["reason"] or "", "DEPLETION_FAILED")
        out.append(
            {
                "menu_item": r["name"],
                "units_sold": _s(_d(r["sold"])),
                "revenue_cents": int(r["revenue"]),
                "reason_code": code,
                "repair": _REPAIR.get(code),
            }
        )
    return out


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
            "floor": True,
            "trend": {"available": False, "state": "NOT_YET_CERTIFIED"},
        },
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

    mapping = pos["dimensions"]["mapping"]
    processing = pos["dimensions"]["processing"]
    connection = pos["dimensions"]["connection"]
    eff = mapping["effective_mapping_pct"]
    if eff is not None and _d(eff) < 100:
        reasons.append(
            {
                "code": "UNMAPPED_SOLD_ITEMS",
                "unmapped_menu_items": mapping["menu_items_unmapped"],
                "effective_mapping_pct": eff,
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
    if pos["dimensions"]["depletion"]["depletion_failure_count"] > 0:
        reasons.append(
            {
                "code": "DEPLETION_FAILURES",
                "depletion_failure_count": pos["dimensions"]["depletion"][
                    "depletion_failure_count"
                ],
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
    # Consumption floor honesty: label observed usage as a MINIMUM when coverage
    # is below 100% (unmapped sales may include this ingredient).
    eff = pos["dimensions"]["mapping"]["effective_mapping_pct"]
    consumption["coverage"] = {
        "effective_mapping_pct": eff,
        "is_floor": eff is None or _d(eff) < Decimal("100"),
        "note_code": "OBSERVED_USAGE_IS_MINIMUM_UNMAPPED_SALES",
    }
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
