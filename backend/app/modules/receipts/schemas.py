"""Pydantic models for the receipts API (Sprint 6 S2)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


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


class ReceiptLineOut(BaseModel):
    id: UUID
    extracted_name: str | None
    inventory_item_id: UUID | None
    received_quantity: float | None
    unit_cost_cents: int | None
    confidence: float | None
    manually_corrected: bool
    match_status: str
    line_ordinal: int | None


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
    extraction_confidence: float | None
    review_visibility_status: str
    lines: list[ReceiptLineOut]
