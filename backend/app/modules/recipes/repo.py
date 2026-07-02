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

from app.modules.inventory.depletion.units import DIMENSION_OF


class MenuItemNotFound(Exception):
    """The menu_item_id does not exist for this tenant (→ 404)."""


class RecipeConfirmed(Exception):
    """The recipe is already confirmed (→ 409). Edits and re-confirms are rejected;
    un-confirm first. (Also the double-click guard: a second confirm of the same
    draft, having lost the FOR UPDATE race, re-reads status='confirmed' and lands
    here — one version is produced, not two.)"""


class RecipeSkipped(Exception):
    """Confirm attempted on a skipped recipe (→ 409). Un-skip (PATCH) first."""


class NoDraft(Exception):
    """Confirm attempted with no draft to confirm (→ 400)."""


class EmptyDraft(Exception):
    """Confirm attempted on a draft with zero ingredients (→ 400)."""


class DuplicateIngredient(Exception):
    """Draft has two ingredients with the same normalized name (→ 400). They would
    dedup to one inventory_item, producing two recipe_ingredients rows for that
    item and a collision on the per-(recipe_version_id, inventory_item_id) base
    idempotency key in the depletion engine. Rejected at the source."""


class UnitTypeConflict(Exception):
    """An existing units_of_measure row for the ingredient's unit has the wrong
    unit_type (→ 409). UNIQUE(tenant_id, name) means there is no second-row escape,
    so the confirm aborts entirely rather than silently attaching a mismatched unit
    (which would corrupt every future conversion for the ingredient)."""


class NotConfirmed(Exception):
    """Un-confirm attempted on a recipe that is not confirmed (→ 409). Also the
    two-un-confirms guard: the second, after the lock, re-reads status='draft' and
    lands here — no second draft, no double-null of recipe_version_id."""


def _as_list(value: Any) -> list[dict[str, Any]]:
    """jsonb may arrive as a str (asyncpg) or already-decoded list."""
    if value is None:
        return []
    if isinstance(value, str):
        return list(json.loads(value))
    return list(value)


def _norm(name: str) -> str:
    """Normalize an ingredient name for dedup/duplicate detection — IDENTICAL to the
    0019 index expression lower(btrim(name)). Draft names are pre-stripped by the
    PATCH validator, so .strip().lower() coincides with lower(btrim(...)) for every
    stored name; the duplicate-rejection here and the dedup index therefore agree on
    what 'the same ingredient' means."""
    return name.strip().lower()


async def list_recipes(
    db: AsyncSession, tenant_id: UUID, *, include_skipped: bool
) -> list[dict[str, Any]]:
    """Active menu items with recipe state, draft ingredient count, modifier
    count, and 30-day sales volume. Skipped recipes hidden unless include_skipped.

    Volume is the 30-day sum(quantity) of active (non-refunded, non-voided) sale
    lines, served by the idx_sli_active_sales partial index (0006)."""
    rows = (
        (
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
        )
        .mappings()
        .all()
    )
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
        (
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
        )
        .mappings()
        .fetchone()
    )
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
        (
            await db.execute(
                text("SELECT id, name FROM menu_items WHERE id = :mid AND tenant_id = :tid"),
                {"mid": menu_item_id, "tid": tenant_id},
            )
        )
        .mappings()
        .fetchone()
    )
    if mi is None:
        raise MenuItemNotFound

    rec = (
        (
            await db.execute(
                text(
                    "SELECT id, status FROM recipes WHERE tenant_id = :tid AND menu_item_id = :mid"
                ),
                {"tid": tenant_id, "mid": menu_item_id},
            )
        )
        .mappings()
        .fetchone()
    )
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


