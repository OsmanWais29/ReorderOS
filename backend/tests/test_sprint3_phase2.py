"""Sprint 3 — Phase 2 verification gate: on_hand() correctness."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import asyncpg
import pytest

from tests.conftest import seed_tenant

UTC = UTC

pytestmark = pytest.mark.integration


# ── test helpers ──────────────────────────────────────────────────────────────


async def _mk_uom(conn: asyncpg.Connection, tenant_id: str) -> str:
    name = f"uom-{uuid.uuid4().hex[:6]}"
    row = await conn.fetchrow(
        "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type)"
        " VALUES ($1, $2, $3, 'weight') RETURNING id",
        uuid.UUID(tenant_id),
        name,
        name[:3],
    )
    return str(row["id"])


async def _mk_item(
    conn: asyncpg.Connection,
    tenant_id: str,
    uom_id: str,
    *,
    mode: str,
    last_count_at: datetime | None = None,
    last_count_qty: float | None = None,
) -> str:
    cadence = 7 if mode == "count_anchored" else None
    grace = 7 if mode == "count_anchored" else None  # grace >= cadence per v5
    row = await conn.fetchrow(
        """
        INSERT INTO inventory_items
            (tenant_id, name, inventory_mode, storage_unit_id,
             last_count_at, last_count_quantity,
             count_cadence_days, count_grace_days)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        uuid.UUID(tenant_id),
        f"item-{uuid.uuid4().hex[:6]}",
        mode,
        uuid.UUID(uom_id),
        last_count_at,
        last_count_qty,
        cadence,
        grace,
    )
    return str(row["id"])


async def _mv(
    conn: asyncpg.Connection,
    tenant_id: str,
    item_id: str,
    movement_type: str,
    delta: float,
    recorded_at: datetime | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO inventory_movements
            (tenant_id, inventory_item_id, movement_type, delta, recorded_at)
        VALUES ($1, $2, $3, $4, COALESCE($5, NOW()))
        """,
        uuid.UUID(tenant_id),
        uuid.UUID(item_id),
        movement_type,
        delta,
        recorded_at,
    )


async def _on_hand(conn: asyncpg.Connection, tenant_id: str, item_id: str) -> Decimal | None:
    row = await conn.fetchrow(
        "SELECT on_hand($1, $2) AS qty",
        uuid.UUID(tenant_id),
        uuid.UUID(item_id),
    )
    return row["qty"]


# ═════════════════════════════════════════════════════════════════════════════
# TEST 2.1 — Mode A deterministic proof
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_2_1_mode_a_deterministic(admin_conn: asyncpg.Connection) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted")

    await _mv(admin_conn, tid, item, "opening_balance", +100)
    await _mv(admin_conn, tid, item, "sale_depletion", -12)
    await _mv(admin_conn, tid, item, "waste", -3)
    await _mv(admin_conn, tid, item, "receive", +50)
    await _mv(admin_conn, tid, item, "count_adjust", -5)

    result = await _on_hand(admin_conn, tid, item)
    assert result == Decimal("130"), f"expected 130, got {result}"


# ═════════════════════════════════════════════════════════════════════════════
# TEST 2.2 — Mode B deterministic proof
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_2_2_mode_b_deterministic(admin_conn: asyncpg.Connection) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)

    # Anchor is at T0; movements inserted after with a future timestamp.
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    item = await _mk_item(
        admin_conn, tid, uom,
        mode="count_anchored",
        last_count_at=t0,
        last_count_qty=100,
    )

    # Yield factor = 1.25
    await admin_conn.execute(
        "INSERT INTO inventory_yield_factors (tenant_id, inventory_item_id, yield_factor)"
        " VALUES ($1, $2, 1.25)",
        uuid.UUID(tid),
        uuid.UUID(item),
    )

    # Movements strictly after T0.
    await _mv(admin_conn, tid, item, "receive", +50, t1)
    await _mv(admin_conn, tid, item, "sale_signal", +20, t1)
    await _mv(admin_conn, tid, item, "waste", -5, t1)  # excluded from on_hand

    # 100 + 50 - (20 * 1.25) = 125
    result = await _on_hand(admin_conn, tid, item)
    assert result == Decimal("125"), f"expected 125, got {result}"


# ═════════════════════════════════════════════════════════════════════════════
# TEST 2.3 — Mode B with no anchor → NULL
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_2_3_mode_b_no_anchor_returns_null(admin_conn: asyncpg.Connection) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="count_anchored")  # no last_count_qty

    result = await _on_hand(admin_conn, tid, item)
    assert result is None, f"expected NULL, got {result}"


# ═════════════════════════════════════════════════════════════════════════════
# TEST 2.4 — Mode B missing yield_factor row defaults to 1.0
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_2_4_mode_b_missing_yield_factor_defaults_1(
    admin_conn: asyncpg.Connection,
) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)

    t0 = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 2, 1, 1, 0, 0, tzinfo=UTC)

    item = await _mk_item(
        admin_conn, tid, uom,
        mode="count_anchored",
        last_count_at=t0,
        last_count_qty=100,
    )

    # No yield_factor row inserted — must default to 1.0.
    await _mv(admin_conn, tid, item, "sale_signal", +20, t1)

    # 100 + 0 - (20 * 1.0) = 80
    result = await _on_hand(admin_conn, tid, item)
    assert result == Decimal("80"), f"expected 80, got {result}"


# ═════════════════════════════════════════════════════════════════════════════
# TEST 2.5 — Mode A ignores sale_signal movements
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_2_5_mode_a_ignores_sale_signal(admin_conn: asyncpg.Connection) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted")

    await _mv(admin_conn, tid, item, "opening_balance", +100)
    await _mv(admin_conn, tid, item, "sale_signal", +50)  # excluded by Mode A

    result = await _on_hand(admin_conn, tid, item)
    assert result == Decimal("100"), f"expected 100, got {result}"
