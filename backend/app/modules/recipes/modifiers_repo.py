"""Data access for modifier configuration (Sprint 5 Phase 6).

Modifier confirm = recipe confirm with the nouns swapped (modifiers / modifier_versions
/ modifier_ingredients / modifier_drafts), so the machinery is reused, not re-invented:
  - the inventory resolve-or-create + dedup + unit_type-conflict abort is the SAME
    _resolve_inventory_item (tenant-scoped, not recipe-specific);
  - FOR UPDATE on the parent + re-read-status-inside-the-lock (double-click → 409, one
    version), MAX+1 allocation, atomic confirm, immutable versions, explicit tenant_id,
    duplicate-normalized-name rejection — all carried over.

Three modifier-specific divergences:
  1. parent-menu-item scoping: every op verifies modifier ∈ (tenant, menu_item) → 404;
     the link is modifiers.current_version_id, set THROUGH the composite FK
     (id, current_version_id) → modifier_versions(modifier_id, id), never bypassed.
  2. additive-only confirmability: only modifier_type='additive' can confirm into a
     depleting version (Phase 10 walks confirmed additive only); else → 409.
  3. ≥1-ingredient precondition (also a DB CHECK on modifier_ingredients.quantity).

Like Phase 5's boundary: a modifier_draft is written by the operator's PATCH, never by
the suggestion service (which is append-only to modifier_llm_suggestions).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.inventory.item_resolver import resolve_inventory_item
from app.modules.recipes.repo import (
    DuplicateIngredient,
    EmptyDraft,
    NoDraft,
    _as_list,
    _norm,
)


class ModifierNotFound(Exception):
    """No modifier with that id for this tenant AND menu item (→ 404, no leak)."""


class ModifierConfirmed(Exception):
    """The modifier is already confirmed (→ 409). PATCH/confirm rejected; un-confirm
    first. Also the double-click guard via the post-lock status re-read."""


class ModifierSkipped(Exception):
    """Confirm attempted on a skipped modifier (→ 409). PATCH (un-skip) first."""


class NonAdditiveModifier(Exception):
    """Confirm attempted on a non-additive modifier (→ 409). Only additive modifiers
    deplete (Phase 10); subtractive/substitution are out of v5 scope — skip them."""


class ModifierNotConfirmed(Exception):
    """Un-confirm attempted on a modifier that is not confirmed (→ 409)."""


async def _require_modifier(
    db: AsyncSession, tenant_id: UUID, menu_item_id: UUID, modifier_id: UUID
) -> dict[str, Any]:
    """Fetch a modifier scoped to (tenant, menu_item) — guard 1. Raises ModifierNotFound."""
    row = (
        (
            await db.execute(
                text(
                    "SELECT id, name, modifier_type, status, current_version_id FROM modifiers"
                    " WHERE id = :mod AND tenant_id = :tid AND menu_item_id = :mid"
                ),
                {"mod": modifier_id, "tid": tenant_id, "mid": menu_item_id},
            )
        )
        .mappings()
        .fetchone()
    )
    if row is None:
        raise ModifierNotFound
    return dict(row)


def _detail(mod: dict[str, Any], status: str, ingredients: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "modifier_id": mod["id"],
        "name": mod["name"],
        "modifier_type": mod["modifier_type"],
        "status": status,
        "ingredients": ingredients,
    }


async def list_modifiers(
    db: AsyncSession, tenant_id: UUID, menu_item_id: UUID
) -> list[dict[str, Any]]:
    """All modifiers for a menu item with state + draft ingredient count. Raises
    ModifierNotFound if the menu item isn't this tenant's (→ 404)."""
    mi = (
        await db.execute(
            text("SELECT id FROM menu_items WHERE id = :mid AND tenant_id = :tid"),
            {"mid": menu_item_id, "tid": tenant_id},
        )
    ).scalar()
    if mi is None:
        raise ModifierNotFound

    rows = (
        (
            await db.execute(
                text("""
                SELECT m.id AS modifier_id, m.name AS name, m.modifier_type AS modifier_type,
                       m.status AS status,
                       COALESCE(jsonb_array_length(md.draft_ingredients), 0) AS ingredient_count
                FROM modifiers m
                LEFT JOIN modifier_drafts md
                    ON md.tenant_id = m.tenant_id AND md.modifier_id = m.id
                WHERE m.tenant_id = :tid AND m.menu_item_id = :mid
                ORDER BY m.name
            """),
                {"tid": tenant_id, "mid": menu_item_id},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "modifier_id": r["modifier_id"],
            "name": r["name"],
            "modifier_type": r["modifier_type"],
            "status": r["status"],
            "ingredient_count": int(r["ingredient_count"]),
        }
        for r in rows
    ]


async def get_modifier(
    db: AsyncSession, tenant_id: UUID, menu_item_id: UUID, modifier_id: UUID
) -> dict[str, Any]:
    """Single modifier's state + draft ingredients (Sprint 5 Phase 16). A PURE READ — the
    read-only partner of the recipe-detail GET, so the modifier config UI can read a draft
    back to display/edit it (and re-hydrate after app-close) instead of round-tripping through
    a PATCH (which would implicitly unskip a skipped modifier and 409 a confirmed one).

    Parent-scoped via _require_modifier (guard 1): a modifier of a DIFFERENT menu item, or
    another tenant's, raises ModifierNotFound (→ 404) — the composite-FK boundary applies to
    reads too. Touches no row: GET on a skipped/confirmed modifier leaves its status untouched.
    Mirrors get_recipe — returns the DRAFT ingredients (empty once confirmed; the operator
    un-confirms to edit, which copies the version back into a draft)."""
    mod = await _require_modifier(db, tenant_id, menu_item_id, modifier_id)
    draft = (
        await db.execute(
            text(
                "SELECT draft_ingredients FROM modifier_drafts"
                " WHERE modifier_id = :mod AND tenant_id = :tid"
            ),
            {"mod": modifier_id, "tid": tenant_id},
        )
    ).scalar()
    return _detail(mod, str(mod["status"]), _as_list(draft))


async def save_draft(
    db: AsyncSession,
    tenant_id: UUID,
    menu_item_id: UUID,
    modifier_id: UUID,
    ingredients: list[dict[str, Any]],
    created_by: UUID,
) -> dict[str, Any]:
    """Auto-save a modifier draft — the inherited four-way branch on status:
    no-draft → create; draft → update; skipped → update + reopen to draft; confirmed → 409.
    Touches only modifier_drafts (+ status flip). Caller commits.

    Raises ModifierNotFound (404) / ModifierConfirmed (409)."""
    mod = await _require_modifier(db, tenant_id, menu_item_id, modifier_id)
    if mod["status"] == "confirmed":
        raise ModifierConfirmed
    if mod["status"] == "skipped":  # editing a skipped modifier reopens it
        await db.execute(
            text("UPDATE modifiers SET status = 'draft', updated_at = now() WHERE id = :mod"),
            {"mod": modifier_id},
        )
    # no-draft → INSERT, existing draft → UPDATE (the four-way's create/update cases)
    await db.execute(
        text("""
            INSERT INTO modifier_drafts (tenant_id, modifier_id, draft_ingredients, created_by)
            VALUES (:tid, :mod, CAST(:di AS jsonb), :uid)
            ON CONFLICT (tenant_id, modifier_id)
            DO UPDATE SET draft_ingredients = CAST(:di AS jsonb), updated_at = now()
        """),
        {"tid": tenant_id, "mod": modifier_id, "di": json.dumps(ingredients), "uid": created_by},
    )
    return _detail(mod, "draft", ingredients)


async def skip_modifier(
    db: AsyncSession, tenant_id: UUID, menu_item_id: UUID, modifier_id: UUID
) -> dict[str, Any]:
    """Mark a modifier skipped, preserving any draft. The only disposition for
    non-additive modifiers (which can't confirm). Skip is for UN-configured modifiers;
    a confirmed one must be un-confirmed first (else 'skipped' would coexist with a live
    current_version_id). Raises ModifierNotFound (404) / ModifierConfirmed (409)."""
    mod = await _require_modifier(db, tenant_id, menu_item_id, modifier_id)
    if mod["status"] == "confirmed":
        raise ModifierConfirmed
    await db.execute(
        text("UPDATE modifiers SET status = 'skipped', updated_at = now() WHERE id = :mod"),
        {"mod": modifier_id},
    )
    draft = (
        await db.execute(
            text("SELECT draft_ingredients FROM modifier_drafts WHERE modifier_id = :mod"),
            {"mod": modifier_id},
        )
    ).scalar()
    return _detail(mod, "skipped", _as_list(draft))


async def confirm_modifier(
    db: AsyncSession,
    tenant_id: UUID,
    menu_item_id: UUID,
    modifier_id: UUID,
    created_by: UUID,
) -> dict[str, Any]:
    """Confirm a modifier draft into an immutable modifier_version, as ONE transaction.

    Raises ModifierNotFound(404) / NoDraft(400) / EmptyDraft(400) / DuplicateIngredient(400)
    / ModifierConfirmed(409) / ModifierSkipped(409) / NonAdditiveModifier(409) /
    UnitTypeConflict(409)."""
    mod = await _require_modifier(db, tenant_id, menu_item_id, modifier_id)

    # serialize same-modifier confirms; the value read here is authoritative
    locked = (
        (
            await db.execute(
                text(
                    "SELECT status, modifier_type FROM modifiers"
                    " WHERE id = :mod AND tenant_id = :tid FOR UPDATE"
                ),
                {"mod": modifier_id, "tid": tenant_id},
            )
        )
        .mappings()
        .one()
    )
    if locked["status"] == "confirmed":
        raise ModifierConfirmed
    if locked["status"] == "skipped":
        raise ModifierSkipped
    if locked["modifier_type"] != "additive":  # guard 2
        raise NonAdditiveModifier

    draft = (
        await db.execute(
            text("SELECT draft_ingredients FROM modifier_drafts WHERE modifier_id = :mod"),
            {"mod": modifier_id},
        )
    ).scalar()
    ingredients = _as_list(draft)
    if not ingredients:  # guard 3 (authoritative, post-lock)
        raise EmptyDraft if draft is not None else NoDraft

    norms = [_norm(str(i["name"])) for i in ingredients]
    if len(set(norms)) != len(norms):
        raise DuplicateIngredient

    # step 1 — resolve units + inventory items (shared helper; side effects roll back)
    lines: list[tuple[UUID, float, str]] = []
    for ing in ingredients:
        item_id = await resolve_inventory_item(db, tenant_id, str(ing["name"]), str(ing["unit"]))
        lines.append((item_id, float(ing["quantity"]), str(ing["unit"])))

    # step 2 — version_number = MAX+1 under the lock; immutable modifier_version
    version_number = (
        await db.execute(
            text(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM modifier_versions"
                " WHERE tenant_id = :tid AND modifier_id = :mod"
            ),
            {"tid": tenant_id, "mod": modifier_id},
        )
    ).scalar_one()
    mv_id = (
        await db.execute(
            text("""
                INSERT INTO modifier_versions
                    (tenant_id, modifier_id, version_number, yield_quantity, created_by)
                VALUES (:tid, :mod, :vnum, 1, :uid)
                RETURNING id
            """),
            {"tid": tenant_id, "mod": modifier_id, "vnum": version_number, "uid": created_by},
        )
    ).scalar_one()

    # step 3 — ingredients (explicit tenant_id)
    for item_id, quantity, unit in lines:
        await db.execute(
            text("""
                INSERT INTO modifier_ingredients
                    (tenant_id, modifier_version_id, inventory_item_id, quantity, unit)
                VALUES (:tid, :mv, :iid, :qty, :unit)
            """),
            {"tid": tenant_id, "mv": mv_id, "iid": item_id, "qty": quantity, "unit": unit},
        )

    # step 4+5 — link via the composite FK (mv_id belongs to this modifier) + confirm
    await db.execute(
        text(
            "UPDATE modifiers SET current_version_id = :mv, status = 'confirmed',"
            " updated_at = now() WHERE id = :mod"
        ),
        {"mv": mv_id, "mod": modifier_id},
    )

    # step 6 — drop the draft (in-txn, so rollback restores it)
    await db.execute(
        text("DELETE FROM modifier_drafts WHERE tenant_id = :tid AND modifier_id = :mod"),
        {"tid": tenant_id, "mod": modifier_id},
    )

    return _detail(
        mod,
        "confirmed",
        [
            {
                "name": str(ing["name"]),
                "quantity": float(ing["quantity"]),
                "unit": str(ing["unit"]),
                "inventory_item_id": str(item_id),
            }
            for ing, (item_id, _q, _u) in zip(ingredients, lines, strict=True)
        ],
    )


async def unconfirm_modifier(
    db: AsyncSession,
    tenant_id: UUID,
    menu_item_id: UUID,
    modifier_id: UUID,
    created_by: UUID,
) -> dict[str, Any]:
    """Re-open a confirmed modifier into a draft copy WITHOUT mutating any version.
    Raises ModifierNotFound (404) / ModifierNotConfirmed (409)."""
    mod = await _require_modifier(db, tenant_id, menu_item_id, modifier_id)
    locked = (
        (
            await db.execute(
                text(
                    "SELECT status, current_version_id FROM modifiers"
                    " WHERE id = :mod AND tenant_id = :tid FOR UPDATE"
                ),
                {"mod": modifier_id, "tid": tenant_id},
            )
        )
        .mappings()
        .one()
    )
    if locked["status"] != "confirmed":
        raise ModifierNotConfirmed
    mv_id = locked["current_version_id"]

    rows = (
        (
            await db.execute(
                text("""
                SELECT ii.name AS name, mi.quantity AS quantity, mi.unit AS unit,
                       mi.inventory_item_id AS inventory_item_id
                FROM modifier_ingredients mi
                JOIN inventory_items ii ON ii.id = mi.inventory_item_id
                WHERE mi.tenant_id = :tid AND mi.modifier_version_id = :mv
                ORDER BY ii.name
            """),
                {"tid": tenant_id, "mv": mv_id},
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
            INSERT INTO modifier_drafts
                (tenant_id, modifier_id, parent_modifier_version_id, draft_ingredients, created_by)
            VALUES (:tid, :mod, :mv, CAST(:di AS jsonb), :uid)
            ON CONFLICT (tenant_id, modifier_id) DO UPDATE
                SET parent_modifier_version_id = :mv,
                    draft_ingredients = CAST(:di AS jsonb),
                    updated_at = now()
        """),
        {
            "tid": tenant_id,
            "mod": modifier_id,
            "mv": mv_id,
            "di": json.dumps(draft_ingredients),
            "uid": created_by,
        },
    )
    # current_version_id → NULL is allowed: composite FK is MATCH SIMPLE (NULL skips check)
    await db.execute(
        text(
            "UPDATE modifiers SET status = 'draft', current_version_id = NULL,"
            " updated_at = now() WHERE id = :mod"
        ),
        {"mod": modifier_id},
    )
    return _detail(mod, "draft", draft_ingredients)
