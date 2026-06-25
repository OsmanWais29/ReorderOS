"""Sprint 5 Phase 3 — onboarding Recipes API (draft side).

Covers list/get/patch/skip/progress, create-on-first-edit, 409-when-confirmed
(state injected directly since confirm is Phase 4), skip-preserves-draft +
PATCH-reopens-skipped, validation → 400, Manager+/Staff RBAC, cross-tenant 404,
and the strict boundary: Phase 3 writes no recipe_versions/recipe_ingredients.

Uses the bound-transaction harness (make_bound_session + outer rollback), the same
pattern as the inventory/phase7 endpoint tests: the app, the seeds, and the
assertions share one connection/transaction that rolls back on teardown — clean
session lifecycle (no GC warnings) and no committed residue.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from uuid6 import uuid7

from app.core.database import engine, get_db_session, make_bound_session
from app.core.security import Principal, get_principal
from app.main import create_app

R = "/api/v1/onboarding"


@pytest.fixture(scope="module")
def app_instance() -> Any:
    return create_app()


@pytest.fixture
async def conn(app_instance: Any) -> AsyncIterator[AsyncConnection]:
    """Bound transaction shared by app + seeds + assertions; rolled back at end."""
    async with engine.connect() as connection:
        await connection.begin()
        db = make_bound_session(connection)
        app_instance.dependency_overrides[get_db_session] = lambda: db
        try:
            yield connection
        finally:
            app_instance.dependency_overrides.clear()
            await connection.rollback()


@pytest.fixture
async def client(app_instance: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app_instance), base_url="http://test"
    ) as c:
        yield c


def _as(app_instance: Any, tenant_id: str, user_id: str, role: str) -> None:
    app_instance.dependency_overrides[get_principal] = lambda: Principal(
        user_id=user_id,
        workos_id=f"w_{user_id[:8]}",
        email="x@test.com",
        tenant_id=tenant_id,
        role=role,  # type: ignore[arg-type]
    )


async def _seed_tenant_user(conn: AsyncConnection) -> tuple[str, str]:
    tid = str(uuid7())
    await conn.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'T', :slug)"),
        {"id": tid, "slug": f"t-{uuid.uuid4().hex[:8]}"},
    )
    uid = (
        await conn.execute(
            text(
                "INSERT INTO users (workos_id, email, email_verified)"
                " VALUES (:w, :e, true) RETURNING id"
            ),
            {"w": f"u-{uuid.uuid4().hex[:8]}", "e": f"{uuid.uuid4().hex[:8]}@t.com"},
        )
    ).scalar_one()
    return tid, str(uid)


async def _menu_item(conn: AsyncConnection, tid: str, name: str = "Latte") -> str:
    mid = (
        await conn.execute(
            text(
                "INSERT INTO menu_items (tenant_id, name, active)"
                " VALUES (:t, :n, true) RETURNING id"
            ),
            {"t": tid, "n": name},
        )
    ).scalar_one()
    return str(mid)


async def _count(conn: AsyncConnection, sql: str, params: dict[str, Any]) -> int:
    return int((await conn.execute(text(sql), params)).scalar_one())


_ING = {"ingredients": [{"name": "Milk", "quantity": 200, "unit": "ml"}]}


# ── list / get ───────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_list_shows_menu_items_state_none(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "staff")

    resp = await client.get(f"{R}/recipes")
    assert resp.status_code == 200
    items = {i["menu_item_id"]: i for i in resp.json()}
    assert items[mid]["status"] == "none"
    assert items[mid]["ingredient_count"] == 0
    assert items[mid]["volume_30d"] == 0


# ── patch: create-on-first-edit + boundary ───────────────────────────────────


@pytest.mark.integration
async def test_patch_creates_recipe_and_draft_only(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")

    resp = await client.patch(f"{R}/recipes/{mid}", json=_ING)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "draft"
    assert body["ingredients"][0]["unit"] == "ml"

    assert await _count(
        conn,
        "SELECT count(*) FROM recipes WHERE tenant_id=:t AND menu_item_id=:m AND status='draft'",
        {"t": tid, "m": mid},
    ) == 1
    assert await _count(
        conn,
        "SELECT count(*) FROM recipe_drafts rd JOIN recipes r ON r.id=rd.recipe_id"
        " WHERE r.tenant_id=:t AND r.menu_item_id=:m",
        {"t": tid, "m": mid},
    ) == 1
    # strict boundary: Phase 3 writes nothing to the version tables
    assert await _count(conn, "SELECT count(*) FROM recipe_versions WHERE tenant_id=:t", {"t": tid}) == 0
    assert await _count(conn, "SELECT count(*) FROM recipe_ingredients WHERE tenant_id=:t", {"t": tid}) == 0


@pytest.mark.integration
async def test_patch_twice_updates_single_draft(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")

    await client.patch(f"{R}/recipes/{mid}", json={"ingredients": [{"name": "Milk", "quantity": 100, "unit": "ml"}]})
    await client.patch(f"{R}/recipes/{mid}", json={"ingredients": [{"name": "Milk", "quantity": 250, "unit": "ml"}]})

    assert await _count(
        conn,
        "SELECT count(*) FROM recipe_drafts rd JOIN recipes r ON r.id=rd.recipe_id"
        " WHERE r.tenant_id=:t AND r.menu_item_id=:m",
        {"t": tid, "m": mid},
    ) == 1
    detail = (await client.get(f"{R}/recipes/{mid}")).json()
    assert detail["ingredients"][0]["quantity"] == 250


# ── validation → 400 ─────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.parametrize(
    "ingredient",
    [
        {"name": "X", "quantity": 1, "unit": "oz"},  # non-canonical unit
        {"name": "X", "quantity": 0, "unit": "g"},  # qty not > 0
        {"name": "  ", "quantity": 1, "unit": "g"},  # empty name
        {"name": "X", "quantity": 1e12, "unit": "g"},  # exceeds sanity ceiling
    ],
)
async def test_patch_validation_400(app_instance, conn, client, ingredient) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    resp = await client.patch(f"{R}/recipes/{mid}", json={"ingredients": [ingredient]})
    assert resp.status_code == 400


@pytest.mark.integration
@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
async def test_patch_rejects_nonfinite_quantity(app_instance, conn, client, token) -> None:
    """Non-finite quantities slip past the `<= 0` guard (every NaN compare is False,
    inf <= 0 is False). A conformant JSON client can't emit them, but Python's
    json.loads (what Starlette uses) accepts the bare NaN/Infinity tokens in a raw
    body — so post raw content to exercise the real attack path. Must 400, not 500."""
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    body = f'{{"ingredients": [{{"name": "X", "quantity": {token}, "unit": "g"}}]}}'
    resp = await client.patch(
        f"{R}/recipes/{mid}", content=body, headers={"content-type": "application/json"}
    )
    assert resp.status_code == 400


# ── 409 when confirmed (state injected; confirm is Phase 4) ───────────────────


@pytest.mark.integration
async def test_patch_409_when_confirmed(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    await conn.execute(
        text("INSERT INTO recipes (tenant_id, menu_item_id, status) VALUES (:t, :m, 'confirmed')"),
        {"t": tid, "m": mid},
    )
    _as(app_instance, tid, uid, "manager")
    resp = await client.patch(f"{R}/recipes/{mid}", json=_ING)
    assert resp.status_code == 409


# ── skip preserves draft; PATCH reopens skipped → draft ──────────────────────


@pytest.mark.integration
async def test_skip_preserves_draft_and_patch_reopens(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")

    await client.patch(f"{R}/recipes/{mid}", json={"ingredients": [{"name": "Milk", "quantity": 100, "unit": "ml"}]})
    skip = await client.post(f"{R}/recipes/{mid}/skip")
    assert skip.status_code == 200 and skip.json()["status"] == "skipped"
    assert await _count(
        conn,
        "SELECT count(*) FROM recipe_drafts rd JOIN recipes r ON r.id=rd.recipe_id"
        " WHERE r.tenant_id=:t AND r.menu_item_id=:m",
        {"t": tid, "m": mid},
    ) == 1  # draft preserved
    reopened = await client.patch(
        f"{R}/recipes/{mid}", json={"ingredients": [{"name": "Milk", "quantity": 150, "unit": "ml"}]}
    )
    assert reopened.status_code == 200 and reopened.json()["status"] == "draft"


# ── progress (incl. zero-denominator → null) ─────────────────────────────────


@pytest.mark.integration
async def test_progress_counts_and_zero_denominator(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid, "OnlyItem")
    _as(app_instance, tid, uid, "manager")

    await client.post(f"{R}/recipes/{mid}/skip")  # total 1, skipped 1 -> denom 0
    prog = (await client.get(f"{R}/progress")).json()
    assert prog["total"] == 1
    assert prog["skipped"] == 1
    assert prog["denominator"] == 0
    assert prog["percent"] is None


# ── RBAC ─────────────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_staff_can_read_cannot_write(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "staff")

    assert (await client.get(f"{R}/recipes")).status_code == 200
    assert (await client.get(f"{R}/progress")).status_code == 200
    assert (await client.patch(f"{R}/recipes/{mid}", json=_ING)).status_code == 403
    assert (await client.post(f"{R}/recipes/{mid}/skip")).status_code == 403


# ── cross-tenant isolation (404, no existence leak) ──────────────────────────


@pytest.mark.integration
async def test_cross_tenant_returns_404(app_instance, conn, client) -> None:
    tid_b, _ = await _seed_tenant_user(conn)
    mid_b = await _menu_item(conn, tid_b, "B-item")
    tid_a, uid_a = await _seed_tenant_user(conn)
    _as(app_instance, tid_a, uid_a, "manager")

    assert (await client.get(f"{R}/recipes/{mid_b}")).status_code == 404
    assert (await client.patch(f"{R}/recipes/{mid_b}", json=_ING)).status_code == 404
    assert (await client.post(f"{R}/recipes/{mid_b}/skip")).status_code == 404
    assert await _count(conn, "SELECT count(*) FROM recipes WHERE menu_item_id=:m", {"m": mid_b}) == 0
