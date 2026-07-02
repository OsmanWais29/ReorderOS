"""Seed a World (and its order stream) into the DB so the REAL engine can run against it.

The seeder MAY import app code — it only writes rows; it computes no expected values (that is
the oracle's job, and the oracle alone must stay import-isolated). Conversions are seeded at
the TENANT tier (tenant_id set, inventory_item_id NULL) so they never collide with 0014's
shared global seeds and never leak across tenants.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from app.modules.inventory.depletion.units import DIMENSION_OF
from tests.depletion_model.world import MODE_B, Line, Order, World
from tests.helpers.sprint5 import seed_recipe_version_session

# A fixed pre-sale anchor time for Mode B counts (all sales recorded strictly after this, so
# on_hand()'s "since last count" window includes every signal).
_ANCHOR_AT = datetime(2026, 1, 1, tzinfo=UTC)
# recorded_at for sale movements: strictly after the anchor.
SALE_AT = _ANCHOR_AT + timedelta(days=1)


@dataclass
class Seeded:
    tenant_id: str
    item_ids: dict[str, str] = field(default_factory=dict)  # item.key -> uuid
    uom_ids: dict[str, str] = field(default_factory=dict)  # unit name -> uuid
    menu_item_ids: dict[str, str] = field(default_factory=dict)  # menu_item key -> uuid
    recipe_version_ids: dict[str, str] = field(default_factory=dict)  # menu_item key -> rv
    modifier_ids: dict[str, tuple[str, str]] = field(default_factory=dict)  # key -> (mod, mv)
    # POS catalog ids (the keys the worker maps Clover line items / modifications by). Set for
    # every recipe/modifier so V5b can build Clover payloads referencing them.
    pos_item_ids: dict[str, str] = field(default_factory=dict)  # menu_item key -> pos_item_id
    pos_modifier_ids: dict[str, str] = field(default_factory=dict)  # mod key -> pos_modifier_id


def _distinct_units(world: World) -> set[str]:
    units = {it.storage_unit for it in world.items}
    for r in world.recipes:
        units |= {ing.unit for ing in r.ingredients}
    for m in world.modifiers:
        units |= {ing.unit for ing in m.ingredients}
    for c in world.conversions:
        units |= {c.from_unit, c.to_unit}
    return units


async def seed_world(session: Any, world: World) -> Seeded:
    tid = str(uuid.uuid4())
    await session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:t, 'V2', :s)"),
        {"t": tid, "s": f"v2-{uuid.uuid4().hex[:8]}"},
    )
    s = Seeded(tenant_id=tid)

    # units_of_measure (one per distinct canonical unit; unit_type from the dimension map)
    for unit in _distinct_units(world):
        uom_id = (
            await session.execute(
                text(
                    "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type)"
                    " VALUES (:t, :n, :n, :ut) RETURNING id"
                ),
                {"t": tid, "n": unit, "ut": DIMENSION_OF.get(unit, "count")},
            )
        ).scalar_one()
        s.uom_ids[unit] = str(uom_id)

    # inventory_items
    for it in world.items:
        cadence = 7 if it.mode == MODE_B else None
        last_count_at = _ANCHOR_AT if (it.mode == MODE_B and it.opening_count is not None) else None
        item_id = (
            await session.execute(
                text("""
                    INSERT INTO inventory_items
                        (tenant_id, name, inventory_mode, storage_unit_id, recipe_unit_id,
                         storage_to_recipe_factor, count_cadence_days, count_grace_days,
                         last_count_at, last_count_quantity)
                    VALUES (:t, :n, :mode, :su, :su, 1.0, :cad, :cad, :lca, :lcq)
                    RETURNING id
                """),
                {
                    "t": tid,
                    "n": f"{it.key}-{uuid.uuid4().hex[:6]}",
                    "mode": it.mode,
                    "su": s.uom_ids[it.storage_unit],
                    "cad": cadence,
                    "lca": last_count_at,
                    "lcq": it.opening_count,
                },
            )
        ).scalar_one()
        s.item_ids[it.key] = str(item_id)
        # yield_factor: only needed when ≠ 1 (on_hand COALESCEs absent to 1.0)
        if it.yield_factor != 1:
            await session.execute(
                text("""
                    INSERT INTO inventory_yield_factors
                        (tenant_id, inventory_item_id, yield_factor, effective_from)
                    VALUES (:t, :iid, :yf, :ef)
                """),
                {"t": tid, "iid": item_id, "yf": it.yield_factor, "ef": _ANCHOR_AT},
            )

    # conversions (tenant tier: tenant set, item NULL)
    for c in world.conversions:
        await session.execute(
            text("""
                INSERT INTO unit_conversions
                    (tenant_id, inventory_item_id, from_unit, to_unit, factor, dimension)
                VALUES (:t, NULL, :f, :to, :factor, :dim)
            """),
            {
                "t": tid,
                "f": c.from_unit,
                "to": c.to_unit,
                "factor": c.factor,
                "dim": DIMENSION_OF.get(c.from_unit, "count"),
            },
        )

    # recipes → recipe_versions → recipe_ingredients (reuse the suite helper)
    for r in world.recipes:
        ingredients = [(s.item_ids[ing.item], ing.qty, ing.unit) for ing in r.ingredients]
        seeded = await seed_recipe_version_session(
            session,
            tid,
            ingredients=ingredients,
            yield_quantity=r.yield_quantity,
            status="confirmed" if r.confirmed else "draft",
        )
        s.menu_item_ids[r.menu_item] = seeded.menu_item_id
        if r.confirmed:
            s.recipe_version_ids[r.menu_item] = seeded.recipe_version_id
        # POS catalog id the worker maps Clover li.item.id by (UNIQUE per tenant).
        pos_item_id = f"pos-{r.menu_item}-{uuid.uuid4().hex[:6]}"
        await session.execute(
            text("UPDATE menu_items SET pos_item_id = :pid WHERE id = :mid AND tenant_id = :t"),
            {"pid": pos_item_id, "mid": seeded.menu_item_id, "t": tid},
        )
        s.pos_item_ids[r.menu_item] = pos_item_id

    # modifiers → modifier_versions → modifier_ingredients (+ confirmed pointer)
    for m in world.modifiers:
        mi_id = s.menu_item_ids[m.menu_item]
        pos_modifier_id = f"posm-{m.key}-{uuid.uuid4().hex[:6]}"
        mod_id = str(
            (
                await session.execute(
                    text("""
                        INSERT INTO modifiers
                            (tenant_id, menu_item_id, name, modifier_type, status, pos_modifier_id)
                        VALUES (:t, :m, :n, 'additive', 'confirmed', :pm) RETURNING id
                    """),
                    {
                        "t": tid,
                        "m": mi_id,
                        "n": f"{m.key}-{uuid.uuid4().hex[:5]}",
                        "pm": pos_modifier_id,
                    },
                )
            ).scalar_one()
        )
        s.pos_modifier_ids[m.key] = pos_modifier_id
        mv_id = str(
            (
                await session.execute(
                    text("""
                        INSERT INTO modifier_versions
                            (tenant_id, modifier_id, version_number, yield_quantity)
                        VALUES (:t, :mod, 1, :yq) RETURNING id
                    """),
                    {"t": tid, "mod": mod_id, "yq": m.yield_quantity},
                )
            ).scalar_one()
        )
        await session.execute(
            text("UPDATE modifiers SET current_version_id = :mv WHERE id = :mod"),
            {"mv": mv_id, "mod": mod_id},
        )
        for ing in m.ingredients:
            await session.execute(
                text("""
                    INSERT INTO modifier_ingredients
                        (tenant_id, modifier_version_id, inventory_item_id, quantity, unit)
                    VALUES (:t, :mv, :iid, :q, :u)
                """),
                {"t": tid, "mv": mv_id, "iid": s.item_ids[ing.item], "q": ing.qty, "u": ing.unit},
            )
        s.modifier_ids[m.key] = (mod_id, mv_id)

    await session.flush()
    return s


async def seed_orders(
    session: Any, world: World, orders: list[Order], seeded: Seeded
) -> list[tuple[str, Line]]:
    """Insert pos_event_inbox/orders/sale_line_items(+modifiers) for the stream. Returns
    [(sale_line_item_id, Line)] in stream order for the runner to drive."""
    tid = seeded.tenant_id
    out: list[tuple[str, Line]] = []
    for order in orders:
        inbox_id = str(uuid.uuid4())
        await session.execute(
            text("""
                INSERT INTO pos_event_inbox
                    (inbox_id, tenant_id, vendor, vendor_event_id, vendor_object_type,
                     vendor_event_type, vendor_ts, raw_payload, signature_verified, source)
                VALUES (:iid, :t, 'clover', :vev, 'O', 'UPDATE', 0, '{}', false, 'webhook')
            """),
            {"iid": inbox_id, "t": tid, "vev": f"O:{uuid.uuid4().hex[:8]}"},
        )
        order_id = str(uuid.uuid4())
        await session.execute(
            text("""
                INSERT INTO orders
                    (id, tenant_id, pos_event_inbox_id, clover_order_id, total_amount_cents,
                     state, payment_state, processed_at)
                VALUES (:oid, :t, :iid, :cord, 0, :st, :ps, now())
            """),
            {
                "oid": order_id,
                "t": tid,
                "iid": inbox_id,
                "cord": f"ord_{uuid.uuid4().hex[:8]}",
                "st": order.order_state,
                "ps": order.payment_state,
            },
        )
        for line in order.lines:
            sli_id = str(uuid.uuid4())
            rv = seeded.recipe_version_ids.get(line.menu_item)
            await session.execute(
                text("""
                    INSERT INTO sale_line_items
                        (id, tenant_id, order_id, clover_line_item_id, menu_item_id, name_at_sale,
                         quantity, price_cents_at_sale, net_revenue_cents, is_refunded, is_voided,
                         recipe_version_id, depletion_status)
                    VALUES (:id, :t, :oid, :cli, :mid, 'L', :qty, 0, 0, :ref, :void, :rv, 'pending')
                """),
                {
                    "id": sli_id,
                    "t": tid,
                    "oid": order_id,
                    "cli": f"cli_{uuid.uuid4().hex[:8]}",
                    "mid": seeded.menu_item_ids.get(line.menu_item),
                    "qty": line.qty,
                    "ref": line.is_refunded,
                    "void": line.is_voided,
                    "rv": rv,
                },
            )
            for mod_key, slim_qty in line.modifiers:
                mod_id, mv_id = seeded.modifier_ids[mod_key]
                await session.execute(
                    text("""
                        INSERT INTO sale_line_item_modifiers
                            (id, tenant_id, sale_line_item_id, modifier_id, modifier_version_id,
                             quantity, pos_modifier_id)
                        VALUES (gen_random_uuid(), :t, :sli, :mod, :mv, :q, :pid)
                    """),
                    {
                        "t": tid,
                        "sli": sli_id,
                        "mod": mod_id,
                        "mv": mv_id,
                        "q": slim_qty,
                        "pid": f"pos_{uuid.uuid4().hex[:6]}",
                    },
                )
            out.append((sli_id, line))
    await session.flush()
    return out
