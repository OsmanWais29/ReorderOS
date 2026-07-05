"""Sprint 5 Phase 6 — modifier configuration + confirm/un-confirm.

Modifier confirm = recipe confirm with the nouns swapped, so this mirrors the Phase 4
suite: bound-transaction harness for HTTP/semantics, real-transaction + admin_conn for
the no-partial-confirm atomicity. Adds the modifier-specific coverage: the inherited
four-way PATCH branch (no-draft/draft/skipped→reopen/confirmed→409), additive-only
confirmability, and skip as the disposition for non-additive modifiers.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from uuid6 import uuid7

from app.core.database import engine, get_db_session, make_bound_session
from app.core.security import Principal, get_principal
from app.main import create_app
from app.modules.recipes import modifiers_repo as mrepo
from app.modules.recipes.repo import UnitTypeConflict

R = "/api/v1/onboarding/recipes"


# ── bound-transaction harness ────────────────────────────────────────────────


@pytest.fixture(scope="module")
def app_instance() -> Any:
    return create_app()


@pytest.fixture
async def conn(app_instance: Any) -> AsyncIterator[AsyncConnection]:
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
    async with AsyncClient(transport=ASGITransport(app=app_instance), base_url="http://test") as c:
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
                "INSERT INTO menu_items (tenant_id, name, active) VALUES (:t, :n, true) RETURNING id"
            ),
            {"t": tid, "n": name},
        )
    ).scalar_one()
    return str(mid)


async def _modifier(
    conn: AsyncConnection, tid: str, mid: str, name: str = "Extra shot", mtype: str = "additive"
) -> str:
    rid = (
        await conn.execute(
            text(
                "INSERT INTO modifiers (tenant_id, menu_item_id, name, modifier_type, status)"
                " VALUES (:t, :m, :n, :ty, 'draft') RETURNING id"
            ),
            {"t": tid, "m": mid, "n": name, "ty": mtype},
        )
    ).scalar_one()
    return str(rid)


async def _count(conn: AsyncConnection, sql: str, params: dict[str, Any]) -> int:
    return int((await conn.execute(text(sql), params)).scalar_one())


_ING = {"ingredients": [{"name": "Espresso", "quantity": 7, "unit": "g"}]}


def _u(menu_item_id: str, modifier_id: str, suffix: str = "") -> str:
    return f"{R}/{menu_item_id}/modifiers/{modifier_id}{suffix}"


async def _patch(client: AsyncClient, mid: str, modid: str, body: dict[str, Any]) -> Any:
    return await client.patch(_u(mid, modid), json=body)


# ── list ─────────────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_list_modifiers(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid)
    _as(app_instance, tid, uid, "staff")
    resp = await client.get(f"{R}/{mid}/modifiers")
    assert resp.status_code == 200
    items = resp.json()
    assert items[0]["modifier_id"] == modid
    assert items[0]["modifier_type"] == "additive"
    assert items[0]["status"] == "draft"
    assert items[0]["ingredient_count"] == 0


# ── PATCH: the inherited four-way state branch ───────────────────────────────


@pytest.mark.integration
async def test_patch_creates_draft_first_edit(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid)
    _as(app_instance, tid, uid, "manager")
    resp = await _patch(client, mid, modid, _ING)
    assert resp.status_code == 200
    assert resp.json()["status"] == "draft"
    assert (
        await _count(
            conn, "SELECT count(*) FROM modifier_drafts WHERE modifier_id=:m", {"m": modid}
        )
        == 1
    )


@pytest.mark.integration
async def test_patch_twice_single_draft(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid)
    _as(app_instance, tid, uid, "manager")
    await _patch(
        client, mid, modid, {"ingredients": [{"name": "Espresso", "quantity": 7, "unit": "g"}]}
    )
    await _patch(
        client, mid, modid, {"ingredients": [{"name": "Espresso", "quantity": 14, "unit": "g"}]}
    )
    assert (
        await _count(
            conn, "SELECT count(*) FROM modifier_drafts WHERE modifier_id=:m", {"m": modid}
        )
        == 1
    )


@pytest.mark.integration
async def test_patch_on_skipped_reopens_to_draft(app_instance, conn, client) -> None:
    """Correction 2: editing a skipped modifier is the implicit un-skip."""
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid)
    _as(app_instance, tid, uid, "manager")
    await _patch(client, mid, modid, _ING)
    assert (await client.post(_u(mid, modid, "/skip"))).status_code == 200
    reopened = await _patch(
        client, mid, modid, {"ingredients": [{"name": "Espresso", "quantity": 9, "unit": "g"}]}
    )
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "draft"
    assert (
        await _count(
            conn, "SELECT count(*) FROM modifiers WHERE id=:m AND status='draft'", {"m": modid}
        )
        == 1
    )


@pytest.mark.integration
async def test_patch_on_confirmed_409(app_instance, conn, client) -> None:
    """Correction 1: confirmed has no draft (deleted at confirm) — un-confirm first."""
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid)
    _as(app_instance, tid, uid, "manager")
    await _patch(client, mid, modid, _ING)
    assert (await client.post(_u(mid, modid, "/confirm"))).status_code == 200
    assert (await _patch(client, mid, modid, _ING)).status_code == 409


# ── confirm ──────────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_confirm_happy_links_via_composite_fk(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid)
    _as(app_instance, tid, uid, "manager")
    await _patch(client, mid, modid, _ING)

    resp = await client.post(_u(mid, modid, "/confirm"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "confirmed"

    mv = (
        (
            await conn.execute(
                text(
                    "SELECT id, version_number FROM modifier_versions WHERE tenant_id=:t AND modifier_id=:m"
                ),
                {"t": tid, "m": modid},
            )
        )
        .mappings()
        .all()
    )
    assert len(mv) == 1 and mv[0]["version_number"] == 1
    # linked through the composite FK; status confirmed; draft gone
    assert (
        await _count(
            conn,
            "SELECT count(*) FROM modifiers WHERE id=:m AND current_version_id=:v AND status='confirmed'",
            {"m": modid, "v": mv[0]["id"]},
        )
        == 1
    )
    assert (
        await _count(
            conn,
            "SELECT count(*) FROM modifier_ingredients WHERE modifier_version_id=:v AND tenant_id=:t",
            {"v": mv[0]["id"], "t": tid},
        )
        == 1
    )
    assert (
        await _count(
            conn, "SELECT count(*) FROM modifier_drafts WHERE modifier_id=:m", {"m": modid}
        )
        == 0
    )


@pytest.mark.integration
async def test_double_click_confirm_one_version_then_409(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid)
    _as(app_instance, tid, uid, "manager")
    await _patch(client, mid, modid, _ING)
    assert (await client.post(_u(mid, modid, "/confirm"))).status_code == 200
    assert (await client.post(_u(mid, modid, "/confirm"))).status_code == 409
    assert (
        await _count(
            conn,
            "SELECT count(*) FROM modifier_versions WHERE tenant_id=:t AND modifier_id=:m",
            {"t": tid, "m": modid},
        )
        == 1
    )


@pytest.mark.integration
async def test_reconfirm_allocates_version_two(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid)
    _as(app_instance, tid, uid, "manager")
    await _patch(client, mid, modid, _ING)
    assert (await client.post(_u(mid, modid, "/confirm"))).status_code == 200
    v1 = (
        await conn.execute(
            text("SELECT id FROM modifier_versions WHERE tenant_id=:t AND version_number=1"),
            {"t": tid},
        )
    ).scalar_one()
    assert (await client.post(_u(mid, modid, "/unconfirm"))).status_code == 200
    await _patch(
        client, mid, modid, {"ingredients": [{"name": "Espresso", "quantity": 21, "unit": "g"}]}
    )
    assert (await client.post(_u(mid, modid, "/confirm"))).status_code == 200

    versions = [
        r["version_number"]
        for r in (
            await conn.execute(
                text(
                    "SELECT version_number FROM modifier_versions WHERE tenant_id=:t ORDER BY version_number"
                ),
                {"t": tid},
            )
        )
        .mappings()
        .all()
    ]
    assert versions == [1, 2]
    assert await _count(conn, "SELECT count(*) FROM modifier_versions WHERE id=:v", {"v": v1}) == 1


@pytest.mark.integration
async def test_non_additive_cannot_confirm(app_instance, conn, client) -> None:
    """Guard 2: subtractive/substitution modifiers cannot confirm into a depleting version."""
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid, name="No foam", mtype="subtractive")
    _as(app_instance, tid, uid, "manager")
    await _patch(client, mid, modid, _ING)  # PATCH is type-agnostic; confirm is the gate
    resp = await client.post(_u(mid, modid, "/confirm"))
    assert resp.status_code == 409
    assert (
        await _count(conn, "SELECT count(*) FROM modifier_versions WHERE tenant_id=:t", {"t": tid})
        == 0
    )


@pytest.mark.integration
async def test_confirm_no_draft_400(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid)
    _as(app_instance, tid, uid, "manager")
    assert (await client.post(_u(mid, modid, "/confirm"))).status_code == 400


@pytest.mark.integration
async def test_confirm_duplicate_ingredient_400(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid)
    _as(app_instance, tid, uid, "manager")
    await _patch(
        client,
        mid,
        modid,
        {
            "ingredients": [
                {"name": "Espresso", "quantity": 7, "unit": "g"},
                {"name": " espresso ", "quantity": 3, "unit": "g"},
            ]
        },
    )
    assert (await client.post(_u(mid, modid, "/confirm"))).status_code == 400


# ── un-confirm + skip ────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_unconfirm_copies_draft_version_immutable(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid)
    _as(app_instance, tid, uid, "manager")
    await _patch(client, mid, modid, _ING)
    assert (await client.post(_u(mid, modid, "/confirm"))).status_code == 200
    mv = (
        await conn.execute(
            text("SELECT id FROM modifier_versions WHERE tenant_id=:t AND modifier_id=:m"),
            {"t": tid, "m": modid},
        )
    ).scalar_one()
    ing_before = [
        (str(r["inventory_item_id"]), float(r["quantity"]), r["unit"])
        for r in (
            await conn.execute(
                text(
                    "SELECT inventory_item_id, quantity, unit FROM modifier_ingredients WHERE modifier_version_id=:v"
                ),
                {"v": mv},
            )
        )
        .mappings()
        .all()
    ]

    assert (await client.post(_u(mid, modid, "/unconfirm"))).status_code == 200
    # version + ingredients byte-identical; status draft; link cleared; draft copy parented at v1
    assert await _count(conn, "SELECT count(*) FROM modifier_versions WHERE id=:v", {"v": mv}) == 1
    ing_after = [
        (str(r["inventory_item_id"]), float(r["quantity"]), r["unit"])
        for r in (
            await conn.execute(
                text(
                    "SELECT inventory_item_id, quantity, unit FROM modifier_ingredients WHERE modifier_version_id=:v"
                ),
                {"v": mv},
            )
        )
        .mappings()
        .all()
    ]
    assert ing_after == ing_before
    assert (
        await _count(
            conn,
            "SELECT count(*) FROM modifiers WHERE id=:m AND status='draft' AND current_version_id IS NULL",
            {"m": modid},
        )
        == 1
    )
    assert (
        await _count(
            conn,
            "SELECT count(*) FROM modifier_drafts WHERE modifier_id=:m AND parent_modifier_version_id=:v",
            {"m": modid, "v": mv},
        )
        == 1
    )


@pytest.mark.integration
async def test_double_unconfirm_409(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid)
    _as(app_instance, tid, uid, "manager")
    await _patch(client, mid, modid, _ING)
    assert (await client.post(_u(mid, modid, "/confirm"))).status_code == 200
    assert (await client.post(_u(mid, modid, "/unconfirm"))).status_code == 200
    assert (await client.post(_u(mid, modid, "/unconfirm"))).status_code == 409


@pytest.mark.integration
async def test_skip_on_confirmed_409(app_instance, conn, client) -> None:
    """Skip is for un-configured modifiers; a confirmed one must be un-confirmed first
    (else 'skipped' would coexist with a live current_version_id)."""
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid)
    _as(app_instance, tid, uid, "manager")
    await _patch(client, mid, modid, _ING)
    assert (await client.post(_u(mid, modid, "/confirm"))).status_code == 200
    assert (await client.post(_u(mid, modid, "/skip"))).status_code == 409
    # the version link is intact (skip was rejected, not half-applied)
    assert (
        await _count(
            conn,
            "SELECT count(*) FROM modifiers WHERE id=:m AND status='confirmed'"
            " AND current_version_id IS NOT NULL",
            {"m": modid},
        )
        == 1
    )


@pytest.mark.integration
async def test_skip_preserves_draft_on_subtractive(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid, name="No foam", mtype="subtractive")
    _as(app_instance, tid, uid, "manager")
    await _patch(client, mid, modid, _ING)
    skip = await client.post(_u(mid, modid, "/skip"))
    assert skip.status_code == 200 and skip.json()["status"] == "skipped"
    assert (
        await _count(
            conn, "SELECT count(*) FROM modifier_drafts WHERE modifier_id=:m", {"m": modid}
        )
        == 1
    )


# ── RBAC + cross-scope ───────────────────────────────────────────────────────


@pytest.mark.integration
async def test_staff_cannot_write(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid)
    _as(app_instance, tid, uid, "staff")
    assert (await client.get(f"{R}/{mid}/modifiers")).status_code == 200
    assert (await _patch(client, mid, modid, _ING)).status_code == 403
    assert (await client.post(_u(mid, modid, "/confirm"))).status_code == 403


@pytest.mark.integration
async def test_cross_tenant_and_cross_item_404(app_instance, conn, client) -> None:
    tid_b, _ = await _seed_tenant_user(conn)
    mid_b = await _menu_item(conn, tid_b, "B-item")
    modid_b = await _modifier(conn, tid_b, mid_b)
    tid_a, uid_a = await _seed_tenant_user(conn)
    mid_a = await _menu_item(conn, tid_a, "A-item")
    modid_a = await _modifier(conn, tid_a, mid_a)
    _as(app_instance, tid_a, uid_a, "manager")

    # cross-tenant modifier → 404
    assert (await client.post(_u(mid_b, modid_b, "/confirm"))).status_code == 404
    # own modifier but WRONG parent menu item in path → 404 (guard 1)
    assert (await _patch(client, mid_b, modid_a, _ING)).status_code == 404


# ── real-transaction no-partial-confirm ──────────────────────────────────────


async def _seed_mod_for_confirm(
    admin_conn: Any,
    ingredients: list[dict[str, Any]],
    *,
    mtype: str = "additive",
    extra_units: tuple[dict[str, str], ...] = (),
) -> tuple[str, str, str, str]:
    tid = str(uuid7())
    await admin_conn.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, 'T', $2)",
        tid,
        f"t-{uuid.uuid4().hex[:8]}",
    )
    uid = str(
        await admin_conn.fetchval(
            "INSERT INTO users (workos_id, email, email_verified) VALUES ($1, $2, true) RETURNING id",
            f"u-{uuid.uuid4().hex[:8]}",
            f"{uuid.uuid4().hex[:8]}@t.com",
        )
    )
    mid = str(
        await admin_conn.fetchval(
            "INSERT INTO menu_items (tenant_id, name, active) VALUES ($1, 'Latte', true) RETURNING id",
            tid,
        )
    )
    modid = str(
        await admin_conn.fetchval(
            "INSERT INTO modifiers (tenant_id, menu_item_id, name, modifier_type, status)"
            " VALUES ($1, $2, 'Extra shot', $3, 'draft') RETURNING id",
            tid,
            mid,
            mtype,
        )
    )
    await admin_conn.execute(
        "INSERT INTO modifier_drafts (tenant_id, modifier_id, draft_ingredients, created_by)"
        " VALUES ($1, $2, $3::jsonb, $4)",
        tid,
        modid,
        json.dumps(ingredients),
        uid,
    )
    for u in extra_units:
        await admin_conn.execute(
            "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type)"
            " VALUES ($1, $2, $2, $3)",
            tid,
            u["name"],
            u["unit_type"],
        )
    return tid, uid, mid, modid


async def _cleanup(admin_conn: Any, tid: str, uid: str) -> None:
    for tbl in ("modifier_ingredients", "modifier_versions", "modifier_drafts"):
        await admin_conn.execute(f"DELETE FROM {tbl} WHERE tenant_id=$1", tid)
    await admin_conn.execute("UPDATE modifiers SET current_version_id=NULL WHERE tenant_id=$1", tid)
    for tbl in ("modifiers", "inventory_items", "units_of_measure", "menu_items"):
        await admin_conn.execute(f"DELETE FROM {tbl} WHERE tenant_id=$1", tid)
    await admin_conn.execute("DELETE FROM users WHERE id=$1", uid)
    await admin_conn.execute("DELETE FROM tenants WHERE id=$1", tid)


async def _run_confirm_in_own_txn(
    tid: str, mid: str, modid: str, uid: str, *, inject_late: bool = False
) -> BaseException | None:
    async with engine.connect() as c:
        trans = await c.begin()
        db = make_bound_session(c)
        if inject_late:
            orig = db.execute

            async def boom(statement: Any, *a: Any, **k: Any) -> Any:
                if "UPDATE modifiers" in str(statement):
                    raise RuntimeError("injected late failure")
                return await orig(statement, *a, **k)

            db.execute = boom  # type: ignore[method-assign]
        try:
            await mrepo.confirm_modifier(db, UUID(tid), UUID(mid), UUID(modid), UUID(uid))
            await trans.commit()
            return None
        except BaseException as exc:  # capture for assertion, then real rollback
            await trans.rollback()
            return exc
        finally:
            await db.close()


@pytest.mark.integration
async def test_no_partial_confirm_unit_type_conflict(admin_conn) -> None:
    tid, uid, mid, modid = await _seed_mod_for_confirm(
        admin_conn,
        [
            {"name": "Espresso", "quantity": 7, "unit": "g"},
            {"name": "Syrup", "quantity": 10, "unit": "ml"},
        ],
        extra_units=({"name": "ml", "unit_type": "weight"},),  # wrong type → abort in step 1
    )
    try:
        exc = await _run_confirm_in_own_txn(tid, mid, modid, uid)
        assert isinstance(exc, UnitTypeConflict)
        assert (
            await admin_conn.fetchval(
                "SELECT count(*) FROM modifier_versions WHERE tenant_id=$1", tid
            )
            == 0
        )
        assert (
            await admin_conn.fetchval(
                "SELECT count(*) FROM inventory_items WHERE tenant_id=$1", tid
            )
            == 0
        )
        assert (
            await admin_conn.fetchval(
                "SELECT count(*) FROM units_of_measure WHERE tenant_id=$1 AND name='g'", tid
            )
            == 0
        )
        assert (
            await admin_conn.fetchval(
                "SELECT count(*) FROM modifier_drafts WHERE modifier_id=$1", modid
            )
            == 1
        )
        assert (
            await admin_conn.fetchval("SELECT status FROM modifiers WHERE id=$1", modid) == "draft"
        )
    finally:
        await _cleanup(admin_conn, tid, uid)


@pytest.mark.integration
async def test_no_partial_confirm_injected_late_failure(admin_conn) -> None:
    tid, uid, mid, modid = await _seed_mod_for_confirm(
        admin_conn, [{"name": "Espresso", "quantity": 7, "unit": "g"}]
    )
    try:
        exc = await _run_confirm_in_own_txn(tid, mid, modid, uid, inject_late=True)
        assert isinstance(exc, RuntimeError)
        assert (
            await admin_conn.fetchval(
                "SELECT count(*) FROM modifier_versions WHERE tenant_id=$1", tid
            )
            == 0
        )
        assert (
            await admin_conn.fetchval(
                "SELECT count(*) FROM modifier_ingredients WHERE tenant_id=$1", tid
            )
            == 0
        )
        assert (
            await admin_conn.fetchval(
                "SELECT count(*) FROM inventory_items WHERE tenant_id=$1", tid
            )
            == 0
        )
        assert (
            await admin_conn.fetchval(
                "SELECT count(*) FROM modifier_drafts WHERE modifier_id=$1", modid
            )
            == 1
        )
        assert (
            await admin_conn.fetchval("SELECT status FROM modifiers WHERE id=$1", modid) == "draft"
        )
        assert (
            await admin_conn.fetchval("SELECT current_version_id FROM modifiers WHERE id=$1", modid)
            is None
        )
    finally:
        await _cleanup(admin_conn, tid, uid)
