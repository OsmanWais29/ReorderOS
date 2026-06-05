"""Onboarding Recipes API (Sprint 5 Phase 3) — draft side only.

Manager+ writes (PATCH, skip); Staff+ reads (list, get, progress) — via the shared
require_role dependency (same pattern as inventory), not a parallel guard. Tenant
scope comes from the principal and is applied explicitly in every repo query.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_rls_session
from app.core.security import Principal, require_role
from app.modules.inventory.depletion.units import is_canonical
from app.modules.recipes import repo
from app.modules.recipes.schemas import Progress, RecipeDetail, RecipeListItem, RecipePatch

router = APIRouter(prefix="/onboarding", tags=["recipes"])


def _validate_ingredients(body: RecipePatch) -> list[dict[str, Any]]:
    """Business validation → 400 (exit gate 12 / fail gate 5): canonical unit,
    quantity > 0, non-empty name. Returns the normalized list for JSONB storage."""
    out: list[dict[str, Any]] = []
    for ing in body.ingredients:
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


@router.get("/recipes", response_model=list[RecipeListItem])
async def list_recipes(
    include_skipped: bool = Query(default=False),
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("staff"),
) -> list[dict[str, Any]]:
    return await repo.list_recipes(
        db, UUID(principal.tenant_id), include_skipped=include_skipped
    )


@router.get("/recipes/{menu_item_id}", response_model=RecipeDetail)
async def get_recipe(
    menu_item_id: UUID,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("staff"),
) -> dict[str, Any]:
    rec = await repo.get_recipe(db, UUID(principal.tenant_id), menu_item_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="menu item not found")
    return rec


@router.patch("/recipes/{menu_item_id}", response_model=RecipeDetail)
async def patch_recipe(
    menu_item_id: UUID,
    body: RecipePatch,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("manager"),
) -> dict[str, Any]:
    ingredients = _validate_ingredients(body)
    try:
        detail = await repo.save_draft(
            db, UUID(principal.tenant_id), menu_item_id, ingredients, UUID(principal.user_id)
        )
    except repo.MenuItemNotFound:
        raise HTTPException(status_code=404, detail="menu item not found") from None
    except repo.RecipeConfirmed:
        raise HTTPException(
            status_code=409, detail="recipe is confirmed; un-confirm before editing"
        ) from None
    await db.commit()
    return detail


@router.post("/recipes/{menu_item_id}/skip", response_model=RecipeDetail)
async def skip_recipe(
    menu_item_id: UUID,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("manager"),
) -> dict[str, Any]:
    try:
        detail = await repo.skip_recipe(db, UUID(principal.tenant_id), menu_item_id)
    except repo.MenuItemNotFound:
        raise HTTPException(status_code=404, detail="menu item not found") from None
    await db.commit()
    return detail


@router.get("/progress", response_model=Progress)
async def progress(
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("staff"),
) -> dict[str, Any]:
    return await repo.get_progress(db, UUID(principal.tenant_id))
