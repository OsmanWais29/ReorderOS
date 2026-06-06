"""Shared draft-ingredient validation for the recipe and modifier PATCH endpoints.

One source of truth for the 400 rules (canonical unit, quantity > 0, non-empty name) so
recipe and modifier drafts validate identically. Canonical-unit check reuses Phase 1
units.is_canonical — not a parallel list.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.modules.inventory.depletion.units import is_canonical
from app.modules.recipes.schemas import IngredientIn


def validate_ingredients(items: list[IngredientIn]) -> list[dict[str, Any]]:
    """Validate + normalize draft ingredients for JSONB storage. Raises HTTP 400."""
    out: list[dict[str, Any]] = []
    for ing in items:
        name = ing.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="ingredient name must not be empty")
        if ing.quantity <= 0:
            raise HTTPException(
                status_code=400, detail=f"quantity must be > 0 (got {ing.quantity})"
            )
        if not is_canonical(ing.unit):
            raise HTTPException(status_code=400, detail=f"non-canonical unit: {ing.unit!r}")
        out.append(
            {
                "name": name,
                "quantity": ing.quantity,
                "unit": ing.unit,
                "inventory_item_id": (
                    str(ing.inventory_item_id) if ing.inventory_item_id else None
                ),
            }
        )
    return out
