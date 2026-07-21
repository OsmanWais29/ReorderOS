"""Typed response contract for GET /inventory/items/{id}/insights (PR-A1).

Every status, scope, blocker code, confidence and availability value is a Literal
so the OpenAPI schema documents the full enum surface and drift is caught at the
response boundary (a status outside its Literal fails serialization). Sub-models
allow extra keys so the evidence fields (counts, timestamps, ids) pass through
without data loss — the enums are the strictly-validated contract.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

_Extra = ConfigDict(extra="allow")

# ── enums ─────────────────────────────────────────────────────────────────────
StageStatus = Literal["unavailable", "failures", "unknown", "in_progress", "ok"]
ConnectionStatus = Literal["connected", "error", "disconnected"]
EventActivityStatus = Literal["unavailable", "none", "quiet", "active"]
ReconStatus = Literal["unavailable", "never_run", "recent", "stale"]
ProcessingStatus = Literal["unavailable", "stalled", "backlogged", "current"]
E2EStatus = Literal["unavailable", "failures", "in_progress", "complete", "none", "partial"]
CompletenessStatus = Literal["unproven"]
ForecastEligStatus = Literal["blocked", "eligible"]
LedgerState = Literal["OK", "DATA_INCONSISTENT", "RECONCILIATION_UNAVAILABLE"]
ItemStatus = Literal["unknown", "out", "low", "critical", "ok"]
ConfidenceStatus = Literal["floor", "complete", "unavailable"]
IngredientCompleteness = Literal["unproven", "proven"]
NotYetCertified = Literal["NOT_YET_CERTIFIED"]
Scope = Literal["tenant"]
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
    "COMPLETENESS_UNPROVEN",
    "NOT_YET_CERTIFIED",
]


# ── sub-models (extra keys = evidence fields, pass through) ──────────────────
class Snapshot(BaseModel):
    model_config = _Extra
    as_of: str
    isolation: Literal["repeatable_read"]


class ItemState(BaseModel):
    model_config = _Extra
    id: str
    status: ItemStatus


class Ledger(BaseModel):
    model_config = _Extra
    state: LedgerState
    reconciled: bool | None = None


class _StageDim(BaseModel):
    model_config = _Extra
    status: StageStatus


class ConnectionDim(BaseModel):
    model_config = _Extra
    status: ConnectionStatus


class EventActivityDim(BaseModel):
    model_config = _Extra
    status: EventActivityStatus


class ReconHealthDim(BaseModel):
    model_config = _Extra
    status: ReconStatus
    certifies_completeness: Literal[False]


class ProcessingDim(BaseModel):
    model_config = _Extra
    status: ProcessingStatus


class E2EDim(BaseModel):
    model_config = _Extra
    status: E2EStatus
    scope: Scope


class CompletenessDim(BaseModel):
    model_config = _Extra
    status: CompletenessStatus
    trusted: Literal[False]


class Blocker(BaseModel):
    model_config = _Extra
    code: BlockerCode


class ForecastEligDim(BaseModel):
    model_config = _Extra
    status: ForecastEligStatus
    blockers: list[Blocker]


class AffectedMenuItems(BaseModel):
    model_config = _Extra
    scope: Scope
    total_count: int
    has_more: bool
    items: list[dict[str, Any]]


class Dimensions(BaseModel):
    model_config = _Extra
    connection: ConnectionDim
    event_activity: EventActivityDim
    reconciliation_health: ReconHealthDim
    processing: ProcessingDim
    recipe_mapping: _StageDim
    conversion_coverage: _StageDim
    depletion_execution: _StageDim
    end_to_end_coverage: E2EDim
    affected_menu_items: AffectedMenuItems
    completeness: CompletenessDim
    forecast_eligibility: ForecastEligDim


class Pos(BaseModel):
    model_config = _Extra
    scope: Scope
    dimensions: Dimensions


class ConsumptionConfidence(BaseModel):
    model_config = _Extra
    status: ConfidenceStatus
    scope: Literal["tenant_proxy"]
    ingredient_level_completeness: IngredientCompleteness


class Consumption(BaseModel):
    model_config = _Extra
    confidence: ConsumptionConfidence


class Forecast(BaseModel):
    model_config = _Extra
    available: bool
    state: NotYetCertified


class Reorder(BaseModel):
    model_config = _Extra
    mode: Literal["unavailable", "suggestion_only"]
    state: NotYetCertified


class Cost(BaseModel):
    model_config = _Extra
    available: bool


class InsightsResponse(BaseModel):
    """The full Stock Item Insights payload (PR-A1, actuals only)."""

    model_config = _Extra
    snapshot: Snapshot
    window: dict[str, Any]
    item: ItemState
    ledger: Ledger
    pos: Pos
    consumption: Consumption
    contributors: list[dict[str, Any]]
    reasons: list[dict[str, Any]]
    forecast: Forecast
    reorder: Reorder
    cost: Cost
