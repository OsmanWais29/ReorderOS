"""Mode-B Stock-list balance fix (prerequisite PR).

The GET /inventory/items count-anchored on-hand used SUM(ABS(delta)) over
'sale_signal' ONLY, while on_hand() sums signed delta over BOTH 'sale_signal'
and 'sale_signal_reversal'. After a refund (sale_signal_reversal) the list
understated on-hand — the Stock list disagreed with the authoritative balance.

These tests drive the REAL GET /inventory/items endpoint and assert its on_hand
equals on_hand() Decimal-for-Decimal across the depletion lifecycle. No new
balance SQL is introduced — the list's inline copy is corrected to the canonical
signed rule.

Production signal convention (walker.py): Mode-B sale_signal delta is POSITIVE
(a consumption magnitude); a refund's sale_signal_reversal delta is NEGATIVE
(credits the consumption back).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import engine, get_db_session, make_bound_session
from app.core.security import Principal, get_principal
from app.main import create_app
from app.modules.inventory.services import on_hand

pytestmark = pytest.mark.integration


class _Ctx:
    def __init__(self, conn: AsyncConnection, tid: uuid.UUID, client: AsyncClient):
        self.conn, self.tid, self.client = conn, tid, client


async def _ctx() -> AsyncIterator[_Ctx]:
    app = create_app()
    tid, uid = uuid.uuid4(), uuid.uuid4()
    conn: AsyncConnection
    async with engine.connect() as conn:
        await conn.begin()
        bound = make_bound_session(conn)
        app.dependency_overrides[get_db_session] = lambda: bound
        app.dependency_overrides[get_principal] = lambda: Principal(
            user_id=str(uid),
            workos_id=f"w_{uid.hex[:8]}",
            email="x@test.com",
            tenant_id=str(tid),
            role="manager",  # type: ignore[arg-type]
        )
        try:
            await conn.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id,:s,'MODB')"),
                {"id": tid, "s": f"modb-{tid.hex[:8]}"},
            )
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                yield _Ctx(conn, tid, c)
        finally:
            app.dependency_overrides.clear()
            await conn.rollback()


async def _unit(conn: AsyncConnection, tid: uuid.UUID) -> uuid.UUID:
    return (
        await conn.execute(
            text(
                "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
                "VALUES (:t,'ea','ea','count') RETURNING id"
            ),
            {"t": tid},
        )
    ).scalar_one()


async def _item(
    conn: AsyncConnection,
    tid: uuid.UUID,
    unit: uuid.UUID,
    *,
    mode: str,
    last_count_at: datetime | None = None,
    last_count_qty: Decimal | None = None,
) -> uuid.UUID:
    cad = 7 if mode == "count_anchored" else None
    gr = 9 if mode == "count_anchored" else None
    return (
        await conn.execute(
            text("""
                INSERT INTO inventory_items
                    (tenant_id, name, inventory_mode, storage_unit_id, recipe_unit_id,
                     count_cadence_days, count_grace_days, last_count_at, last_count_quantity)
                VALUES (:t,:n,:m,:u,:u,:cad,:gr,:lca,:lcq) RETURNING id
            """),
            {
                "t": tid,
                "n": f"i-{uuid.uuid4().hex[:8]}",
                "m": mode,
                "u": unit,
                "cad": cad,
                "gr": gr,
                "lca": last_count_at,
                "lcq": last_count_qty,
            },
        )
    ).scalar_one()


async def _mv(
    conn: AsyncConnection,
    tid: uuid.UUID,
    iid: uuid.UUID,
    *,
    mtype: str,
    delta: str,
    days: int,
    key: str | None = None,
) -> None:
    now = datetime.now(UTC)
    await conn.execute(
        text("""
            INSERT INTO inventory_movements
                (tenant_id, inventory_item_id, movement_type, delta, recorded_at, created_at,
                 idempotency_key)
            VALUES (:t,:i,:mt,:d,:w,:w,:k)
            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
        """),
        {
            "t": tid,
            "i": iid,
            "mt": mtype,
            "d": delta,
            "w": now - timedelta(days=days),
            "k": key or f"modb:{uuid.uuid4()}",
        },
    )


async def _list_on_hand(ctx: _Ctx, item_id: uuid.UUID) -> Decimal | None:
    r = await ctx.client.get("/api/v1/inventory/items")
    assert r.status_code == 200, r.text
    for it in r.json()["items"]:
        if it["id"] == str(item_id):
            return None if it["on_hand"] is None else Decimal(str(it["on_hand"]))
    raise AssertionError("item not in list")


async def _assert_agrees(ctx: _Ctx, item_id: uuid.UUID) -> Decimal | None:
    """List endpoint on_hand must equal on_hand() Decimal-for-Decimal."""
    listed = await _list_on_hand(ctx, item_id)
    canonical = await on_hand(ctx.conn, tenant_id=ctx.tid, inventory_item_id=item_id)
    if canonical is None:
        assert listed is None
    else:
        assert listed is not None and listed == canonical, f"list {listed} != on_hand {canonical}"
    return canonical


# ── The bug: refund reversal on a Mode-B item ────────────────────────────────


async def test_mode_b_refund_reversal_list_matches_on_hand() -> None:
    """Regression: count 200, receive +30, sale_signal +12, refund reversal −12.
    Correct on-hand = 200 + 30 − (12 − 12) = 230. The old list dropped the
    reversal → 218. Now list == on_hand == 230."""
    async for ctx in _ctx():
        unit = await _unit(ctx.conn, ctx.tid)
        item = await _item(
            ctx.conn,
            ctx.tid,
            unit,
            mode="count_anchored",
            last_count_at=datetime.now(UTC) - timedelta(days=6),
            last_count_qty=Decimal("200"),
        )
        await _mv(ctx.conn, ctx.tid, item, mtype="receive", delta="30", days=5)
        await _mv(ctx.conn, ctx.tid, item, mtype="sale_signal", delta="12", days=3)
        await _mv(ctx.conn, ctx.tid, item, mtype="sale_signal_reversal", delta="-12", days=2)

        canonical = await _assert_agrees(ctx, item)
        assert canonical == Decimal("230")  # the corrected arithmetic (was 218)


# ── Lifecycle coverage ───────────────────────────────────────────────────────


async def test_mode_b_receipt_after_count() -> None:
    async for ctx in _ctx():
        unit = await _unit(ctx.conn, ctx.tid)
        item = await _item(
            ctx.conn,
            ctx.tid,
            unit,
            mode="count_anchored",
            last_count_at=datetime.now(UTC) - timedelta(days=6),
            last_count_qty=Decimal("100"),
        )
        await _mv(ctx.conn, ctx.tid, item, mtype="receive", delta="40", days=4)
        canonical = await _assert_agrees(ctx, item)
        assert canonical == Decimal("140")


async def test_mode_b_sale_after_count() -> None:
    async for ctx in _ctx():
        unit = await _unit(ctx.conn, ctx.tid)
        item = await _item(
            ctx.conn,
            ctx.tid,
            unit,
            mode="count_anchored",
            last_count_at=datetime.now(UTC) - timedelta(days=6),
            last_count_qty=Decimal("100"),
        )
        await _mv(ctx.conn, ctx.tid, item, mtype="sale_signal", delta="15", days=3)
        canonical = await _assert_agrees(ctx, item)
        assert canonical == Decimal("85")


async def test_mode_b_count_adjustment() -> None:
    async for ctx in _ctx():
        unit = await _unit(ctx.conn, ctx.tid)
        item = await _item(
            ctx.conn,
            ctx.tid,
            unit,
            mode="count_anchored",
            last_count_at=datetime.now(UTC) - timedelta(days=6),
            last_count_qty=Decimal("100"),
        )
        await _mv(ctx.conn, ctx.tid, item, mtype="count_adjust", delta="-7", days=2)
        canonical = await _assert_agrees(ctx, item)
        assert canonical == Decimal("93")


async def test_mode_b_duplicate_replay_no_double_count() -> None:
    async for ctx in _ctx():
        unit = await _unit(ctx.conn, ctx.tid)
        item = await _item(
            ctx.conn,
            ctx.tid,
            unit,
            mode="count_anchored",
            last_count_at=datetime.now(UTC) - timedelta(days=6),
            last_count_qty=Decimal("100"),
        )
        # Same idempotency key twice — the unique constraint drops the replay.
        await _mv(ctx.conn, ctx.tid, item, mtype="sale_signal", delta="10", days=3, key="dup-1")
        await _mv(ctx.conn, ctx.tid, item, mtype="sale_signal", delta="10", days=3, key="dup-1")
        canonical = await _assert_agrees(ctx, item)
        assert canonical == Decimal("90")  # depleted once, not twice


async def test_mode_a_unchanged() -> None:
    async for ctx in _ctx():
        unit = await _unit(ctx.conn, ctx.tid)
        item = await _item(ctx.conn, ctx.tid, unit, mode="recipe_deducted")
        await _mv(ctx.conn, ctx.tid, item, mtype="opening_balance", delta="50", days=20)
        await _mv(ctx.conn, ctx.tid, item, mtype="receive", delta="30", days=10)
        await _mv(ctx.conn, ctx.tid, item, mtype="sale_depletion", delta="-12", days=3)
        await _mv(ctx.conn, ctx.tid, item, mtype="sale_depletion_reversal", delta="4", days=2)
        canonical = await _assert_agrees(ctx, item)
        assert canonical == Decimal("72")  # 50+30-12+4


async def test_tenant_isolation_list_scoped() -> None:
    async for ctx in _ctx():
        unit = await _unit(ctx.conn, ctx.tid)
        mine = await _item(
            ctx.conn,
            ctx.tid,
            unit,
            mode="count_anchored",
            last_count_at=datetime.now(UTC) - timedelta(days=6),
            last_count_qty=Decimal("100"),
        )
        # A foreign tenant's item must never appear in this tenant's list.
        other = uuid.uuid4()
        await ctx.conn.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id,:s,'OTHER')"),
            {"id": other, "s": f"oth-{other.hex[:8]}"},
        )
        ounit = await _unit(ctx.conn, other)
        oitem = await _item(ctx.conn, other, ounit, mode="recipe_deducted")
        await _mv(ctx.conn, other, oitem, mtype="opening_balance", delta="999", days=5)

        r = await ctx.client.get("/api/v1/inventory/items")
        ids = {it["id"] for it in r.json()["items"]}
        assert str(mine) in ids
        assert str(oitem) not in ids
