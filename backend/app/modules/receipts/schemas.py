"""Pydantic models for the receipts API (Sprint 6 S2 + review endpoints)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.modules.inventory.depletion.units import CANONICAL_UNITS


class ReceiptCreate(BaseModel):
    """Create a manual draft receipt (no photo)."""

    supplier_name: str | None = None
    invoice_date: date | None = None
    notes: str | None = None


class UploadResponse(BaseModel):
    receipt_id: UUID
    photo_object_key: str
    mime_type: str
    extraction_status: str


class DismissRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class CommitRequest(BaseModel):
    confirm: bool = False
    reviewed_affirmation: bool = False
    idempotency_key: UUID | None = None


class AdjustRequest(BaseModel):
    adjustment_type: Literal["correction", "return", "damage", "count_fix"]
    inventory_item_id: UUID
    delta_quantity: Decimal = Field(description="Signed, in storage units; must be non-zero")
    delta_unit: str
    reason: str | None = None
    receipt_line_id: UUID | None = None
    delta_cost_cents: int | None = None

    @field_validator("delta_quantity")
    @classmethod
    def _non_zero(cls, v: Decimal) -> Decimal:
        if v == 0:
            raise ValueError("delta_quantity must be non-zero")
        return v


class LineUpdate(BaseModel):
    """PATCH-style line edit (spec §5 PUT /lines/{line_id}, D-606-25/26).

    Tri-state item link: field ABSENT = unchanged; explicit null = clear the item
    (→ unmatched, manually_corrected=false); UUID = link an existing item
    (→ matched, manually_corrected=true). `new_item_name`+`new_item_unit` creates
    and links a new item via the shared resolver (→ created). `skipped` is its own
    action and cannot be combined with item/field edits."""

    inventory_item_id: UUID | None = None
    new_item_name: str | None = Field(default=None, min_length=1, max_length=200)
    new_item_unit: str | None = None
    received_quantity: Decimal | None = Field(default=None, gt=0)
    extracted_unit: str | None = None
    unit_cost_cents: int | None = Field(default=None, ge=0)
    extracted_name: str | None = Field(default=None, min_length=1, max_length=500)
    skipped: bool | None = None
    # Conversion confirmation (purchase U/M → storage unit). Setting received_unit
    # means "receive received_quantity x received_unit for this line"; the line's
    # invoice qty/U-M are stashed into purchase_quantity/purchase_unit. Requires
    # received_quantity + conversion_factor in the same call — an explicit,
    # operator-confirmed statement, never a partial one.
    received_unit: str | None = None
    conversion_factor: Decimal | None = Field(default=None, gt=0)
    remember_conversion: bool = False

    @field_validator("extracted_unit", "new_item_unit", "received_unit")
    @classmethod
    def _canonical_unit(cls, v: str | None) -> str | None:
        if v is not None and v not in CANONICAL_UNITS:
            raise ValueError(f"unit {v!r} is not canonical")
        return v

    @model_validator(mode="after")
    def _coherent(self) -> LineUpdate:
        if not self.model_fields_set:
            raise ValueError("empty update — provide at least one field")
        if "skipped" in self.model_fields_set and self.skipped is None:
            raise ValueError("skipped must be true or false, not null")
        if "received_quantity" in self.model_fields_set and self.received_quantity is None:
            raise ValueError("received_quantity cannot be cleared")
        links_item = (
            "inventory_item_id" in self.model_fields_set and self.inventory_item_id is not None
        )
        clears_item = (
            "inventory_item_id" in self.model_fields_set and self.inventory_item_id is None
        )
        creates_item = self.new_item_name is not None
        edits_fields = bool(
            self.model_fields_set
            & {"received_quantity", "extracted_unit", "unit_cost_cents", "extracted_name"}
        )
        if creates_item and self.new_item_unit is None:
            raise ValueError("new_item_name requires new_item_unit")
        if self.new_item_unit is not None and not creates_item:
            raise ValueError("new_item_unit is only valid together with new_item_name")
        if creates_item and (links_item or clears_item):
            raise ValueError("provide inventory_item_id OR new_item_name, not both")
        if self.skipped is not None and (links_item or clears_item or creates_item or edits_fields):
            raise ValueError("skip/unskip is its own action — no other edits in the same call")
        if clears_item and edits_fields:
            raise ValueError(
                "clearing the item reverts the line to machine state — "
                "no field edits in the same call"
            )
        confirms = "received_unit" in self.model_fields_set
        if confirms and (
            self.received_unit is None
            or self.received_quantity is None
            or self.conversion_factor is None
        ):
            raise ValueError(
                "confirming a conversion requires received_unit, received_quantity "
                "and conversion_factor together"
            )
        if self.remember_conversion and not confirms:
            raise ValueError("remember_conversion is only valid with a conversion confirmation")
        if confirms and (self.skipped is not None or clears_item):
            raise ValueError("confirm conversion on an active, linked line only")
        return self


class LineCreate(BaseModel):
    """Add an operator line (spec §5 POST /lines): unmatched until an item is set,
    matched immediately when inventory_item_id is provided."""

    extracted_name: str = Field(min_length=1, max_length=500)
    received_quantity: Decimal = Field(gt=0)
    extracted_unit: str
    unit_cost_cents: int | None = Field(default=None, ge=0)
    inventory_item_id: UUID | None = None

    @field_validator("extracted_unit")
    @classmethod
    def _canonical_unit(cls, v: str) -> str:
        if v not in CANONICAL_UNITS:
            raise ValueError(f"unit {v!r} is not canonical")
        return v


class ResetExtractionRequest(BaseModel):
    """Destructive start-over (spec §5 reset-extraction): requires an explicit
    discard_edits=true or the endpoint returns 409 RECEIPT_RESET_NEEDS_CONFIRM."""

    discard_edits: bool = False


class NoteCreate(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class NoteOut(BaseModel):
    id: UUID
    user_id: UUID
    text: str
    created_at: datetime


class ItemSuggestion(BaseModel):
    """Ranked match suggestion for an unmatched line — a suggestion, never an
    auto-match (D-606-26)."""

    id: UUID
    name: str


class ReceiptLineOut(BaseModel):
    id: UUID
    extracted_name: str | None
    inventory_item_id: UUID | None
    # Linked item's CURRENT name — operator-visible proof of what a match/create
    # actually linked (extracted_name stays the verbatim invoice text).
    item_name: str | None = None
    # Linked item's canonical storage unit — the conversion panel's target.
    item_storage_unit: str | None = None
    # Invoice originals (stashed when a conversion is confirmed).
    purchase_quantity: float | None = None
    purchase_unit: str | None = None
    # Confirmed conversion state.
    received_unit: str | None = None
    conversion_factor: float | None = None
    conversion_source: str | None = None
    conversion_confirmed_at: datetime | None = None
    # Prefill suggestion (pack hints / actual weight / remembered) — never authority.
    suggested_quantity: float | None = None
    suggested_factor: float | None = None
    suggestion_source: str | None = None
    # Raw packaging clues from extraction — shown so the operator can verify the
    # suggestion against what the invoice actually printed ("4x4L").
    pack_count: float | None = None
    pack_size_qty: float | None = None
    pack_size_unit: str | None = None
    actual_weight_qty: float | None = None
    actual_weight_unit: str | None = None
    received_quantity: float | None
    extracted_unit: str | None
    unit_cost_cents: int | None
    confidence: float | None
    manually_corrected: bool
    match_status: str
    line_ordinal: int | None
    suggestions: list[ItemSuggestion] = []


class ReceiptListItem(BaseModel):
    id: UUID
    source: str
    commit_state: str
    extraction_status: str
    supplier_name: str | None
    total_cents: int | None
    manual_entry_required: bool
    quota_blocked: bool
    created_at: datetime


class ReceiptDetail(ReceiptListItem):
    photo_object_key: str | None
    photo_url: str | None
    mime_type: str | None
    invoice_number: str | None
    invoice_date: date | None
    subtotal_cents: int | None
    tax_cents: int | None
    extraction_confidence: float | None
    review_visibility_status: str
    sender_email: str | None
    filter_flags: list[str]
    reviewed_affirmation: bool
    review_started_at: datetime | None
    notes_log: list[NoteOut]
    lines: list[ReceiptLineOut]
