"""Sprint 5 Phase 1 — unit conversion service.

Covers: canonical correctness, identity short-circuit (no DB), cross-dimension and
unknown-unit errors, the tenant-required-with-item ValueError guard, table-driven
behaviour, item -> tenant -> global precedence, and — most importantly —
cross-tenant density isolation.

Global conversions rely on the 0014 seed rows. Tenant/item-tier rows are seeded
inside an uncommitted session and rolled back on exit (no residue, no collision
with the global unique index).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_sessionmaker
from app.modules.inventory.depletion.conversions import ConversionError, convert
from app.modules.inventory.depletion.units import CANONICAL_UNITS, is_canonical

pytestmark = pytest.mark.integration


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Uncommitted session; everything rolls back on close (no residue)."""
    sm = get_sessionmaker()
    async with sm() as s:
        try:
            yield s
        finally:
            await s.rollback()


async def _seed_tenant(s: AsyncSession) -> uuid.UUID:
    slug = f"conv-{uuid.uuid4().hex[:10]}"
    return (
        await s.execute(
            text("INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id"),
            {"s": slug},
        )
    ).scalar_one()


async def _seed_item(s: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    uom = (
        await s.execute(
            text(
                "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type)"
                " VALUES (:t, :n, 'g', 'weight') RETURNING id"
            ),
            {"t": tenant_id, "n": f"g-{uuid.uuid4().hex[:6]}"},
        )
    ).scalar_one()
    return (
        await s.execute(
            text(
                "INSERT INTO inventory_items (tenant_id, name, inventory_mode, storage_unit_id)"
                " VALUES (:t, :n, 'recipe_deducted', :u) RETURNING id"
            ),
            {"t": tenant_id, "n": f"flour-{uuid.uuid4().hex[:6]}", "u": uom},
        )
    ).scalar_one()


async def _seed_conv(
    s: AsyncSession,
    from_u: str,
    to_u: str,
    factor: str,
    *,
    dimension: str = "weight",
    tenant_id: uuid.UUID | None = None,
    item_id: uuid.UUID | None = None,
) -> None:
    await s.execute(
        text(
            "INSERT INTO unit_conversions"
            " (from_unit, to_unit, dimension, factor, tenant_id, inventory_item_id)"
            " VALUES (:f, :t, :d, :fac, :tid, :iid)"
        ),
        {"f": from_u, "t": to_u, "d": dimension, "fac": factor, "tid": tenant_id, "iid": item_id},
    )


# ── units allowlist ───────────────────────────────────────────────────────────


def test_canonical_allowlist() -> None:
    assert is_canonical("g") and is_canonical("oz_weight") and is_canonical("fl_oz")
    assert not is_canonical("oz")  # ambiguous — excluded
    assert not is_canonical("gg")
    assert {"g", "kg", "ml", "L", "ea", "dozen"} <= CANONICAL_UNITS


# ── correctness (global seeds from 0014) ─────────────────────────────────────


async def test_global_conversions(session: AsyncSession) -> None:
    assert await convert(session, Decimal("500"), "g", "kg") == Decimal("0.5")
    assert await convert(session, Decimal("2"), "kg", "g") == Decimal("2000")
    assert await convert(session, Decimal("250"), "ml", "L") == Decimal("0.25")
    assert await convert(session, Decimal("1"), "lb", "oz_weight") == Decimal("16")


async def test_identity_needs_no_row(session: AsyncSession) -> None:
    # Even with no g->g seed row, identity returns qty unchanged (no query).
    assert await convert(session, Decimal("100"), "g", "g") == Decimal("100")


async def test_round_trip(session: AsyncSession) -> None:
    half = await convert(session, Decimal("500"), "g", "kg")
    back = await convert(session, half, "kg", "g")
    assert back == Decimal("500")


async def test_cross_dimension_raises(session: AsyncSession) -> None:
    with pytest.raises(ConversionError):
        await convert(session, Decimal("1"), "g", "ml")


async def test_unknown_unit_raises(session: AsyncSession) -> None:
    with pytest.raises(ConversionError):
        await convert(session, Decimal("1"), "gg", "g")


# ── caller-contract guard (distinct from ConversionError) ────────────────────


async def test_item_without_tenant_is_valueerror(session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await convert(session, Decimal("1"), "g", "kg", inventory_item_id=uuid.uuid4())


# ── table-driven + precedence ────────────────────────────────────────────────


async def test_reads_from_table_tenant_override(session: AsyncSession) -> None:
    tid = await _seed_tenant(session)
    # A deliberately wrong tenant-tier factor proves the value comes from the row.
    await _seed_conv(session, "g", "kg", "999", tenant_id=tid)
    assert await convert(session, Decimal("1"), "g", "kg", tenant_id=tid) == Decimal("999")
    # No tenant -> falls through to the real global seed.
    assert await convert(session, Decimal("1"), "g", "kg") == Decimal("0.001")


async def test_precedence_item_over_tenant_over_global(session: AsyncSession) -> None:
    tid = await _seed_tenant(session)
    item = await _seed_item(session, tid)
    await _seed_conv(session, "g", "kg", "0.5", tenant_id=tid)  # tenant tier
    await _seed_conv(session, "g", "kg", "0.9", tenant_id=tid, item_id=item)  # item tier

    # item-specific wins
    assert await convert(
        session, Decimal("1"), "g", "kg", inventory_item_id=item, tenant_id=tid
    ) == Decimal("0.9")
    # tenant-specific when no item override for that item
    other_item = await _seed_item(session, tid)
    assert await convert(
        session, Decimal("1"), "g", "kg", inventory_item_id=other_item, tenant_id=tid
    ) == Decimal("0.5")
    # tenant-specific when no item passed
    assert await convert(session, Decimal("1"), "g", "kg", tenant_id=tid) == Decimal("0.5")
    # global when no tenant
    assert await convert(session, Decimal("1"), "g", "kg") == Decimal("0.001")


# ── the most important test: cross-tenant density isolation ──────────────────


async def test_cross_tenant_density_isolation(session: AsyncSession) -> None:
    # Tenant A defines a private density: 1 cup of A's flour = 130 g.
    a = await _seed_tenant(session)
    a_flour = await _seed_item(session, a)
    await _seed_conv(session, "cup", "g", "130", tenant_id=a, item_id=a_flour)

    # A sees its own override.
    assert await convert(
        session, Decimal("1"), "cup", "g", inventory_item_id=a_flour, tenant_id=a
    ) == Decimal("130")

    # Half 1 — A's override is INVISIBLE to tenant B: B has no cup->g anywhere
    # (cross-dimension is not seeded globally) -> ConversionError, NOT 130.
    b = await _seed_tenant(session)
    b_flour = await _seed_item(session, b)
    with pytest.raises(ConversionError):
        await convert(session, Decimal("1"), "cup", "g", inventory_item_id=b_flour, tenant_id=b)

    # Half 2 — isolation does not over-correct: B still gets correct fallback for
    # a normal conversion (global g->kg), proving B isn't starved of conversions.
    assert await convert(session, Decimal("1"), "g", "kg", tenant_id=b) == Decimal("0.001")
