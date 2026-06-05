"""Pydantic schemas for the onboarding Recipes API (Sprint 5 Phase 3)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel


class IngredientIn(BaseModel):
    """One draft ingredient line. Business validation (canonical unit, qty > 0,
    non-empty name) happens in the router → 400; this only fixes the shape."""

    name: str
    quantity: float
    unit: str
    inventory_item_id: UUID | None = None


class RecipePatch(BaseModel):
    ingredients: list[IngredientIn]


class RecipeListItem(BaseModel):
    menu_item_id: UUID
    name: str
    active: bool
    status: str  # none | draft | confirmed | skipped
    ingredient_count: int
    modifier_count: int
    volume_30d: float


class RecipeDetail(BaseModel):
    menu_item_id: UUID
    name: str
    status: str
    ingredients: list[dict[str, Any]]


class Progress(BaseModel):
    total: int  # active menu items
    confirmed: int
    skipped: int
    denominator: int  # total - skipped
    percent: float | None  # null when denominator is 0