async def skip_recipe(db: AsyncSession, tenant_id: UUID, menu_item_id: UUID) -> dict[str, Any]:
    """Move a recipe to skipped, preserving any existing draft. Creates the recipe
    row if none exists. Returns the resulting detail (no post-commit re-query).
    Caller commits. Skip is for UN-configured items; a confirmed recipe must be
    un-confirmed first (else 'skipped' would coexist with a live
    menu_items.recipe_version_id, which Phase 9 base depletion reads). Raises
    MenuItemNotFound (404) / RecipeConfirmed (409)."""
    mi = (
        (
            await db.execute(
                text("SELECT id, name FROM menu_items WHERE id = :mid AND tenant_id = :tid"),
                {"mid": menu_item_id, "tid": tenant_id},
            )
        )
        .mappings()
        .fetchone()
    )
    if mi is None:
        raise MenuItemNotFound

    # Upsert recipe status to skipped; recipe_drafts is left untouched (preserved).
    rec = (
        (
            await db.execute(
                text(
                    "SELECT id, status FROM recipes WHERE tenant_id = :tid AND menu_item_id = :mid"
                ),
                {"tid": tenant_id, "mid": menu_item_id},
            )
        )
        .mappings()
        .fetchone()
    )
    if rec is None:
        await db.execute(
            text(
                "INSERT INTO recipes (tenant_id, menu_item_id, status)"
                " VALUES (:tid, :mid, 'skipped')"
            ),
            {"tid": tenant_id, "mid": menu_item_id},
        )
    else:
        if rec["status"] == "confirmed":
            raise RecipeConfirmed
        await db.execute(
            text("UPDATE recipes SET status = 'skipped', updated_at = now() WHERE id = :rid"),
            {"rid": rec["id"]},
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
        (
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
        )
        .mappings()
        .fetchone()
    )
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


async def _resolve_inventory_item(db: AsyncSession, tenant_id: UUID, name: str, unit: str) -> UUID:
    """Step 1 of confirm, per ingredient — resolve-or-create the storage unit and
    dedup-or-create the inventory_item, race-safe via existing unique constraints.

    inventory_items.storage_unit_id is NOT NULL (FK → units_of_measure). v5 §8 says
    'create with Mode A default' but the schema forces a unit, so we resolve-or-create
    a units_of_measure row from the ingredient's canonical unit (storage unit ==
    recipe unit, storage_to_recipe_factor defaults 1.0). If a row for that unit name
    already exists with a different unit_type (units_of_measure is dirty with test
    residue), we abort the whole confirm — UNIQUE(tenant_id, name) leaves no second-row
    escape, and silently reusing a mismatched unit corrupts every later conversion.

    Then dedup-or-create the inventory_item on (tenant_id, lower(btrim(name))) via the
    0019 unique index, so two concurrent confirms of a new ingredient converge on one
    row instead of duplicating it."""
    dimension = DIMENSION_OF[unit]  # unit is canonical (PATCH-validated); KeyError = bug

    # resolve-or-create the storage unit (race-safe via UNIQUE(tenant_id, name))
    await db.execute(
        text("""
            INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type)
            VALUES (:tid, :name, :name, :ut)
            ON CONFLICT (tenant_id, name) DO NOTHING
        """),
        {"tid": tenant_id, "name": unit, "ut": dimension},
    )
    uom = (
        (
            await db.execute(
                text(
                    "SELECT id, unit_type FROM units_of_measure"
                    " WHERE tenant_id = :tid AND name = :name"
                ),
                {"tid": tenant_id, "name": unit},
            )
        )
        .mappings()
        .one()
    )
    if uom["unit_type"] != dimension:
        raise UnitTypeConflict(
            f"unit {unit!r} exists for this tenant with unit_type "
            f"{uom['unit_type']!r}, expected {dimension!r}"
        )
    unit_id = uom["id"]

    # dedup-or-create the inventory_item (race-safe via 0019 unique index). Store the
    # display name TRIMMED but CASE-PRESERVED (btrim, not lower) — lower(btrim()) is the
    # *matching* key, but the stored value should read 'Tomato', not 'tomato' or ' Tomato '.
    # Self-defensive: the draft is already PATCH-stripped, but confirm shouldn't rely on it.
    await db.execute(
        text("""
            INSERT INTO inventory_items
                (tenant_id, name, inventory_mode, storage_unit_id, recipe_unit_id)
            VALUES (:tid, btrim(:name), 'recipe_deducted', :uid, :uid)
            ON CONFLICT (tenant_id, lower(btrim(name))) DO NOTHING
        """),
        {"tid": tenant_id, "name": name, "uid": unit_id},
    )
    item_id: UUID = (
        await db.execute(
            text(
                "SELECT id FROM inventory_items"
                " WHERE tenant_id = :tid AND lower(btrim(name)) = lower(btrim(:name))"
            ),
            {"tid": tenant_id, "name": name},
        )
    ).scalar_one()
    return item_id


