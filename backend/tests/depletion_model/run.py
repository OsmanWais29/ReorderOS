"""Drive the REAL engine over a seeded World, then read back the actual ledger / on-hand.

This is the only part that touches the production engine (handler.process_line,
handler.reverse_line, services.on_hand). The oracle never does. A V2 test asserts
actual_* == oracle expected_*.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.modules.inventory.depletion import handler
from app.modules.inventory.services import on_hand, record_count_event
from tests.depletion_model.seed import SALE_AT, Seeded, seed_orders
from tests.depletion_model.world import Count, Line, Order, World


async def run_stream(
    session: Any,
    world: World,
    orders: list[Order],
    seeded: Seeded,
    *,
    partial_refunds_enabled: bool = False,
) -> list[tuple[str, str, str | None]]:
    """Seed the orders and drive process_line per line (the engine's per-line entry point —
    V2 isolates depletion from ingestion; V5 drives the full worker). For refund_after_deplete
    lines, also call reverse_line (the refund path). Returns [(sli_id, status, reason)]."""
    lines = await seed_orders(session, world, orders, seeded)
    results: list[tuple[str, str, str | None]] = []
    tid = UUID(seeded.tenant_id)
    for sli_id, line in lines:
        status, reason = await handler.process_line(
            session, tid, UUID(sli_id),
            recorded_at=SALE_AT, partial_refunds_enabled=partial_refunds_enabled,
        )
        if line.refund_after_deplete:
            await handler.reverse_line(session, tid, UUID(sli_id))
        results.append((sli_id, status, reason))
    return results


async def run_timeline(
    session: Any,
    world: World,
    timeline: list[object],
    seeded: Seeded,
    *,
    partial_refunds_enabled: bool = False,
) -> None:
    """Drive a timeline of Orders and Counts IN ORDER, with monotonically increasing logical
    timestamps (so the historical on_hand boundary recorded_at>last_count_at partitions
    pre/post-count sales). Orders → seed+process lines; Counts → record_count_event (the real
    operator count path, which re-anchors Mode B / emits Mode A count_adjust). Mirrors the order
    expected_timeline assumes."""
    tid = UUID(seeded.tenant_id)
    for i, event in enumerate(timeline):
        ts = SALE_AT + timedelta(minutes=i)
        if isinstance(event, Order):
            lines = await seed_orders(session, world, [event], seeded)
            for sli_id, line in lines:
                await handler.process_line(
                    session, tid, UUID(sli_id),
                    recorded_at=ts, partial_refunds_enabled=partial_refunds_enabled,
                )
                if line.refund_after_deplete:
                    await handler.reverse_line(session, tid, UUID(sli_id))
        elif isinstance(event, Count):
            await record_count_event(
                session,
                tenant_id=tid,
                inventory_item_id=UUID(seeded.item_ids[event.item]),
                counted_quantity=Decimal(str(event.counted)),
                counted_at=ts,
            )


async def actual_ledger(session: Any, seeded: Seeded) -> dict[str, Decimal]:
    """Net Σ(delta) per item key across ALL movement types (forward + reversal). Items with a
    net of exactly zero are dropped so the comparison matches expected_ledger (which omits
    no-movement items). A refunded-then-reversed item nets to 0 → dropped (= absent in both)."""
    rows = (
        await session.execute(
            text("""
                SELECT inventory_item_id, COALESCE(SUM(delta), 0) AS net
                  FROM inventory_movements
                 WHERE tenant_id = :t
                 GROUP BY inventory_item_id
            """),
            {"t": seeded.tenant_id},
        )
    ).mappings().all()
    by_id_to_key = {v: k for k, v in seeded.item_ids.items()}
    out: dict[str, Decimal] = {}
    for r in rows:
        net = Decimal(str(r["net"]))
        if net != 0:
            out[by_id_to_key[str(r["inventory_item_id"])]] = net
    return out


async def actual_on_hand(session: Any, seeded: Seeded, world: World) -> dict[str, Decimal | None]:
    """on_hand() per item (historical path: cutoff=None → recorded_at>last_count_at + live
    yield_factor table). Returns {item_key: qty|None}."""
    out: dict[str, Decimal | None] = {}
    for it in world.items:
        q = await on_hand(
            session,
            tenant_id=UUID(seeded.tenant_id),
            inventory_item_id=UUID(seeded.item_ids[it.key]),
        )
        out[it.key] = q if q is None else Decimal(str(q))
    return out


# re-exported for test ergonomics
__all__ = ["Line", "Order", "World", "actual_ledger", "actual_on_hand", "run_stream"]
