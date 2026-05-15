"""Pydantic I/O schemas for the inventory module (Sprint 3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

# ── Count events ──────────────────────────────────────────────────────────────


class CountEventCreate(BaseModel):
    inventory_item_id: uuid.UUID
    counted_quantity: Decimal = Field(..., ge=0)
    counted_at: datetime | None = None
    notes: str | None = None


class CountEventResponse(BaseModel):
    id: uuid.UUID
    inventory_item_id: uuid.UUID
    counted_quantity: Decimal
    predicted_on_hand_at_count: Decimal | None
    created_at: datetime


# ── Receipts ──────────────────────────────────────────────────────────────────


class ReceiptCreate(BaseModel):
    notes: str | None = None
    received_at: datetime | None = None


class ReceiptResponse(BaseModel):
    id: uuid.UUID
    commit_state: str
    received_at: datetime
    notes: str | None


class ReceiptLineCreate(BaseModel):
    inventory_item_id: uuid.UUID
    received_quantity: Decimal = Field(..., gt=0)
    purchase_unit_id: uuid.UUID | None = None
    unit_cost_cents: int | None = None


class ReceiptLineResponse(BaseModel):
    id: uuid.UUID
    inventory_item_id: uuid.UUID
    received_quantity: Decimal
    unit_cost_cents: int | None


class ReceiptCommitResponse(BaseModel):
    receipt_id: uuid.UUID
    status: str
    movement_ids: list[uuid.UUID] = []


# ── Stock warnings (Phase 6) ──────────────────────────────────────────────────


class InventoryItemStatus(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    inventory_mode: str
    on_hand: Decimal | None
    stock_status: str
    count_required: bool
    count_state: str | None
    low_stock: bool | None
    out_of_stock: bool | None
    par_level: Decimal | None
    last_count_at: datetime | None


class InventoryItemsListResponse(BaseModel):
    items: list[InventoryItemStatus]
    total: int


# ── Opening balance ───────────────────────────────────────────────────────────


class OpeningBalanceCreate(BaseModel):
    quantity: Decimal = Field(..., ge=0)
    recorded_at: datetime | None = None


class OpeningBalanceResponse(BaseModel):
    movement_id: uuid.UUID
    inventory_item_id: uuid.UUID
    quantity: Decimal
