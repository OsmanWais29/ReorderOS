"""Shared inventory-item resolution (Sprint 6 S1).

Home of `resolve_inventory_item` — resolve-or-create an inventory_item (and its
storage unit) from a name + canonical unit — extracted VERBATIM from
`recipes/repo.py` so the recipe/modifier confirm path and the new receipts review
path share one implementation instead of forking. `recipes.repo` re-exports it
(as `_resolve_inventory_item`) and `UnitTypeConflict`, so every existing caller and
import is unchanged (this move is behavior-preserving — proven by the unchanged
Sprint 5 confirm suite).

Also adds `suggest_inventory_items` for the receipt review UI: a ranked
SUGGESTION helper (never an auto-match) so an operator linking an extracted line
sees the closest existing items first. Suggestion is advisory — the operator's
explicit link is what sets the item and `match_status` (D-606-26).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.inventory.depletion.units import DIMENSION_OF


class UnitTypeConflict(Exception):
    """An existing units_of_measure row for the ingredient's unit has the wrong
    unit_type (→ 409). UNIQUE(tenant_id, name) means there is no second-row escape,
    so the confirm aborts entirely rather than silently attaching a mismatched unit
    (which would corrupt every future conversion for the ingredient)."""


async def resolve_unit(db: AsyncSession, tenant_id: UUID, unit: str) -> UUID:
    """Resolve-or-create the units_of_measure row for a canonical unit string
    (race-safe via UNIQUE(tenant_id, name)); UnitTypeConflict when the existing
    row carries the wrong dimension. Extracted from resolve_inventory_item so
    item-unit correction shares the exact same semantics."""
    dimension = DIMENSION_OF[unit]  # unit must be canonical; KeyError = caller bug
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
    unit_id: UUID = uom["id"]
    return unit_id


async def resolve_inventory_item(db: AsyncSession, tenant_id: UUID, name: str, unit: str) -> UUID:
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
    # resolve-or-create the storage unit — shared helper (same race-safety +
    # UnitTypeConflict semantics as before the extraction).
    unit_id = await resolve_unit(db, tenant_id, unit)

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


async def suggest_inventory_items(
    db: AsyncSession, tenant_id: UUID, name: str, *, limit: int = 5
) -> list[dict[str, Any]]:
    """Rank existing inventory items by name similarity to an extracted line name —
    a SUGGESTION for the receipt review UI, never an auto-match (D-606-26: the item
    is set only by an explicit operator link).

    Ranking is deterministic and dependency-free: exact normalized match first, then
    prefix, then substring (containment either direction), tie-broken by name. Scoped
    by tenant_id explicitly (not RLS alone), consistent with the rest of this module.
    Returns active items only — you don't reorder into a deactivated item.
    """
    rows = await db.execute(
        text("""
            SELECT id, name, storage_unit_id, par_level
              FROM inventory_items
             WHERE tenant_id = :tid
               AND active = true
               AND (
                     lower(btrim(name)) = lower(btrim(:q))
                  OR lower(btrim(name)) LIKE lower(btrim(:q)) || '%'
                  OR position(lower(btrim(:q)) in lower(btrim(name))) > 0
                  OR position(lower(btrim(name)) in lower(btrim(:q))) > 0
               )
             ORDER BY
                 CASE
                     WHEN lower(btrim(name)) = lower(btrim(:q))                 THEN 0
                     WHEN lower(btrim(name)) LIKE lower(btrim(:q)) || '%'       THEN 1
                     WHEN position(lower(btrim(:q)) in lower(btrim(name))) > 0  THEN 2
                     ELSE 3
                 END,
                 name
             LIMIT :lim
        """),
        {"tid": tenant_id, "q": name, "lim": limit},
    )
    return [dict(r) for r in rows.mappings().all()]
