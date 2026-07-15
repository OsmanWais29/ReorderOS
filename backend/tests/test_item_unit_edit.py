"""Item storage-unit correction — the oz_weight-goblets lesson.

A mis-picked storage unit must be fixable BEFORE the item accrues history, and
IMMUTABLE-in-place after: movements or recipe references mean an in-place unit
swap silently re-denominates the ledger / depletion math.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import engine, get_db_session, make_bound_session
from app.core.security import Principal, get_principal
from app.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def app_instance() -> Any:
    return create_app()


@pytest.fixture
async def conn(app_instance: Any) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as connection:
        await connection.begin()
        bound = make_bound_session(connection)
        app_instance.dependency_overrides[get_db_session] = lambda: bound
        try:
            yield connection
        finally:
            app_instance.dependency_overrides.clear()
            await connection.rollback()


@pytest.fixture
async def client(app_instance: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app_instance), base_url="http://test") as c:
        yield c


def _as(app_instance: Any, tenant_id: str, user_id: str, role: str = "manager") -> None:
    app_instance.dependency_overrides[get_principal] = lambda: Principal(
        user_id=user_id,
        workos_id=f"w_{user_id[:8]}",
        email="x@test.com",
        tenant_id=tenant_id,
        role=role,  # type: ignore[arg-type]
    )


async def _seed_item(conn: AsyncConnection, *, unit: str = "oz_weight") -> dict[str, Any]:
    tid, uid, unit_id, item_id = (uuid.uuid4() for _ in range(4))
    await conn.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'U', :slug)"),
        {"id": tid, "slug": f"u-{tid.hex[:8]}"},
    )
    await conn.execute(
        text("INSERT INTO users (id, workos_id, email) VALUES (:id, :w, :e)"),
        {"id": uid, "w": f"w_{uid.hex[:8]}", "e": f"{uid.hex[:8]}@test.com"},
    )
    ut = "weight" if unit in ("g", "kg", "oz_weight", "lb") else "count"
    await conn.execute(
        text(
            "INSERT INTO units_of_measure (id, tenant_id, name, abbreviation, unit_type) "
            "VALUES (:id, :t, :n, :n, :ut)"
        ),
        {"id": unit_id, "t": tid, "n": unit, "ut": ut},
    )
    await conn.execute(
        text(
            "INSERT INTO inventory_items (id, tenant_id, name, inventory_mode, "
            "storage_unit_id, recipe_unit_id) "
            "VALUES (:id, :t, 'GOBELET CARTON', 'recipe_deducted', :u, :u)"
        ),
        {"id": item_id, "t": tid, "u": unit_id},
    )
    return {"tenant_id": tid, "user_id": uid, "item_id": item_id}


async def test_zero_history_unit_fix_succeeds(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed_item(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))
    r = await client.put(
        f"/api/v1/inventory/items/{s['item_id']}/storage-unit",
        json={"storage_unit": "ea"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["storage_unit"] == "ea"
    row = (
        await conn.execute(
            text("""
                SELECT su.name, ru.name FROM inventory_items ii
                JOIN units_of_measure su ON su.id = ii.storage_unit_id
                JOIN units_of_measure ru ON ru.id = ii.recipe_unit_id
               WHERE ii.id = :i
            """),
            {"i": s["item_id"]},
        )
    ).fetchone()
    assert row is not None and row[0] == "ea" and row[1] == "ea"  # resolver invariant kept


async def test_item_with_movements_is_immutable_in_place(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed_item(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))
    await conn.execute(
        text("""
            INSERT INTO inventory_movements
                (id, tenant_id, inventory_item_id, movement_type, delta,
                 source_type, idempotency_key)
            VALUES (:id, :t, :i, 'receive', 1200, 'receipt_line', :k)
        """),
        {"id": uuid.uuid4(), "t": s["tenant_id"], "i": s["item_id"], "k": f"ue-{uuid.uuid4()}"},
    )
    r = await client.put(
        f"/api/v1/inventory/items/{s['item_id']}/storage-unit",
        json={"storage_unit": "ea"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "ITEM_HAS_MOVEMENTS"


async def test_item_in_recipes_is_immutable_in_place(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed_item(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))
    menu_item_id, recipe_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        text("INSERT INTO menu_items (id, tenant_id, name) VALUES (:id, :t, 'Latte')"),
        {"id": menu_item_id, "t": s["tenant_id"]},
    )
    await conn.execute(
        text(
            "INSERT INTO recipes (id, tenant_id, menu_item_id, status) "
            "VALUES (:id, :t, :m, 'draft')"
        ),
        {"id": recipe_id, "t": s["tenant_id"], "m": menu_item_id},
    )
    await conn.execute(
        text(
            "INSERT INTO recipe_versions (id, tenant_id, recipe_id, version_number, "
            "yield_quantity) VALUES (:id, :t, :r, 1, 1)"
        ),
        {"id": version_id, "t": s["tenant_id"], "r": recipe_id},
    )
    await conn.execute(
        text("""
            INSERT INTO recipe_ingredients
                (tenant_id, recipe_version_id, inventory_item_id, quantity, unit)
            VALUES (:t, :v, :i, 0.25, 'ea')
        """),
        {"t": s["tenant_id"], "v": version_id, "i": s["item_id"]},
    )
    r = await client.put(
        f"/api/v1/inventory/items/{s['item_id']}/storage-unit",
        json={"storage_unit": "ea"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "ITEM_IN_RECIPES"


async def test_non_canonical_unit_422_and_staff_403(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed_item(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))
    r = await client.put(
        f"/api/v1/inventory/items/{s['item_id']}/storage-unit",
        json={"storage_unit": "CS"},
    )
    assert r.status_code == 422  # purchase units are not storage units

    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]), role="staff")
    r = await client.put(
        f"/api/v1/inventory/items/{s['item_id']}/storage-unit",
        json={"storage_unit": "ea"},
    )
    assert r.status_code == 403