async def confirm_recipe(db: AsyncSession, tenant_id: UUID, menu_item_id: UUID) -> dict[str, Any]:
    """Confirm a draft into an immutable recipe_version (v5 §7), as ONE transaction.

    Locks the parent recipe row (FOR UPDATE) to serialize confirms of the *same*
    recipe while leaving different recipes parallel, then RE-READS the locked state:
    a confirm that loses the race (double-click, retry, replay) sees status changed
    and returns 409 — one version is produced, never a duplicate version 2.

    Six steps, all-or-nothing (caller commits once; any raise → full rollback,
    leaving no orphan version, ingredients, inventory_items, or units):
      1. per ingredient: resolve-or-create unit + dedup-or-create inventory_item
      2. allocate version_number = MAX+1 (safe under the lock) → recipe_versions
      3. recipe_ingredients (one row per ingredient)
      4. menu_items.recipe_version_id → the new version
      5. recipes.status → 'confirmed'
      6. delete the recipe_draft

    Raises MenuItemNotFound(404) / NoDraft(400) / EmptyDraft(400) /
    DuplicateIngredient(400) / RecipeConfirmed(409) / RecipeSkipped(409) /
    UnitTypeConflict(409)."""
    mi = (
        (
            await db.execute(
                text("SELECT id, name FROM menu_items WHERE id = :mid AND tenant_id = :tid"),
                {"mid": menu_item_id, "tid": tenant_id},
            )
        )
        .mappings()
        .fetchone()
    )
    if mi is None:
        raise MenuItemNotFound

    # Serialize same-recipe confirms; the value read here is authoritative.
    rec = (
        (
            await db.execute(
                text(
                    "SELECT id, status FROM recipes"
                    " WHERE tenant_id = :tid AND menu_item_id = :mid FOR UPDATE"
                ),
                {"tid": tenant_id, "mid": menu_item_id},
            )
        )
        .mappings()
        .fetchone()
    )
    if rec is None:
        raise NoDraft
    if rec["status"] == "confirmed":
        raise RecipeConfirmed
    if rec["status"] == "skipped":
        raise RecipeSkipped
    recipe_id = rec["id"]

    draft = (
        await db.execute(
            text(
                "SELECT draft_ingredients FROM recipe_drafts"
                " WHERE tenant_id = :tid AND recipe_id = :rid"
            ),
            {"tid": tenant_id, "rid": recipe_id},
        )
    ).scalar()
    ingredients = _as_list(draft)
    if not ingredients:  # authoritative ≥1-ingredient check, after the lock
        raise EmptyDraft

    norms = [_norm(str(ing["name"])) for ing in ingredients]
    if len(set(norms)) != len(norms):
        raise DuplicateIngredient

    # step 1 — resolve units + inventory items (side effects roll back on any failure)
    lines: list[tuple[UUID, float, str]] = []
    for ing in ingredients:
        item_id = await _resolve_inventory_item(db, tenant_id, str(ing["name"]), str(ing["unit"]))
        lines.append((item_id, float(ing["quantity"]), str(ing["unit"])))

    # step 2 — allocate version_number under the lock, insert the immutable version
    version_number = (
        await db.execute(
            text(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM recipe_versions"
                " WHERE tenant_id = :tid AND recipe_id = :rid"
            ),
            {"tid": tenant_id, "rid": recipe_id},
        )
    ).scalar_one()
    rv_id = (
        await db.execute(
            text("""
                INSERT INTO recipe_versions
                    (tenant_id, recipe_id, version_number, yield_quantity, name)
                VALUES (:tid, :rid, :vnum, 1, :name)
                RETURNING id
            """),
            {"tid": tenant_id, "rid": recipe_id, "vnum": version_number, "name": mi["name"]},
        )
    ).scalar_one()

    # step 3 — ingredients (explicit tenant_id; RLS depends on it)
    for item_id, quantity, unit in lines:
        await db.execute(
            text("""
                INSERT INTO recipe_ingredients
                    (tenant_id, recipe_version_id, inventory_item_id, quantity, unit)
                VALUES (:tid, :rv, :iid, :qty, :unit)
            """),
            {"tid": tenant_id, "rv": rv_id, "iid": item_id, "qty": quantity, "unit": unit},
        )

    # step 4 — link the menu item to the new version
    await db.execute(
        text("UPDATE menu_items SET recipe_version_id = :rv WHERE id = :mid AND tenant_id = :tid"),
        {"rv": rv_id, "mid": menu_item_id, "tid": tenant_id},
    )

    # step 5 — mark confirmed
    await db.execute(
        text("UPDATE recipes SET status = 'confirmed', updated_at = now() WHERE id = :rid"),
        {"rid": recipe_id},
    )

    # step 6 — drop the draft (inside the txn, so a rollback restores it)
    await db.execute(
        text("DELETE FROM recipe_drafts WHERE tenant_id = :tid AND recipe_id = :rid"),
        {"tid": tenant_id, "rid": recipe_id},
    )

    return {
        "menu_item_id": menu_item_id,
        "name": mi["name"],
        "status": "confirmed",
        "ingredients": [
            {
                "name": str(ing["name"]),
                "quantity": float(ing["quantity"]),
                "unit": str(ing["unit"]),
                "inventory_item_id": str(item_id),
            }
            for ing, (item_id, _q, _u) in zip(ingredients, lines, strict=True)
        ],
    }


