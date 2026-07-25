"""Typed response contract for GET /inventory/items/{id}/insights (PR-A1).

Every stable structure is fully typed with `extra="forbid"`, so a missing,
renamed, or extra field fails response validation (and a generated frontend
client sees a concrete shape, not an arbitrary object). Enum-valued fields are
Literals, so the OpenAPI schema documents the full enum surface. The only
flexibility is in the deliberately-polymorphic evidence carriers — `Reason`,
`StatusReason`, `Blocker`, `ConfidenceReason` — whose optional evidence fields are
still individually enumerated (no open `dict[str, Any]`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_Forbid = ConfigDict(extra="forbid")

# ── enums ─────────────────────────────────────────────────────────────────────
StageStatus = Literal["unavailable", "failures", "unknown", "in_progress", "ok"]
ConnectionStatus = Literal["connected", "error", "disconnected"]
EventActivityStatus = Literal["unavailable", "none", "quiet", "active"]
ReconStatus = Literal["unavailable", "never_run", "recent", "stale"]
ProcessingStatus = Literal["unavailable", "stalled", "backlogged", "current"]
E2EStatus = Literal[
    "unavailable",
    "data_inconsistent",
    "failures",
    "unknown",
    "in_progress",
    "complete",
    "none",
    "partial",
]
CompletenessStatus = Literal["unproven"]
ForecastEligStatus = Literal["blocked", "eligible"]
LedgerState = Literal["OK", "DATA_INCONSISTENT", "RECONCILIATION_UNAVAILABLE"]
LedgerMode = Literal["recipe_deducted", "count_anchored"]
AnchorBasis = Literal["ledger_sum", "count", "historical_window"]
ItemStatus = Literal["unknown", "out", "low", "critical", "ok"]
ConfidenceStatus = Literal["floor", "complete", "unavailable"]
IngredientCompleteness = Literal["unproven", "proven"]
NotYetCertified = Literal["NOT_YET_CERTIFIED"]
Scope = Literal["tenant"]
WindowKey = Literal["7d", "14d", "30d"]
TimezoneSource = Literal["configured", "fallback"]
LedgerRowKind = Literal["received", "pos_consumption", "refund_reversals", "adjustments"]
AffectedReasonCode = Literal[
    "NO_RECIPE",
    "INVALID_RECIPE",
    "MISSING_CONVERSION",
    "DEPLETION_FAILED",
    "PROCESSING_PENDING",
    "UNKNOWN_FAILURE",
]
RepairCode = Literal["review_recipe", "inspect_recipe_conversions"]
ReasonCode = Literal[
    "TOP_MENU_CONSUMER",
    "NO_RECENT_RECEIPT",
    "MANUAL_ADJUSTMENT",
    "UNMAPPED_SOLD_ITEMS",
    "UNKNOWN_SALE_LINES",
    "POS_DISCONNECTED",
    "POS_BACKLOG",
    "DEPLETION_FAILURES",
]
StatusReasonCode = Literal["ABOVE_PAR", "AT_OR_BELOW_PAR", "ON_HAND_UNKNOWN", "OUT_OF_STOCK"]
ConfidenceReasonCode = Literal["PROVIDER_COMPLETENESS_UNPROVEN", "TENANT_COVERAGE_INCOMPLETE"]
BlockerCode = Literal[
    "POS_DISCONNECTED",
    "PENDING_EVENTS",
    "PROCESSING_EVENTS",
    "FAILED_EVENTS",
    "POS_PROCESSING_STALLED",
    "PENDING_SALE_LINES",
    "END_TO_END_COVERAGE_INCOMPLETE",
    "CONVERSION_FAILURES",
    "DEPLETION_FAILURES",
    "UNKNOWN_SALE_LINES",
    "POS_PARTITION_DATA_INCONSISTENT",
    "COMPLETENESS_UNPROVEN",
    "NOT_YET_CERTIFIED",
]


# ── shared ────────────────────────────────────────────────────────────────────
class Drill(BaseModel):
    model_config = _Forbid
    type: Literal["recipe"]
    destination: str


class CodeReason(BaseModel):
    model_config = _Forbid
    code: str


# ── snapshot / window / item ─────────────────────────────────────────────────
class Snapshot(BaseModel):
    model_config = _Forbid
    as_of: str
    isolation: Literal["repeatable_read"]


class Window(BaseModel):
    model_config = _Forbid
    key: WindowKey
    from_: str = Field(alias="from")
    to: str
    bucket_timezone: str
    timezone_source: TimezoneSource


class StatusReason(BaseModel):
    model_config = _Forbid
    code: StatusReasonCode
    on_hand: str | None = None
    par_level: str | None = None


class ItemState(BaseModel):
    model_config = _Forbid
    id: str
    name: str
    storage_unit: str
    inventory_mode: LedgerMode
    on_hand: str | None
    par_level: str | None
    pct_of_par: str | None
    status: ItemStatus
    status_reasons: list[StatusReason]
    last_movement_at: str | None


# ── ledger ───────────────────────────────────────────────────────────────────
class LedgerRow(BaseModel):
    model_config = _Forbid
    kind: LedgerRowKind
    quantity: str | None
    events: int


class Ledger(BaseModel):
    model_config = _Forbid
    mode: LedgerMode
    anchor_basis: AnchorBasis
    reconciled: bool
    state: LedgerState
    rows: list[LedgerRow]
    current_on_hand: str | None
    # Mode-A only
    balance_at_window_start: str | None = None
    anchor_at: str | None = None
    anchor_quantity: str | None = None
    window_sum: str | None = None
    reconciliation_delta: str | None = None
    # Mode-B only
    reason: CodeReason | None = None


# ── POS dimensions ───────────────────────────────────────────────────────────
class ConnectionDim(BaseModel):
    model_config = _Forbid
    status: ConnectionStatus
    provider: str | None
    state: str | None


class EventActivityDim(BaseModel):
    model_config = _Forbid
    status: EventActivityStatus
    latest_sales_data_received_at: str | None


class ReconHealthDim(BaseModel):
    model_config = _Forbid
    status: ReconStatus
    latest_background_reconciliation_check_at: str | None
    stale_after_seconds: int
    certifies_completeness: Literal[False]


class ProcessingDim(BaseModel):
    model_config = _Forbid
    status: ProcessingStatus
    latest_sales_data_processed_at: str | None
    pending_event_count: int
    processing_event_count: int
    failed_event_count: int
    permanently_failed_event_count: int
    oldest_pending_at: str | None
    oldest_processing_at: str | None
    historical_unresolved_event_count: int


class HistoricalWindow(BaseModel):
    model_config = _Forbid
    eligible_sale_line_count: int
    with_recipe_count: int
    coverage_pct: str | None
    no_recipe_count: int
    invalid_recipe_count: int
    pending_count: int
    unknown_count: int
    note: str


class CurrentCatalog(BaseModel):
    model_config = _Forbid
    menu_items_mapped: int
    menu_items_unmapped: int
    note: str


class RecipeMappingDim(BaseModel):
    model_config = _Forbid
    status: StageStatus
    historical_window: HistoricalWindow
    current_catalog: CurrentCatalog


class ConversionCoverageDim(BaseModel):
    model_config = _Forbid
    status: StageStatus
    with_recipe_count: int
    converted_count: int
    coverage_pct: str | None
    missing_conversion_count: int
    unknown_count: int


class DepletionExecutionDim(BaseModel):
    model_config = _Forbid
    status: StageStatus
    convertible_count: int
    depleted_count: int
    coverage_pct: str | None
    depletion_failure_count: int
    unknown_count: int


class ReasonBreakdown(BaseModel):
    model_config = _Forbid
    NO_RECIPE: int = Field(ge=0)
    INVALID_RECIPE: int = Field(ge=0)
    MISSING_CONVERSION: int = Field(ge=0)
    DEPLETION_FAILED: int = Field(ge=0)
    PROCESSING_PENDING: int = Field(ge=0)
    UNKNOWN: int = Field(ge=0)


class E2EDim(BaseModel):
    model_config = _Forbid
    scope: Scope
    status: E2EStatus
    failure_count: int = Field(ge=0)
    # unknown = honest remainder (never negative); overlap > 0 ONLY when the
    # partition is impossible (double-counted rows) → status data_inconsistent.
    unknown_line_count: int = Field(ge=0)
    overlap_line_count: int = Field(ge=0)
    pending_line_count: int = Field(ge=0)
    eligible_sale_line_count: int = Field(ge=0)
    depleted_sale_line_count: int = Field(ge=0)
    line_coverage_pct: str | None
    eligible_net_revenue_cents: int
    depleted_net_revenue_cents: int
    revenue_coverage_pct: str | None
    revenue_coverage_applicable: bool
    effective_coverage_pct: str | None
    reason_breakdown: ReasonBreakdown


class AffectedItem(BaseModel):
    model_config = _Forbid
    menu_item_id: str | None
    menu_item: str | None
    affected_sale_line_count: int
    units_sold: str | None
    revenue_cents: int
    reason_code: AffectedReasonCode
    repair: RepairCode | None


class AffectedMenuItems(BaseModel):
    model_config = _Forbid
    scope: Scope
    items: list[AffectedItem]
    total_count: int
    has_more: bool


class CompletenessDim(BaseModel):
    model_config = _Forbid
    status: CompletenessStatus
    candidate_orders_complete_through: str | None
    trusted: Literal[False]
    reason: CodeReason


class Blocker(BaseModel):
    model_config = _Forbid
    code: BlockerCode
    count: int | None = None
    effective_pct: str | None = None


class ForecastEligDim(BaseModel):
    model_config = _Forbid
    status: ForecastEligStatus
    blockers: list[Blocker]


class Dimensions(BaseModel):
    model_config = _Forbid
    connection: ConnectionDim
    event_activity: EventActivityDim
    reconciliation_health: ReconHealthDim
    processing: ProcessingDim
    recipe_mapping: RecipeMappingDim
    conversion_coverage: ConversionCoverageDim
    depletion_execution: DepletionExecutionDim
    end_to_end_coverage: E2EDim
    affected_menu_items: AffectedMenuItems
    completeness: CompletenessDim
    forecast_eligibility: ForecastEligDim


class Pos(BaseModel):
    model_config = _Forbid
    provider: str | None
    scope: Scope
    orders_seen_in_window: int
    eligible_orders_in_window: int
    coverage_denominator: Literal["eligible_sale_lines"]
    dimensions: Dimensions


# ── consumption ──────────────────────────────────────────────────────────────
class DailyConsumption(BaseModel):
    model_config = _Forbid
    date: str
    consumed: str | None
    orders: int
    mapped_sale_lines: int
    is_partial: bool


class Trend(BaseModel):
    model_config = _Forbid
    available: bool
    state: NotYetCertified


class ConsumptionSummary(BaseModel):
    model_config = _Forbid
    basis: Literal["observed_day"]
    avg_per_observed_day: str | None
    peak_observed_day: str | None
    observed_days: int
    trend: Trend


class ConfidenceReason(BaseModel):
    model_config = _Forbid
    code: ConfidenceReasonCode
    effective_coverage_pct: str | None = None


class ConsumptionConfidence(BaseModel):
    model_config = _Forbid
    status: ConfidenceStatus
    scope: Literal["tenant_proxy"]
    ingredient_level_completeness: IngredientCompleteness
    effective_coverage_pct: str | None
    reasons: list[ConfidenceReason]


class Consumption(BaseModel):
    model_config = _Forbid
    bucket_timezone: str
    completeness: Literal["uncertified"]
    completeness_reason: CodeReason
    daily: list[DailyConsumption]
    summary: ConsumptionSummary
    confidence: ConsumptionConfidence


# ── contributors ─────────────────────────────────────────────────────────────
class ContributorGroup(BaseModel):
    model_config = _Forbid
    recipe_version: int | None
    units_sold: str | None
    qty_per_sale: str | None
    consumed: str | None
    explained: bool


class Contributor(BaseModel):
    model_config = _Forbid
    menu_item_id: str | None
    menu_item: str
    mapping: Literal["confirmed", "unmapped"]
    total_consumed: str | None
    share_pct: str
    groups: list[ContributorGroup]


# ── reasons (polymorphic evidence carrier, fields enumerated) ────────────────
class Reason(BaseModel):
    model_config = _Forbid
    code: ReasonCode
    source: Literal["sales", "receipts", "count_event", "pos"]
    drill: Drill | None = None
    # evidence fields (only the relevant ones are present per code)
    menu_item: str | None = None
    menu_item_id: str | None = None
    units_sold: str | None = None
    qty_per_sale: str | None = None
    total_consumed: str | None = None
    days_since: int | None = None
    delta: str | None = None
    at: str | None = None
    unmapped_menu_items: int | None = None
    effective_coverage_pct: str | None = None
    pending_event_count: int | None = None
    oldest_pending_at: str | None = None
    depletion_failure_count: int | None = None
    unknown_line_count: int | None = None


# ── forecast / reorder ───────────────────────────────────────────────────────
class Forecast(BaseModel):
    model_config = _Forbid
    available: bool
    state: NotYetCertified
    blockers: list[Blocker]


class Reorder(BaseModel):
    model_config = _Forbid
    mode: Literal["unavailable", "suggestion_only"]
    state: NotYetCertified
    target_cover_days: int
    target_source: Literal["default", "scenario"]
    blockers: list[Blocker]


# ── cost ─────────────────────────────────────────────────────────────────────
class CostHistoryEntry(BaseModel):
    model_config = _Forbid
    unit_cost_cents_exact: str | None
    effective_from: str | None
    receipt_id: str | None


class CostAggregated(BaseModel):
    model_config = _Forbid
    available: bool
    reason: CodeReason | None = None
    supplier_name: str | None = None
    receipt_id: str | None = None
    history: list[CostHistoryEntry] | None = None


class Cost(BaseModel):
    model_config = _Forbid
    available: bool
    reason: CodeReason | None = None
    latest_unit_cost_cents_exact: str | None = None
    as_of: str | None = None
    aggregated: CostAggregated | None = None


# ── top-level ────────────────────────────────────────────────────────────────
class InsightsResponse(BaseModel):
    """The full Stock Item Insights payload (PR-A1, actuals only)."""

    model_config = _Forbid
    snapshot: Snapshot
    window: Window
    item: ItemState
    ledger: Ledger
    pos: Pos
    consumption: Consumption
    contributors: list[Contributor]
    reasons: list[Reason]
    forecast: Forecast
    reorder: Reorder
    cost: Cost
