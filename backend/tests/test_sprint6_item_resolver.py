"""Sprint 6 S1 — unit tests for the shared inventory item resolver.

Covers the resolver in its NEW home (inventory/item_resolver.py) — the move out of
recipes/repo.py is separately proven behavior-preserving by the unchanged Sprint 5
confirm/modifier suites — plus the NEW `suggest_inventory_items` ranking helper for
the receipt review UI.

Uses the bound-transaction harness (engine connection + outer rollback) from the
Sprint 5 confirm tests: seeds + calls + assertions share one rolled-back txn, so the
test self-cleans. The engine connects as the superuser test role, so RLS is bypassed
and the resolver's explicit tenant_id filtering is what isolates.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import engine, make_bound_session
from app.modules.inventory.item_resolver import (
    UnitTypeConflict,
    resolve_inventory_item,
    suggest_inventory_items,
)

pytestmark = pytest.mark.integration


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


async def _seed_tenant(db: Any) -> uuid.UUID:
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, 'S1 Resolver Test')"),
        {"id": tid, "slug": f"s1-resolver-{tid.hex[:8]}"},
    )
    return tid


# ── resolve_inventory_item (moved, must still behave) ─────────────────────────


async def test_resolve_creates_then_dedups(db: Any) -> None:
    tid = await _seed_tenant(db)
    first = await resolve_inventory_item(db, tid, "Tomato", "g")
    # Same name (case/space-insensitive) → SAME item, no duplicate (0019 index).
    again = await resolve_inventory_item(db, tid, "  tomato ", "g")
    assert first == again
    n = (
        await db.execute(
            text("SELECT count(*) FROM inventory_items WHERE tenant_id = :t"),
            {"t": tid},
        )
    ).scalar_one()
    assert n == 1


async def test_resolve_preserves_display_case(db: Any) -> None:
    tid = await _seed_tenant(db)
    item_id = await resolve_inventory_item(db, tid, "  Olive Oil ", "ml")
    name = (
        await db.execute(text("SELECT name FROM inventory_items WHERE id = :i"), {"i": item_id})
    ).scalar_one()
    # btrim, not lower — trimmed but case-preserved.
    assert name == "Olive Oil"


async def test_resolve_raises_on_unit_type_conflict(db: Any) -> None:
    tid = await _seed_tenant(db)
    # Pre-seed a units_of_measure row 'g' with the WRONG dimension for this tenant.
    await db.execute(
        text(
            "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
            "VALUES (:t, 'g', 'g', 'volume')"
        ),
        {"t": tid},
    )
    with pytest.raises(UnitTypeConflict):
        await resolve_inventory_item(db, tid, "Flour", "g")  # 'g' is weight, row says volume


# ── suggest_inventory_items (new) ─────────────────────────────────────────────


async def test_suggest_ranks_exact_then_prefix_then_substring(db: Any) -> None:
    tid = await _seed_tenant(db)
    for name in ("Tomato", "Tomato Paste", "Roma Tomato", "Cheese"):
        await resolve_inventory_item(db, tid, name, "g")

    out = await suggest_inventory_items(db, tid, "tomato")
    names = [r["name"] for r in out]

    assert "Cheese" not in names  # no match → excluded
    assert names[0] == "Tomato"  # exact (normalized) first
    assert names.index("Tomato Paste") < names.index("Roma Tomato")  # prefix before substring


async def test_suggest_excludes_inactive(db: Any) -> None:
    tid = await _seed_tenant(db)
    keep = await resolve_inventory_item(db, tid, "Active Tomato", "g")
    drop = await resolve_inventory_item(db, tid, "Dead Tomato", "g")
    await db.execute(text("UPDATE inventory_items SET active = false WHERE id = :i"), {"i": drop})
    ids = {r["id"] for r in await suggest_inventory_items(db, tid, "tomato")}
    assert keep in ids
    assert drop not in ids


async def test_suggest_is_tenant_scoped(db: Any) -> None:
    tid_a = await _seed_tenant(db)
    tid_b = await _seed_tenant(db)
    await resolve_inventory_item(db, tid_a, "Tomato", "g")
    await resolve_inventory_item(db, tid_b, "Tomato", "g")
    out_a = await suggest_inventory_items(db, tid_a, "tomato")
    assert len(out_a) == 1  # only tenant A's item, never B's


async def test_suggest_respects_limit(db: Any) -> None:
    tid = await _seed_tenant(db)
    for i in range(7):
        await resolve_inventory_item(db, tid, f"Tomato {i}", "g")
    out = await suggest_inventory_items(db, tid, "tomato", limit=3)
    assert len(out) == 3