async def unconfirm_recipe(
    db: AsyncSession, tenant_id: UUID, menu_item_id: UUID, created_by: UUID
) -> dict[str, Any]:
    """Re-open a confirmed recipe for editing (v5 §7), as ONE transaction, WITHOUT
    mutating any recipe_version.

    Locks the recipe row and re-reads status (a second un-confirm sees 'draft' →
    NotConfirmed). Then: read the confirmed version's ingredients → write a draft copy
    whose parent_recipe_version_id points at that version → status='draft' and clear
    menu_items.recipe_version_id. recipe_versions / recipe_ingredients are never
    touched, so historical sale_line_items keep pointing at a byte-identical version.

    Raises MenuItemNotFound(404) / NotConfirmed(409)."""
    mi = (
        (
            await db.execute(
                text(
                    "SELECT id, name, recipe_version_id FROM menu_items"
                    " WHERE id = :mid AND tenant_id = :tid"
                ),
                {"mid": menu_item_id, "tid": tenant_id},
            )
        )
        .mappings()
        .fetchone()
    )
    if mi is None:
        raise MenuItemNotFound

    rec = (
        (
            await db.execute(
                text(
                    "SELECT id, status FROM recipes"
                    " WHERE tenant_id = :tid AND menu_item_id = :mid FOR UPDATE"
                ),
                {"tid": tenant_id, "mid": menu_item_id},
            )
        )
        .mappings()
        .fetchone()
    )
    if rec is None or rec["status"] != "confirmed":
        raise NotConfirmed
    recipe_id = rec["id"]
    rv_id = mi["recipe_version_id"]

    rows = (
        (
            await db.execute(
                text("""
                SELECT ii.name AS name, ri.quantity AS quantity, ri.unit AS unit,
                       ri.inventory_item_id AS inventory_item_id
                FROM recipe_ingredients ri
                JOIN inventory_items ii ON ii.id = ri.inventory_item_id
                WHERE ri.tenant_id = :tid AND ri.recipe_version_id = :rv
                ORDER BY ii.name
            """),
                {"tid": tenant_id, "rv": rv_id},
            )
        )
        .mappings()
        .all()
    )
    draft_ingredients = [
        {
            "name": r["name"],
            "quantity": float(r["quantity"]),
            "unit": r["unit"],
            "inventory_item_id": str(r["inventory_item_id"]),
        }
        for r in rows
    ]

    await db.execute(
        text("""
            INSERT INTO recipe_drafts
                (tenant_id, recipe_id, parent_recipe_version_id, draft_ingredients, created_by)
            VALUES (:tid, :rid, :rv, CAST(:di AS jsonb), :uid)
            ON CONFLICT (tenant_id, recipe_id) DO UPDATE
                SET parent_recipe_version_id = :rv,
                    draft_ingredients = CAST(:di AS jsonb),
                    updated_at = now()
        """),
        {
            "tid": tenant_id,
            "rid": recipe_id,
            "rv": rv_id,
            "di": json.dumps(draft_ingredients),
            "uid": created_by,
        },
    )
    await db.execute(
        text("UPDATE recipes SET status = 'draft', updated_at = now() WHERE id = :rid"),
        {"rid": recipe_id},
    )
    await db.execute(
        text("UPDATE menu_items SET recipe_version_id = NULL WHERE id = :mid AND tenant_id = :tid"),
        {"mid": menu_item_id, "tid": tenant_id},
    )

    return {
        "menu_item_id": menu_item_id,
        "name": mi["name"],
        "status": "draft",
        "ingredients": draft_ingredients,
    }
