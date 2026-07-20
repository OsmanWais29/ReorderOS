"""Characterization gate for the canonical balance projection (PR-A1 condition 2).

Proves, across a fixture matrix, that the projection module is Decimal-identical
to the authoritative on_hand():

  - current_balance_batch()[iid] == on_hand(iid)          (list consolidation gate)
  - balance_before_mode_a(before=+inf) == on_hand(iid)    (Mode-A boundary gate)

If any assertion fails, consolidation must NOT proceed — the list keeps its own
copy and insights falls back to calling on_hand() with reconciliation
unavailable. These tests are the machine proof behind that decision.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import engine, make_bound_session
from app.modules.inventory.balance_projection import (
    balance_before_mode_a,
    current_balance_batch,
)
from app.modules.inventory.services import on_hand

pytestmark = pytest.mark.integration

_FAR_FUTURE = datetime(2999, 1, 1, tzinfo=UTC)


@pytest.fixture
async def db() -> AsyncIterator[Any]:
    connection: AsyncConnection
    async with engine.connect() as connection:
        await connection.begin()
        session = make_bound_session(connection)
        try:
            yield session
        finally:
            await connection.rollback()


async def _unit(db: Any, tid: uuid.UUID) -> uuid.UUID:
    return (
        await db.execute(
            text(
                "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
                "VALUES (:t, 'ea', 'ea', 'count') RETURNING id"
            ),
            {"t": tid},
        )
    ).scalar_one()


async def _item(
    db: Any,
    tid: uuid.UUID,
    unit: uuid.UUID,
    *,
    mode: str,
    last_count_at: datetime | None = None,
    last_count_quantity: Decimal | None = None,
) -> uuid.UUID:
    # cadence_coherence CHECK (0004): Mode B needs both set, grace >= cadence.
    cadence = 7 if mode == "count_anchored" else None
    grace = 9 if mode == "count_anchored" else None
    return (
        await db.execute(
            text("""
                INSERT INTO inventory_items
                    (tenant_id, name, inventory_mode, storage_unit_id, recipe_unit_id,
                     count_cadence_days, count_grace_days, last_count_at, last_count_quantity)
                VALUES (:t, :n, :m, :u, :u, :cad, :gr, :lca, :lcq)
                RETURNING id
            """),
            {
                "t": tid,
                "n": f"item-{uuid.uuid4().hex[:8]}",
                "m": mode,
                "u": unit,
                "cad": cadence,
                "gr": grace,
                "lca": last_count_at,
                "lcq": last_count_quantity,
            },
        )
    ).scalar_one()


async def _mv(
    db: Any,
    tid: uuid.UUID,
    iid: uuid.UUID,
    *,
    mtype: str,
    delta: str,
    when: datetime,
    yfa: str | None = None,
) -> None:
    await db.execute(
        text("""
            INSERT INTO inventory_movements
                (tenant_id, inventory_item_id, movement_type, delta, recorded_at,
                 created_at, yield_factor_applied, idempotency_key)
            VALUES (:t, :i, :mt, :d, :w, :w, :yfa, :k)
        """),
        {
            "t": tid,
            "i": iid,
            "mt": mtype,
            "d": delta,
            "w": when,
            "yfa": yfa,
            "k": f"char:{uuid.uuid4()}",
        },
    )


async def _matrix(db: Any) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Seed a diverse fixture set and return (tenant_id, [item_ids])."""
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'CHAR')"),
        {"id": tid, "s": f"char-{tid.hex[:8]}"},
    )
    unit = await _unit(db, tid)
    now = datetime.now(UTC)
    ids: list[uuid.UUID] = []

    # 1. Mode A, mixed movements incl. a reversal and a negative depletion.
    a1 = await _item(db, tid, unit, mode="recipe_deducted")
    await _mv(db, tid, a1, mtype="opening_balance", delta="100", when=now - timedelta(days=30))
    await _mv(db, tid, a1, mtype="receive", delta="48", when=now - timedelta(days=10))
    await _mv(db, tid, a1, mtype="sale_depletion", delta="-25.5", when=now - timedelta(days=5))
    await _mv(db, tid, a1, mtype="sale_depletion_reversal", delta="0.5", when=now - timedelta(days=4))
    await _mv(db, tid, a1, mtype="count_adjust", delta="-1", when=now - timedelta(days=2))
    ids.append(a1)

    # 2. Mode A with NO movements (→ 0).
    a2 = await _item(db, tid, unit, mode="recipe_deducted")
    ids.append(a2)

    # 3. Mode A that also has sale_signal rows (must be IGNORED by Mode A rule).
    a3 = await _item(db, tid, unit, mode="recipe_deducted")
    await _mv(db, tid, a3, mtype="receive", delta="60", when=now - timedelta(days=8))
    await _mv(db, tid, a3, mtype="sale_signal", delta="-9", when=now - timedelta(days=3))
    await _mv(db, tid, a3, mtype="sale_depletion", delta="-4", when=now - timedelta(days=1))
    ids.append(a3)

    # 4. Mode B counted, with signals after the count and a receipt after it.
    count_at = now - timedelta(days=6)
    b1 = await _item(
        db, tid, unit, mode="count_anchored",
        last_count_at=count_at, last_count_quantity=Decimal("200"),
    )
    await _mv(db, tid, b1, mtype="receive", delta="30", when=now - timedelta(days=4))
    await _mv(db, tid, b1, mtype="sale_signal", delta="-12", when=now - timedelta(days=2), yfa="1.0")
    ids.append(b1)

    # 5. Mode B NOT counted (last_count_quantity NULL → on_hand None).
    b2 = await _item(db, tid, unit, mode="count_anchored", last_count_at=None, last_count_quantity=None)
    await _mv(db, tid, b2, mtype="receive", delta="15", when=now - timedelta(days=1))
    ids.append(b2)

    return tid, ids


