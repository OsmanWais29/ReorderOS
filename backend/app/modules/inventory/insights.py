"""Stock Item Insights — PR-A1 (actuals only).

Every number here derives from inventory_movements / on_hand() / POS ingest
state. No LLM, no fabrication. Forecast, days-of-cover, trend, and reorder are
NOT built here — they return NOT_YET_CERTIFIED, gated on the completeness
watermark (which cannot advance yet; see balance_projection + migration 0034).

Sections produced: item state, ledger reconciliation (Mode A; Mode B reports
RECONCILIATION_UNAVAILABLE), POS health, observed consumption, mapping coverage,
menu-item contributors, deterministic reasons, cost (manager+). RBAC redaction is
applied by the caller-provided `can_view_cost` flag.

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


async def _pos_health(s: AsyncSession, tid: UUID, window_start: datetime) -> dict[str, Any]:
    conn = (
        (
            await s.execute(
                text("""
                SELECT vendor, state, last_reconciliation_at, initial_sync_completed_at,
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

    if conn is None:
        return {
            "provider": None,
            "connected": False,
            "state": "not_connected",
            "last_order_event_received_at": None,
            "last_order_event_processed_at": None,
            "last_background_check_at": None,
            "orders_complete_through": None,
            "pending_event_count": 0,
            "processing_event_count": 0,
            "failed_event_count": 0,
            "dead_letter_event_count": 0,
            "oldest_pending_at": None,
            "oldest_processing_at": None,
            "depletion_failure_count": 0,
            "missing_conversion_count": 0,
            "orders_in_window": 0,
            "menu_items_mapped": 0,
            "menu_items_unmapped": 0,
            "sale_line_mapping_pct": None,
            "revenue_mapping_pct": None,
            "effective_mapping_pct": None,
            "freshness": "unavailable",
            "forecast_ready": False,
            "forecast_blockers": [{"code": "POS_DISCONNECTED"}],
        }

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

    orders_in_window = (
        await s.execute(
            text(
                "SELECT COUNT(*) FROM orders "
                "WHERE tenant_id = :t AND closed_at >= :ws AND closed_at < :ao"
            ),
            {"t": tid, "ws": window_start, "ao": _AS_OF.get()},
        )
    ).scalar_one()

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

    cov = (
        (
            await s.execute(
                text("""
                SELECT
                    COUNT(*) AS total_lines,
                    COUNT(*) FILTER (WHERE sli.depletion_status = 'depleted') AS depleted_lines,
                    COALESCE(SUM(sli.net_revenue_cents), 0) AS total_rev,
                    COALESCE(SUM(sli.net_revenue_cents)
                             FILTER (WHERE sli.depletion_status = 'depleted'), 0) AS depleted_rev
                  FROM sale_line_items sli
                  JOIN orders o ON o.id = sli.order_id
                 WHERE sli.tenant_id = :t AND o.closed_at >= :ws AND o.closed_at < :ao
                   AND sli.is_refunded = false AND sli.is_voided = false
            """),
                {"t": tid, "ws": window_start, "ao": _AS_OF.get()},
            )
        )
        .mappings()
        .one()
    )

    def pct(num: int, den: int) -> str | None:
        return None if den == 0 else str((_d(num) / _d(den) * 100).quantize(Decimal("0.1")))

    sale_line_pct = pct(int(cov["depleted_lines"]), int(cov["total_lines"]))
    revenue_pct = pct(int(cov["depleted_rev"]), int(cov["total_rev"]))
    eff = None
    if sale_line_pct is not None and revenue_pct is not None:
        eff = str(min(_d(sale_line_pct), _d(revenue_pct)))

    dep_fail = (
        (
            await s.execute(
                text("""
                SELECT
                    COUNT(*) FILTER (WHERE sli.depletion_status = 'failed') AS failed,
                    COUNT(*)
                        FILTER (WHERE sli.depletion_reason = 'missing_conversion') AS missing_conv
                  FROM sale_line_items sli
                  JOIN orders o ON o.id = sli.order_id
                 WHERE sli.tenant_id = :t AND o.closed_at >= :ws AND o.closed_at < :ao
            """),
                {"t": tid, "ws": window_start, "ao": _AS_OF.get()},
            )
        )
        .mappings()
        .one()
    )

    connected = conn["state"] == "active"
    # Freshness is deliberately conservative: PR-A1 cannot certify completeness,
    # so "current" is never claimed for forecast purposes — forecast stays
    # NOT_YET_CERTIFIED regardless. We still surface truthful health signals.
    freshness = "current" if connected else "unavailable"
    blockers = [{"code": "NOT_YET_CERTIFIED"}]

    return {
        "provider": conn["vendor"],
        "connected": connected,
        "state": conn["state"],
        "last_order_event_received_at": _iso(inbox["last_received"]),
        "last_order_event_processed_at": _iso(inbox["last_processed"]),
        "last_background_check_at": _iso(conn["last_reconciliation_at"]),
        "orders_complete_through": _iso(conn["orders_complete_through"]),
        "pending_event_count": int(inbox["pending"]),
        "processing_event_count": int(inbox["processing"]),
        "failed_event_count": int(inbox["failed"]),
        "dead_letter_event_count": int(inbox["dead_letter"]),
        "oldest_pending_at": _iso(inbox["oldest_pending"]),
        "oldest_processing_at": _iso(inbox["oldest_processing"]),
        "depletion_failure_count": int(dep_fail["failed"]),
        "missing_conversion_count": int(dep_fail["missing_conv"]),
        "orders_in_window": int(orders_in_window),
        "menu_items_mapped": int(menu["mapped"]),
        "menu_items_unmapped": int(menu["unmapped"]),
        "sale_line_mapping_pct": sale_line_pct,
        "revenue_mapping_pct": revenue_pct,
        "effective_mapping_pct": eff,
        "freshness": freshness,
        "forecast_ready": False,
        "forecast_blockers": blockers,
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

    if pos["effective_mapping_pct"] is not None and _d(pos["effective_mapping_pct"]) < 100:
        reasons.append(
            {
                "code": "UNMAPPED_SOLD_ITEMS",
                "unmapped_menu_items": pos["menu_items_unmapped"],
                "effective_mapping_pct": pos["effective_mapping_pct"],
                "source": "pos",
                "drill": {"type": "recipe", "destination": "/onboarding/recipes"},
            }
        )
    if not pos["connected"]:
        reasons.append({"code": "POS_DISCONNECTED", "source": "pos", "drill": None})
    if pos["pending_event_count"] > 0:
        reasons.append(
            {
                "code": "POS_BACKLOG",
                "pending_event_count": pos["pending_event_count"],
                "oldest_pending_at": pos["oldest_pending_at"],
                "source": "pos",
                "drill": None,
            }
        )
    if pos["depletion_failure_count"] > 0:
        reasons.append(
            {
                "code": "DEPLETION_FAILURES",
                "depletion_failure_count": pos["depletion_failure_count"],
                "source": "pos",
                "drill": None,
            }
        )
    return reasons


async def _cost(s: AsyncSession, tid: UUID, iid: UUID, can_view: bool) -> dict[str, Any]:
    if not can_view:
        return {"available": False, "reason": {"code": "MANAGER_ONLY"}}
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
    return {
        "available": True,
        # Cost exact keeps its full DB precision (e.g. "120.0000") — NOT trimmed;
        # it is the 4-dp costing signal, unlike storage quantities.
        "latest_unit_cost_cents_exact": (
            str(row["unit_cost_cents_exact"]) if row["unit_cost_cents_exact"] is not None else None
        ),
        "as_of": _iso(row["effective_from"]),
        "supplier_name": row["supplier_name"],
        "receipt_id": str(row["receipt_id"]) if row["receipt_id"] else None,
    }


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
    can_view_cost: bool,
    target_cover_days: int,
    target_source: str,
) -> dict[str, Any]:
    """Assemble the PR-A1 insights payload. Caller owns the REPEATABLE READ txn
    and passes the DB-derived as_of. Forecast + reorder return NOT_YET_CERTIFIED."""
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
    pos = await _pos_health(session, tenant_id, window_start)
    consumption = await _consumption(
        session, tenant_id, item_id, item.inventory_mode, window_start, timezone_name
    )
    contributors = await _contributors(
        session, tenant_id, item_id, item.inventory_mode, window_start
    )
    reasons = await _reasons(session, tenant_id, item_id, window_start, pos, contributors)
    cost = await _cost(session, tenant_id, item_id, can_view_cost)

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
