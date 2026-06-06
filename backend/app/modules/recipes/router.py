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

from app.core.config import get_settings
from app.core.deps import get_rls_session
from app.core.security import Principal, require_role
from app.modules.recipes import repo
from app.modules.recipes.llm_client import AnthropicLLMClient, LLMClient, LLMUnavailable
from app.modules.recipes.schemas import (
    Progress,
    RecipeDetail,
    RecipeListItem,
    RecipePatch,
    SuggestResponse,
)
from app.modules.recipes.suggest import suggest_recipe
from app.modules.recipes.validators import validate_ingredients

router = APIRouter(prefix="/onboarding", tags=["recipes"])


def get_llm_client() -> LLMClient:
    """Provide the Anthropic-backed client, or 503 if no key is configured. Tests
    override this dependency with a fake — no network, no SDK key needed."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="LLM suggestion service not configured")
    return AnthropicLLMClient(settings.anthropic_api_key, settings.anthropic_model)


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
    ingredients = validate_ingredients(body.ingredients)
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
    except repo.RecipeConfirmed:
        raise HTTPException(
            status_code=409, detail="recipe is confirmed; un-confirm before skipping"
        ) from None
    await db.commit()
    return detail


@router.post("/recipes/{menu_item_id}/confirm", response_model=RecipeDetail)
async def confirm_recipe(
    menu_item_id: UUID,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("manager"),
) -> dict[str, Any]:
    """Confirm a draft into an immutable recipe_version (one atomic transaction)."""
    try:
        detail = await repo.confirm_recipe(db, UUID(principal.tenant_id), menu_item_id)
    except repo.MenuItemNotFound:
        raise HTTPException(status_code=404, detail="menu item not found") from None
    except repo.NoDraft:
        raise HTTPException(status_code=400, detail="no draft to confirm") from None
    except repo.EmptyDraft:
        raise HTTPException(
            status_code=400, detail="draft has no ingredients to confirm"
        ) from None
    except repo.DuplicateIngredient:
        raise HTTPException(
            status_code=400,
            detail="duplicate ingredient names in draft (they resolve to one item)",
        ) from None
    except repo.RecipeConfirmed:
        raise HTTPException(status_code=409, detail="recipe is already confirmed") from None
    except repo.RecipeSkipped:
        raise HTTPException(
            status_code=409, detail="recipe is skipped; un-skip before confirming"
        ) from None
    except repo.UnitTypeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await db.commit()
    return detail


@router.post("/recipes/{menu_item_id}/unconfirm", response_model=RecipeDetail)
async def unconfirm_recipe(
    menu_item_id: UUID,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("manager"),
) -> dict[str, Any]:
    """Re-open a confirmed recipe into a draft copy, without mutating any version."""
    try:
        detail = await repo.unconfirm_recipe(
            db, UUID(principal.tenant_id), menu_item_id, UUID(principal.user_id)
        )
    except repo.MenuItemNotFound:
        raise HTTPException(status_code=404, detail="menu item not found") from None
    except repo.NotConfirmed:
        raise HTTPException(status_code=409, detail="recipe is not confirmed") from None
    await db.commit()
    return detail


@router.post("/recipes/{menu_item_id}/suggest", response_model=SuggestResponse)
async def suggest(
    menu_item_id: UUID,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("manager"),
    llm: LLMClient = Depends(get_llm_client),
) -> dict[str, Any]:
    """Infer base + modifier ingredients via Claude and store them append-only. Does
    NOT write recipe_drafts/recipe_versions — promotion to a draft is the operator's
    separate PATCH. An LLM failure (503) cannot corrupt a draft or version."""
    try:
        result = await suggest_recipe(db, llm, UUID(principal.tenant_id), menu_item_id)
    except repo.MenuItemNotFound:
        raise HTTPException(status_code=404, detail="menu item not found") from None
    except LLMUnavailable:
        raise HTTPException(
            status_code=503, detail="recipe suggestion is temporarily unavailable"
        ) from None
    await db.commit()
    return result


@router.get("/progress", response_model=Progress)
async def progress(
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("staff"),
) -> dict[str, Any]:
    return await repo.get_progress(db, UUID(principal.tenant_id))