async def test_batch_matches_on_hand_per_item(db: Any) -> None:
    tid, ids = await _matrix(db)
    batch = await current_balance_batch(db, tenant_id=tid, item_ids=ids)
    for iid in ids:
        single = await on_hand(db, tenant_id=tid, inventory_item_id=iid)
        assert batch[iid] == single, f"batch {batch[iid]} != on_hand {single} for {iid}"


async def test_mode_a_balance_before_infinity_matches_on_hand(db: Any) -> None:
    tid, ids = await _matrix(db)
    for iid in ids:
        mode = (
            await db.execute(
                text("SELECT inventory_mode FROM inventory_items WHERE id = :i"), {"i": iid}
            )
        ).scalar_one()
        bounded = await balance_before_mode_a(
            db, tenant_id=tid, inventory_item_id=iid, before=_FAR_FUTURE
        )
        if mode == "recipe_deducted":
            single = await on_hand(db, tenant_id=tid, inventory_item_id=iid)
            assert bounded == single, f"bounded {bounded} != on_hand {single} for {iid}"
        else:
            assert bounded is None, f"Mode B must return None, got {bounded} for {iid}"


async def test_list_endpoint_mode_b_diverges_from_on_hand_documented(db: Any) -> None:
    """DOCUMENTS why the list is NOT consolidated in PR-A1.

    The GET /inventory/items inline Mode-B math uses SUM(ABS(delta)) over
    'sale_signal' ONLY; on_hand() sums signed delta over BOTH signal types. On a
    count-anchored item with a signal reversal they disagree, so switching the
    list onto the canonical projection would silently change displayed on-hand.
    This test pins that disagreement so the divergence cannot regress unnoticed.
    """
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'DIVERGE')"),
        {"id": tid, "s": f"div-{tid.hex[:8]}"},
    )
    unit = await _unit(db, tid)
    now = datetime.now(UTC)
    count_at = now - timedelta(days=6)
    b = await _item(
        db, tid, unit, mode="count_anchored",
        last_count_at=count_at, last_count_quantity=Decimal("200"),
    )
    await _mv(db, tid, b, mtype="receive", delta="30", when=now - timedelta(days=5))
    await _mv(db, tid, b, mtype="sale_signal", delta="-12", when=now - timedelta(days=3), yfa="1.0")
    await _mv(db, tid, b, mtype="sale_signal_reversal", delta="12", when=now - timedelta(days=2), yfa="1.0")

    authoritative = await on_hand(db, tenant_id=tid, inventory_item_id=b)
    # on_hand: 200 + 30 - (-12 + 12) = 230
    assert authoritative == Decimal("230")

    # The list's ABS(sale_signal)-only formula, reproduced exactly:
    list_value = (
        await db.execute(
            text("""
                SELECT ii.last_count_quantity
                     + COALESCE((SELECT SUM(m.delta) FROM inventory_movements m
                                  WHERE m.tenant_id=ii.tenant_id AND m.inventory_item_id=ii.id
                                    AND m.recorded_at>ii.last_count_at
                                    AND m.movement_type IN
                                        ('receive','transfer_in','count_adjust','opening_balance')),0)
                     - (COALESCE((SELECT SUM(ABS(m.delta)) FROM inventory_movements m
                                   WHERE m.tenant_id=ii.tenant_id AND m.inventory_item_id=ii.id
                                     AND m.recorded_at>ii.last_count_at
                                     AND m.movement_type='sale_signal'),0)
                        * COALESCE((SELECT yf.yield_factor FROM inventory_yield_factors yf
                                     WHERE yf.tenant_id=ii.tenant_id
                                       AND yf.inventory_item_id=ii.id),1.0))
                  FROM inventory_items ii WHERE ii.id = :b
            """),
            {"b": b},
        )
    ).scalar_one()
    # list: 200 + 30 - 12 = 218  (drops the +12 reversal)
    assert Decimal(str(list_value)) == Decimal("218")
    assert authoritative != Decimal(str(list_value)), "divergence must be real"


async def test_empty_batch_returns_empty() -> None:
    # Pure function guard — no DB needed.
    import asyncio

    async def _run() -> None:
        async with engine.connect() as c:
            await c.begin()
            s = make_bound_session(c)
            out = await current_balance_batch(s, tenant_id=uuid.uuid4(), item_ids=[])
            assert out == {}
            await c.rollback()

    await _run()
