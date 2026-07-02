"""Shared draft-ingredient validation for the recipe and modifier PATCH endpoints.

One source of truth for the 400 rules (canonical unit, quantity > 0, non-empty name) so
recipe and modifier drafts validate identically. Canonical-unit check reuses Phase 1
units.is_canonical — not a parallel list.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import HTTPException

from app.modules.inventory.depletion.units import is_canonical
from app.modules.recipes.schemas import IngredientIn

# Sanity ceiling for a single recipe-line quantity in canonical units. Far above
# any real per-serving amount (largest seen is in the thousands), it only rejects
# garbage. Pydantic defaults allow_inf_nan=True, so NaN/inf reach here — they pass
# `<= 0` (every NaN comparison is False; inf <= 0 is False) and would otherwise
# corrupt the JSONB draft and the downstream depletion math.
MAX_QUANTITY: float = 10_000_000.0


def validate_ingredients(items: list[IngredientIn]) -> list[dict[str, Any]]:
    """Validate + normalize draft ingredients for JSONB storage. Raises HTTP 400."""
    out: list[dict[str, Any]] = []
    for ing in items:
        name = ing.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="ingredient name must not be empty")
        if not math.isfinite(ing.quantity):
            raise HTTPException(
                status_code=400, detail=f"quantity must be a finite number (got {ing.quantity})"
            )
        if ing.quantity <= 0:
            raise HTTPException(
                status_code=400, detail=f"quantity must be > 0 (got {ing.quantity})"
            )
        if ing.quantity > MAX_QUANTITY:
            raise HTTPException(
                status_code=400,
                detail=f"quantity {ing.quantity} exceeds max {MAX_QUANTITY:.0f}",
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
