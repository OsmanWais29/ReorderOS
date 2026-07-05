"""Sprint 5 shared test seed helpers.

TEST-ONLY — never import this from app code.

Builds the post-0016 *valid* chain ``menu_item -> recipe -> recipe_version``
(+ optional ``recipe_ingredients``) with ``recipe_id`` / ``version_number`` /
``yield_quantity`` populated and a canonical ``unit`` on every ingredient, so
fixtures satisfy the tightened Sprint-5 schema. Replaces the pre-Sprint-5 stub
inserts (``INSERT INTO recipe_versions (tenant_id, name)``) that violate the
0016 NOT NULL / canonical-unit constraints.

Two variants because the suite uses two connection styles:
  - ``seed_recipe_version``          — asyncpg ``Connection`` (admin_conn etc.)
  - ``seed_recipe_version_session``  — SQLAlchemy ``AsyncSession``

``version_number`` auto-increments per ``recipe_id`` (or is taken explicitly), so
repeated calls can never collide with ``UNIQUE(recipe_id, version_number)``.
``status='confirmed'`` (default) produces the minimum valid *confirmed* chain:
``recipes.status='confirmed'`` and ``menu_items.recipe_version_id`` wired.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any, NamedTuple

from sqlalchemy import text

from app.modules.inventory.depletion.writer import write_movement

# Canonical default unit — must be in the 0016 recipe_ingredients CHECK allowlist.
DEFAULT_UNIT = "g"


class SeededRecipe(NamedTuple):
    recipe_version_id: str
    recipe_id: str
    menu_item_id: str
    ingredient_ids: list[str]


def _u(v: Any) -> uuid.UUID:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


def _norm_ingredients(
    ingredients: Iterable[Sequence[Any]],
) -> list[tuple[uuid.UUID, Any, str]]:
    """Normalise (item_id, qty) or (item_id, qty, unit) tuples → (uuid, qty, unit)."""
    out: list[tuple[uuid.UUID, Any, str]] = []
    for spec in ingredients:
        item_id, qty = spec[0], spec[1]
        unit = spec[2] if len(spec) > 2 else DEFAULT_UNIT
        out.append((_u(item_id), qty, unit))
    return out


async def seed_recipe_version(
    conn: Any,
    tenant_id: Any,
    *,
    ingredients: Iterable[Sequence[Any]] = (),
    version_number: int | None = None,
    yield_quantity: float = 1.0,
    status: str = "confirmed",
) -> SeededRecipe:
    """asyncpg: create a 0016-valid menu_item -> recipe -> recipe_version chain.

    ``ingredients``: iterable of ``(inventory_item_id, qty)`` or
    ``(inventory_item_id, qty, unit)``; ``unit`` defaults to 'g' (canonical).
    Returns a SeededRecipe with all ids as strings.
    """
    tid = _u(tenant_id)
    mi_id = await conn.fetchval(
        "INSERT INTO menu_items (tenant_id, name) VALUES ($1, $2) RETURNING id",
        tid,
        f"mi-{uuid.uuid4().hex[:8]}",
    )
    recipe_id = await conn.fetchval(
        "INSERT INTO recipes (tenant_id, menu_item_id, status) VALUES ($1, $2, $3) RETURNING id",
        tid,
        mi_id,
        status,
    )
    if version_number is None:
        version_number = await conn.fetchval(
            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM recipe_versions WHERE recipe_id = $1",
            recipe_id,
        )
    rv_id = await conn.fetchval(
        "INSERT INTO recipe_versions"
        " (tenant_id, recipe_id, version_number, yield_quantity, name)"
        " VALUES ($1, $2, $3, $4, $5) RETURNING id",
        tid,
        recipe_id,
        version_number,
        yield_quantity,
        f"rv-{uuid.uuid4().hex[:6]}",
    )
    ingredient_ids: list[str] = []
    for item_id, qty, unit in _norm_ingredients(ingredients):
        ri_id = await conn.fetchval(
            "INSERT INTO recipe_ingredients"
            " (tenant_id, recipe_version_id, inventory_item_id, quantity, unit)"
            " VALUES ($1, $2, $3, $4, $5) RETURNING id",
            tid,
            rv_id,
            item_id,
            qty,
            unit,
        )
        ingredient_ids.append(str(ri_id))
    if status == "confirmed":
        await conn.execute(
            "UPDATE menu_items SET recipe_version_id = $1 WHERE id = $2", rv_id, mi_id
        )
    return SeededRecipe(str(rv_id), str(recipe_id), str(mi_id), ingredient_ids)


async def seed_recipe_version_session(
    session: Any,
    tenant_id: Any,
    *,
    ingredients: Iterable[Sequence[Any]] = (),
    version_number: int | None = None,
    yield_quantity: float = 1.0,
    status: str = "confirmed",
    recipe_version_id: Any | None = None,
) -> SeededRecipe:
    """SQLAlchemy AsyncSession variant of :func:`seed_recipe_version`.

    ``recipe_version_id``: optionally force the recipe_version id (some tests
    pre-generate fixed ids). When the forced id already exists this is a no-op
    that returns the existing chain — preserving the ``ON CONFLICT DO NOTHING``
    idempotency the old fixtures relied on against the no-teardown dev DB.
    """
    tid = _u(tenant_id)

    if recipe_version_id is not None:
        existing = (
            await session.execute(
                text("SELECT recipe_id FROM recipe_versions WHERE id = :id"),
                {"id": _u(recipe_version_id)},
            )
        ).scalar()
        if existing is not None:
            return SeededRecipe(str(recipe_version_id), str(existing), "", [])

    mi_id = (
        await session.execute(
            text("INSERT INTO menu_items (tenant_id, name) VALUES (:tid, :name) RETURNING id"),
            {"tid": tid, "name": f"mi-{uuid.uuid4().hex[:8]}"},
        )
    ).scalar_one()
    recipe_id = (
        await session.execute(
            text(
                "INSERT INTO recipes (tenant_id, menu_item_id, status)"
                " VALUES (:tid, :mi, :st) RETURNING id"
            ),
            {"tid": tid, "mi": mi_id, "st": status},
        )
    ).scalar_one()
    if version_number is None:
        version_number = (
            await session.execute(
                text(
                    "SELECT COALESCE(MAX(version_number), 0) + 1"
                    " FROM recipe_versions WHERE recipe_id = :rid"
                ),
                {"rid": recipe_id},
            )
        ).scalar_one()
    rv_id = _u(recipe_version_id) if recipe_version_id is not None else uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO recipe_versions"
            " (id, tenant_id, recipe_id, version_number, yield_quantity, name)"
            " VALUES (:id, :tid, :rid, :vn, :yq, :name)"
        ),
        {
            "id": rv_id,
            "tid": tid,
            "rid": recipe_id,
            "vn": version_number,
            "yq": yield_quantity,
            "name": f"rv-{str(rv_id)[-6:]}",
        },
    )
    ingredient_ids: list[str] = []
    for item_id, qty, unit in _norm_ingredients(ingredients):
        ri_id = (
            await session.execute(
                text(
                    "INSERT INTO recipe_ingredients"
                    " (tenant_id, recipe_version_id, inventory_item_id, quantity, unit)"
                    " VALUES (:tid, :rv, :iid, :qty, :unit) RETURNING id"
                ),
                {"tid": tid, "rv": rv_id, "iid": item_id, "qty": qty, "unit": unit},
            )
        ).scalar_one()
        ingredient_ids.append(str(ri_id))
    if status == "confirmed":
        await session.execute(
            text("UPDATE menu_items SET recipe_version_id = :rv WHERE id = :mi"),
            {"rv": rv_id, "mi": mi_id},
        )
    await session.flush()
    return SeededRecipe(str(rv_id), str(recipe_id), str(mi_id), ingredient_ids)


async def seed_sale_effect_session(
    session: Any,
    *,
    tenant_id: Any,
    sale_line_item_id: Any,
    inventory_item_id: Any,
    recorded_at: Any | None = None,
) -> Any:
    """TEST-ONLY drop-in for the retired record_sale_inventory_effect.

    Reproduces its forward sale movement (old formula = sale_qty*recipe_qty/factor, old key
    sale_line:{sli}:{ii}, yield_factor snapshot) via write_movement — for tests that need a
    sale movement as SETUP to exercise surviving behavior (reversal, yield snapshot,
    watermark, on_hand). Late-signal is intentionally NOT here (it now lives in the handler;
    H0.9/H0.10 drive process_line instead). Returns the movement id (None on replay/conflict).
    """
    tid, sli, iid = _u(tenant_id), _u(sale_line_item_id), _u(inventory_item_id)
    row = (
        (
            await session.execute(
                text("""
                SELECT s.quantity AS sq, ri.quantity AS rq,
                       ii.storage_to_recipe_factor AS f, ii.inventory_mode AS m
                FROM sale_line_items s
                JOIN recipe_ingredients ri
                  ON ri.recipe_version_id = s.recipe_version_id
                 AND ri.inventory_item_id = :iid AND ri.tenant_id = :tid
                JOIN inventory_items ii ON ii.id = :iid AND ii.tenant_id = :tid
                WHERE s.id = :sli AND s.tenant_id = :tid
            """),
                {"tid": tid, "sli": sli, "iid": iid},
            )
        )
        .mappings()
        .one()
    )
    theoretical = Decimal(str(row["sq"])) * Decimal(str(row["rq"])) / Decimal(str(row["f"]))
    yf = (
        await session.execute(
            text(
                "SELECT yield_factor FROM inventory_yield_factors"
                " WHERE tenant_id = :t AND inventory_item_id = :i"
            ),
            {"t": tid, "i": iid},
        )
    ).scalar()
    yield_factor = Decimal(str(yf)) if yf is not None else Decimal("1.0")
    if row["m"] == "recipe_deducted":
        mtype, delta = "sale_depletion", -theoretical
    else:
        mtype, delta = "sale_signal", theoretical
    return await write_movement(
        session,
        tenant_id=tid,
        idempotency_key=f"sale_line:{sli}:{iid}",
        legacy_key=None,
        inventory_item_id=iid,
        movement_type=mtype,
        delta=delta,
        yield_factor_applied=yield_factor,
        source_type="sale_line_item",
        source_id=sli,
        recorded_at=recorded_at,
    )
