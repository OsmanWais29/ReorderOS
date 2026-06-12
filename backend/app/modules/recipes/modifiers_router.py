"""Modifier configuration API (Sprint 5 Phase 6).

Routes nest under the parent menu item so the parent-menu-item scoping (guard 1) is a
property of the URL: every handler resolves the modifier within (tenant, menu_item) and
returns 404 on mismatch — no existence leak. Manager+ writes, Staff+ reads, via the
shared require_role. Confirm/un-confirm reuse the recipe-confirm machinery (nouns
swapped) through modifiers_repo.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_rls_session
from app.core.security import Principal, require_role
from app.modules.recipes import modifiers_repo as mrepo
from app.modules.recipes.repo import (
    DuplicateIngredient,
    EmptyDraft,
    NoDraft,
    UnitTypeConflict,
)
from app.modules.recipes.schemas import ModifierDetail, ModifierListItem, RecipePatch
from app.modules.recipes.validators import validate_ingredients

router = APIRouter(prefix="/onboarding/recipes", tags=["modifiers"])


@router.get("/{menu_item_id}/modifiers", response_model=list[ModifierListItem])
async def list_modifiers(
    menu_item_id: UUID,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("staff"),
) -> list[dict[str, Any]]:
    try:
        return await mrepo.list_modifiers(db, UUID(principal.tenant_id), menu_item_id)
    except mrepo.ModifierNotFound:
        raise HTTPException(status_code=404, detail="menu item not found") from None


@router.get("/{menu_item_id}/modifiers/{modifier_id}", response_model=ModifierDetail)
async def get_modifier(
    menu_item_id: UUID,
    modifier_id: UUID,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("staff"),
) -> dict[str, Any]:
    """Read-only modifier detail (Staff+) — the partner of the recipe-detail GET; parent-scoped,
    pure read (no implicit unskip/409). 404 on wrong parent / cross-tenant."""
    try:
        return await mrepo.get_modifier(
            db, UUID(principal.tenant_id), menu_item_id, modifier_id
        )
    except mrepo.ModifierNotFound:
        raise HTTPException(status_code=404, detail="modifier not found") from None


@router.patch("/{menu_item_id}/modifiers/{modifier_id}", response_model=ModifierDetail)
async def patch_modifier(
    menu_item_id: UUID,
    modifier_id: UUID,
    body: RecipePatch,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("manager"),
) -> dict[str, Any]:
    ingredients = validate_ingredients(body.ingredients)
    try:
        detail = await mrepo.save_draft(
            db, UUID(principal.tenant_id), menu_item_id, modifier_id,
            ingredients, UUID(principal.user_id),
        )
    except mrepo.ModifierNotFound:
        raise HTTPException(status_code=404, detail="modifier not found") from None
    except mrepo.ModifierConfirmed:
        raise HTTPException(
            status_code=409, detail="modifier is confirmed; un-confirm before editing"
        ) from None
    await db.commit()
    return detail


@router.post("/{menu_item_id}/modifiers/{modifier_id}/confirm", response_model=ModifierDetail)
async def confirm_modifier(
    menu_item_id: UUID,
    modifier_id: UUID,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("manager"),
) -> dict[str, Any]:
    try:
        detail = await mrepo.confirm_modifier(
            db, UUID(principal.tenant_id), menu_item_id, modifier_id, UUID(principal.user_id)
        )
    except mrepo.ModifierNotFound:
        raise HTTPException(status_code=404, detail="modifier not found") from None
    except NoDraft:
        raise HTTPException(status_code=400, detail="no draft to confirm") from None
    except EmptyDraft:
        raise HTTPException(status_code=400, detail="draft has no ingredients to confirm") from None
    except DuplicateIngredient:
        raise HTTPException(
            status_code=400, detail="duplicate ingredient names in draft"
        ) from None
    except mrepo.ModifierConfirmed:
        raise HTTPException(status_code=409, detail="modifier is already confirmed") from None
    except mrepo.ModifierSkipped:
        raise HTTPException(
            status_code=409, detail="modifier is skipped; un-skip before confirming"
        ) from None
    except mrepo.NonAdditiveModifier:
        raise HTTPException(
            status_code=409,
            detail="only additive modifiers can be configured for depletion",
        ) from None
    except UnitTypeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await db.commit()
    return detail


@router.post("/{menu_item_id}/modifiers/{modifier_id}/unconfirm", response_model=ModifierDetail)
async def unconfirm_modifier(
    menu_item_id: UUID,
    modifier_id: UUID,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("manager"),
) -> dict[str, Any]:
    try:
        detail = await mrepo.unconfirm_modifier(
            db, UUID(principal.tenant_id), menu_item_id, modifier_id, UUID(principal.user_id)
        )
    except mrepo.ModifierNotFound:
        raise HTTPException(status_code=404, detail="modifier not found") from None
    except mrepo.ModifierNotConfirmed:
        raise HTTPException(status_code=409, detail="modifier is not confirmed") from None
    await db.commit()
    return detail


@router.post("/{menu_item_id}/modifiers/{modifier_id}/skip", response_model=ModifierDetail)
async def skip_modifier(
    menu_item_id: UUID,
    modifier_id: UUID,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("manager"),
) -> dict[str, Any]:
    try:
        detail = await mrepo.skip_modifier(
            db, UUID(principal.tenant_id), menu_item_id, modifier_id
        )
    except mrepo.ModifierNotFound:
        raise HTTPException(status_code=404, detail="modifier not found") from None
    except mrepo.ModifierConfirmed:
        raise HTTPException(
            status_code=409, detail="modifier is confirmed; un-confirm before skipping"
        ) from None
    await db.commit()
    return detail
