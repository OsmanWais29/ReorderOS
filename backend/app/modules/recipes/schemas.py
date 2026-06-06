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


# ── LLM suggestion (Phase 5) — append-only suggestion layer, never a draft ───


class SuggestionIngredient(BaseModel):
    name: str
    quantity: float
    unit: str
    valid: bool  # false when unit non-canonical / name empty / qty<=0 — operator fixes
    issue: str | None = None


class ModifierSuggestionOut(BaseModel):
    modifier_id: UUID | None  # null when the model echoed an unknown id
    name: str
    confidence: str  # confident | likely | uncertain
    ingredients: list[SuggestionIngredient]


class SuggestResponse(BaseModel):
    suggestion_id: UUID
    menu_item_id: UUID
    base_ingredients: list[SuggestionIngredient]
    confidence: str
    modifiers: list[ModifierSuggestionOut]
    model_version: str
