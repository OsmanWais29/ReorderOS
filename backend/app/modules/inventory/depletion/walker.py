"""Recipe ingredient walker (Sprint 5 Phase 9, v5 §11) — base recipe side.

COMPUTE ONLY — walks the FROZEN sale_line_items.recipe_version_id, converts each
ingredient to its inventory item's storage unit, aggregates per item, and returns either
the planned movements or a non-deplete verdict. It writes nothing; the handler commits.

Why the frozen snapshot, not the current recipe: re-reading recipes.status at walk time
would let an operator un-confirming a recipe AFTER a sale retroactively change that sale's
ledger — violating the historical-immutability guarantee (gate #25). The snapshot is what
makes a past sale immune to future edits; we deplete against it, full stop. The
confirmed-status gate lives at ingestion (the snapshot step), not here.

Formula (v5 §11), per ingredient, then summed per (inventory_item):
    storage_qty = convert(recipe_ingredient.quantity, recipe_unit -> item storage_unit)
    magnitude   = line_quantity * storage_qty / recipe_versions.yield_quantity
    Mode A (recipe_deducted) -> sale_depletion, delta = -magnitude
    Mode B (count_anchored)  -> sale_signal,    delta = +magnitude

Aggregation (correction): two ingredient rows resolving to one inventory_item would share
the base key (sale_line:{sli}:base:{rv}:{item}); writing both would collide and under-
deplete. Summing per item before writing makes the walker robust regardless of whether
Phase 4's write-side duplicate rejection ever has a gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.inventory.depletion.conversions import ConversionError, convert


@dataclass(frozen=True)
class PlannedMovement:
    inventory_item_id: UUID
    movement_type: str  # sale_depletion | sale_signal
    delta: Decimal
    yield_factor_applied: Decimal
    idempotency_key: str
    legacy_key: str | None
    source_type: str  # sale_line_item (base) | sale_line_item_modifier
    source_id: UUID  # sale_line_item_id (base) | sale_line_item_modifier_id


@dataclass(frozen=True)
class WalkOutcome:
    """Either movements to write (status='depleted', reason=None) or a non-deplete
    verdict (status in unmapped/skipped/failed, reason set, movements empty)."""

    movements: list[PlannedMovement]
    status: str
    reason: str | None


def _no_deplete(status: str, reason: str) -> WalkOutcome:
    return WalkOutcome(movements=[], status=status, reason=reason)


async def walk_base(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    sale_line_item_id: UUID,
    recipe_version_id: UUID | None,
    line_quantity: Decimal,
) -> WalkOutcome:
    """Plan the base-recipe movements for one (eligible) sale line. Compute only."""
    if recipe_version_id is None:
        # No confirmed recipe version was frozen at ingestion → nothing to deplete. The
        # worker (service_worker) cannot read the operator-owned recipes table, so it does
        # NOT distinguish never-created / draft / skipped here — that finer breakdown is a
        # reporting concern (the coverage view, operator-readable). The honest depletion-side
        # status is unmapped/no_recipe.
        return _no_deplete("unmapped", "no_recipe")

    yield_quantity = (
        await session.execute(
            text("SELECT yield_quantity FROM recipe_versions WHERE id = :rv AND tenant_id = :tid"),
            {"rv": recipe_version_id, "tid": tenant_id},
        )
    ).scalar()
    if yield_quantity is None:
        return _no_deplete("failed", "invalid_recipe")
    yield_q = Decimal(str(yield_quantity))

    rows = (
        (
            await session.execute(
                text("""
                SELECT ri.inventory_item_id AS iid,
                       ri.quantity          AS recipe_qty,
                       ri.unit              AS recipe_unit,
                       ii.inventory_mode    AS mode,
                       uom.name             AS storage_unit
                FROM recipe_ingredients ri
                JOIN inventory_items ii ON ii.id = ri.inventory_item_id
                JOIN units_of_measure uom ON uom.id = ii.storage_unit_id
                WHERE ri.recipe_version_id = :rv AND ri.tenant_id = :tid
            """),
                {"rv": recipe_version_id, "tid": tenant_id},
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return _no_deplete("failed", "invalid_recipe")

    # aggregate per inventory item (sum deltas; one mode per item)
    agg: dict[UUID, dict[str, object]] = {}
    for r in rows:
        iid = r["iid"]
        recipe_unit = str(r["recipe_unit"])
        storage_unit = str(r["storage_unit"])
        try:
            storage_qty = await convert(
                session,
                Decimal(str(r["recipe_qty"])),
                recipe_unit,
                storage_unit,
                inventory_item_id=iid,
                tenant_id=tenant_id,
            )
        except ConversionError:
            # any missing conversion fails the whole line — no movements written
            return _no_deplete("failed", "missing_conversion")

        magnitude = line_quantity * storage_qty / yield_q
        if r["mode"] == "recipe_deducted":
            mtype, signed = "sale_depletion", -magnitude
        else:
            mtype, signed = "sale_signal", magnitude

        yf = (
            await session.execute(
                text(
                    "SELECT yield_factor FROM inventory_yield_factors"
                    " WHERE tenant_id = :tid AND inventory_item_id = :iid"
                ),
                {"tid": tenant_id, "iid": iid},
            )
        ).scalar()
        yield_factor = Decimal(str(yf)) if yf is not None else Decimal("1.0")

        if iid in agg:
            agg[iid]["delta"] = Decimal(str(agg[iid]["delta"])) + signed
        else:
            agg[iid] = {"mtype": mtype, "delta": signed, "yf": yield_factor}

    movements = [
        PlannedMovement(
            inventory_item_id=iid,
            movement_type=str(v["mtype"]),
            delta=Decimal(str(v["delta"])),
            yield_factor_applied=Decimal(str(v["yf"])),
            idempotency_key=f"sale_line:{sale_line_item_id}:base:{recipe_version_id}:{iid}",
            legacy_key=f"sale_line:{sale_line_item_id}:{iid}",
            source_type="sale_line_item",
            source_id=sale_line_item_id,
        )
        for iid, v in agg.items()
    ]
    return WalkOutcome(movements=movements, status="depleted", reason=None)


async def walk_modifiers(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    sale_line_item_id: UUID,
    line_quantity: Decimal,
) -> WalkOutcome:
    """Plan the modifier movements for one (eligible) sale line (Sprint 5 Phase 10).

    Reads the FROZEN sale_line_item_modifiers snapshot (written at ingestion only for
    confirmed additive modifiers). Per modifier ingredient:
        magnitude = line_qty * slim.quantity
                    * (mod_ingredient.qty / modifier_versions.yield_quantity) * convert
    Mode A -> sale_depletion (negative); Mode B -> sale_signal (positive). Key includes the
    slim id + modifier_version_id so it never collides with the base key for the same item
    (a shared item gets a base row AND a modifier row, both applied). Compute only.

    Returns status='depleted' with the planned movements (possibly empty — a line may have
    no modifiers), or failed/missing_conversion if any modifier ingredient can't convert.
    No legacy key (modifiers never existed in the legacy format)."""
    rows = (
        (
            await session.execute(
                text("""
                SELECT slim.id           AS slim_id,
                       slim.modifier_version_id AS mv,
                       slim.quantity      AS mult,
                       mi.inventory_item_id AS iid,
                       mi.quantity        AS mod_qty,
                       mi.unit            AS recipe_unit,
                       ii.inventory_mode  AS mode,
                       uom.name           AS storage_unit,
                       modv.yield_quantity AS yield_quantity
                FROM sale_line_item_modifiers slim
                JOIN modifier_versions modv ON modv.id = slim.modifier_version_id
                JOIN modifier_ingredients mi
                    ON mi.modifier_version_id = slim.modifier_version_id AND mi.tenant_id = :tid
                JOIN inventory_items ii ON ii.id = mi.inventory_item_id
                JOIN units_of_measure uom ON uom.id = ii.storage_unit_id
                WHERE slim.sale_line_item_id = :sli AND slim.tenant_id = :tid
            """),
                {"sli": sale_line_item_id, "tid": tenant_id},
            )
        )
        .mappings()
        .all()
    )

    # aggregate per (slim_id, modifier_version_id, inventory_item) — the modifier key
    agg: dict[tuple[UUID, UUID, UUID], dict[str, object]] = {}
    for r in rows:
        try:
            storage_qty = await convert(
                session,
                Decimal(str(r["mod_qty"])),
                str(r["recipe_unit"]),
                str(r["storage_unit"]),
                inventory_item_id=r["iid"],
                tenant_id=tenant_id,
            )
        except ConversionError:
            return _no_deplete("failed", "missing_conversion")

        magnitude = (
            line_quantity
            * Decimal(str(r["mult"]))
            * storage_qty
            / Decimal(str(r["yield_quantity"]))
        )
        if r["mode"] == "recipe_deducted":
            mtype, signed = "sale_depletion", -magnitude
        else:
            mtype, signed = "sale_signal", magnitude

        yf = (
            await session.execute(
                text(
                    "SELECT yield_factor FROM inventory_yield_factors"
                    " WHERE tenant_id = :tid AND inventory_item_id = :iid"
                ),
                {"tid": tenant_id, "iid": r["iid"]},
            )
        ).scalar()
        yield_factor = Decimal(str(yf)) if yf is not None else Decimal("1.0")

        key = (r["slim_id"], r["mv"], r["iid"])
        if key in agg:
            agg[key]["delta"] = Decimal(str(agg[key]["delta"])) + signed
        else:
            agg[key] = {"mtype": mtype, "delta": signed, "yf": yield_factor}

    movements = [
        PlannedMovement(
            inventory_item_id=iid,
            movement_type=str(v["mtype"]),
            delta=Decimal(str(v["delta"])),
            yield_factor_applied=Decimal(str(v["yf"])),
            idempotency_key=f"sale_line:{sale_line_item_id}:modifier:{slim_id}:{mv}:{iid}",
            legacy_key=None,
            source_type="sale_line_item_modifier",
            source_id=slim_id,
        )
        for (slim_id, mv, iid), v in agg.items()
    ]
    return WalkOutcome(movements=movements, status="depleted", reason=None)
