"""Data access for the onboarding Recipes API (Sprint 5 Phase 3).

DRAFT SIDE ONLY — writes touch only ``recipes`` and ``recipe_drafts``. This module
never writes (and the depletion engine never reads) ``recipe_versions`` /
``recipe_ingredients``.

Every query is explicitly scoped by tenant_id (not relying on RLS alone, since the
app connects as a role that may bypass RLS): a menu_item_id belonging to another
tenant simply isn't found → the router returns 404, never leaking existence.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class MenuItemNotFound(Exception):
    """The menu_item_id does not exist for this tenant (→ 404)."""


class RecipeConfirmed(Exception):
    """The recipe is confirmed; drafts cannot be edited (→ 409). The 'confirmed'
    state is produced by Phase 4's confirm endpoint; this guard is built and
    tested now so it is correct when that lands."""


def _as_list(value: Any) -> list[dict[str, Any]]:
    """jsonb may arrive as a str (asyncpg) or already-decoded list."""
    if value is None:
        return []
    if isinstance(value, str):
        return list(json.loads(value))
    return list(value)


async def list_recipes(
    db: AsyncSession, tenant_id: UUID, *, include_skipped: bool
) -> list[dict[str, Any]]:
    """Active menu items with recipe state, draft ingredient count, modifier
    count, and 30-day sales volume. Skipped recipes hidden unless include_skipped.

    Volume is the 30-day sum(quantity) of active (non-refunded, non-voided) sale
    lines, served by the idx_sli_active_sales partial index (0006)."""
    rows = (
        await db.execute(
            text("""
                SELECT
                    mi.id                                        AS menu_item_id,
                    mi.name                                      AS name,
                    mi.active                                    AS active,
                    COALESCE(r.status, 'none')                   AS status,
                    COALESCE(jsonb_array_length(rd.draft_ingredients), 0) AS ingredient_count,
                    COALESCE(mc.n, 0)                            AS modifier_count,
                    COALESCE(v.qty, 0)                           AS volume_30d
                FROM menu_items mi
                LEFT JOIN recipes r
                    ON r.tenant_id = mi.tenant_id AND r.menu_item_id = mi.id
                LEFT JOIN recipe_drafts rd
                    ON rd.tenant_id = mi.tenant_id AND rd.recipe_id = r.id
                LEFT JOIN (
                    SELECT menu_item_id, count(*) AS n
                    FROM modifiers WHERE tenant_id = :tid
                    GROUP BY menu_item_id
                ) mc ON mc.menu_item_id = mi.id
                LEFT JOIN (
                    SELECT menu_item_id, sum(quantity) AS qty
                    FROM sale_line_items
                    WHERE tenant_id = :tid
                      AND created_at > now() - INTERVAL '30 days'
                      AND NOT is_refunded AND NOT is_voided
                    GROUP BY menu_item_id
                ) v ON v.menu_item_id = mi.id
                WHERE mi.tenant_id = :tid AND mi.active = true
                  AND (:include_skipped OR COALESCE(r.status, 'none') <> 'skipped')
                ORDER BY COALESCE(v.qty, 0) DESC, mi.name ASC
            """),
            {"tid": tenant_id, "include_skipped": include_skipped},
        )
    ).mappings().all()
    return [
        {
            "menu_item_id": row["menu_item_id"],
            "name": row["name"],
            "active": row["active"],
            "status": row["status"],
            "ingredient_count": int(row["ingredient_count"]),
            "modifier_count": int(row["modifier_count"]),
            "volume_30d": float(row["volume_30d"]),
        }
        for row in rows
    ]


async def get_recipe(
    db: AsyncSession, tenant_id: UUID, menu_item_id: UUID
) -> dict[str, Any] | None:
    """Single menu item's recipe state + draft ingredients. None if the menu item
    isn't this tenant's (→ 404)."""
    row = (
        await db.execute(
            text("""
                SELECT mi.id AS menu_item_id, mi.name AS name,
                       COALESCE(r.status, 'none') AS status,
                       rd.draft_ingredients AS ingredients
                FROM menu_items mi
                LEFT JOIN recipes r
                    ON r.tenant_id = mi.tenant_id AND r.menu_item_id = mi.id
                LEFT JOIN recipe_drafts rd
                    ON rd.tenant_id = mi.tenant_id AND rd.recipe_id = r.id
                WHERE mi.id = :mid AND mi.tenant_id = :tid
            """),
            {"mid": menu_item_id, "tid": tenant_id},
        )
    ).mappings().fetchone()
    if row is None:
        return None
    return {
        "menu_item_id": row["menu_item_id"],
        "name": row["name"],
        "status": row["status"],
        "ingredients": _as_list(row["ingredients"]),
    }


async def save_draft(
    db: AsyncSession,
    tenant_id: UUID,
    menu_item_id: UUID,
    ingredients: list[dict[str, Any]],
    created_by: UUID,
) -> dict[str, Any]:
    """Auto-save a draft. First edit creates recipes(draft)+recipe_drafts in one
    transaction; a skipped recipe moves back to draft; a confirmed recipe is
    rejected. Touches only recipes + recipe_drafts. Returns the resulting detail
    (no post-commit re-query needed). Caller commits.

    Raises MenuItemNotFound (404) / RecipeConfirmed (409)."""
    mi = (
        await db.execute(
            text("SELECT id, name FROM menu_items WHERE id = :mid AND tenant_id = :tid"),
            {"mid": menu_item_id, "tid": tenant_id},
        )
    ).mappings().fetchone()
    if mi is None:
        raise MenuItemNotFound

    rec = (
        await db.execute(
            text(
                "SELECT id, status FROM recipes"
                " WHERE tenant_id = :tid AND menu_item_id = :mid"
            ),
            {"tid": tenant_id, "mid": menu_item_id},
        )
    ).mappings().fetchone()
    if rec is not None and rec["status"] == "confirmed":
        raise RecipeConfirmed

    if rec is None:
        recipe_id = (
            await db.execute(
                text(
                    "INSERT INTO recipes (tenant_id, menu_item_id, status)"
                    " VALUES (:tid, :mid, 'draft') RETURNING id"
                ),
                {"tid": tenant_id, "mid": menu_item_id},
            )
        ).scalar_one()
    else:
        recipe_id = rec["id"]
        # draft or skipped -> draft (skipped moves back to draft on edit)
        await db.execute(
            text("UPDATE recipes SET status = 'draft', updated_at = now() WHERE id = :rid"),
            {"rid": recipe_id},
        )

    await db.execute(
        text("""
            INSERT INTO recipe_drafts (tenant_id, recipe_id, draft_ingredients, created_by)
            VALUES (:tid, :rid, CAST(:di AS jsonb), :uid)
            ON CONFLICT (tenant_id, recipe_id)
            DO UPDATE SET draft_ingredients = CAST(:di AS jsonb), updated_at = now()
        """),
        {"tid": tenant_id, "rid": recipe_id, "di": json.dumps(ingredients), "uid": created_by},
    )
    return {
        "menu_item_id": menu_item_id,
        "name": mi["name"],
        "status": "draft",
        "ingredients": ingredients,
    }


async def skip_recipe(
    db: AsyncSession, tenant_id: UUID, menu_item_id: UUID
) -> dict[str, Any]:
    """Move a recipe to skipped, preserving any existing draft. Creates the recipe
    row if none exists. Returns the resulting detail (no post-commit re-query).
    Caller commits. Raises MenuItemNotFound (404)."""
    mi = (
        await db.execute(
            text("SELECT id, name FROM menu_items WHERE id = :mid AND tenant_id = :tid"),
            {"mid": menu_item_id, "tid": tenant_id},
        )
    ).mappings().fetchone()
    if mi is None:
        raise MenuItemNotFound

    # Upsert recipe status to skipped; recipe_drafts is left untouched (preserved).
    rec = (
        await db.execute(
            text("SELECT id FROM recipes WHERE tenant_id = :tid AND menu_item_id = :mid"),
            {"tid": tenant_id, "mid": menu_item_id},
        )
    ).scalar()
    if rec is None:
        await db.execute(
            text(
                "INSERT INTO recipes (tenant_id, menu_item_id, status)"
                " VALUES (:tid, :mid, 'skipped')"
            ),
            {"tid": tenant_id, "mid": menu_item_id},
        )
    else:
        await db.execute(
            text("UPDATE recipes SET status = 'skipped', updated_at = now() WHERE id = :rid"),
            {"rid": rec},
        )

    # Preserved draft (if any) for the response — read within the same txn.
    draft = (
        await db.execute(
            text(
                "SELECT rd.draft_ingredients FROM recipe_drafts rd"
                " JOIN recipes r ON r.id = rd.recipe_id"
                " WHERE r.tenant_id = :tid AND r.menu_item_id = :mid"
            ),
            {"tid": tenant_id, "mid": menu_item_id},
        )
    ).scalar()
    return {
        "menu_item_id": menu_item_id,
        "name": mi["name"],
        "status": "skipped",
        "ingredients": _as_list(draft),
    }


async def get_progress(db: AsyncSession, tenant_id: UUID) -> dict[str, Any]:
    """confirmed / (active_total - skipped). percent is None when the denominator
    is 0 (no configurable items)."""
    row = (
        await db.execute(
            text("""
                SELECT
                    count(*) FILTER (WHERE mi.active)                              AS total,
                    count(*) FILTER (WHERE mi.active AND r.status = 'confirmed')   AS confirmed,
                    count(*) FILTER (WHERE mi.active AND r.status = 'skipped')     AS skipped
                FROM menu_items mi
                LEFT JOIN recipes r
                    ON r.tenant_id = mi.tenant_id AND r.menu_item_id = mi.id
                WHERE mi.tenant_id = :tid
            """),
            {"tid": tenant_id},
        )
    ).mappings().fetchone()
    total = int(row["total"]) if row else 0
    confirmed = int(row["confirmed"]) if row else 0
    skipped = int(row["skipped"]) if row else 0
    denominator = total - skipped
    percent = round(100.0 * confirmed / denominator, 2) if denominator > 0 else None
    return {
        "total": total,
        "confirmed": confirmed,
        "skipped": skipped,
        "denominator": denominator,
        "percent": percent,
    }
