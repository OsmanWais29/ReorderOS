"""Sprint 5 Phase 4 — recipe confirm / un-confirm engine.

Two harnesses, each chosen for what it can actually prove:

* Bound-transaction harness (conn + client) — happy path, the corrected double-click
  semantics (one version, then 409), un-confirm + version immutability, RBAC,
  cross-tenant 404, validation. App + seeds + assertions share one rolled-back
  transaction (no residue), exactly like Phase 3.

* Real-transaction harness (admin_conn seed + a true engine transaction that rolls
  back) — the no-partial-confirm atomicity tests. The bound harness shares ONE
  transaction with the app, so an aborted confirm's writes would still be visible to
  the assertions; only a real commit/rollback boundary proves they vanish. The
  confirm runs in its own engine transaction; admin_conn (a separate connection)
  asserts nothing persisted, then cleans up the committed seed.
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
from app.modules.recipes import repo

R = "/api/v1/onboarding"


# ── bound-transaction harness (same as Phase 3) ──────────────────────────────


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


_TWO = {
    "ingredients": [
        {"name": "Flour", "quantity": 100, "unit": "g"},
        {"name": "Milk", "quantity": 200, "unit": "ml"},
    ]
}


async def _patch_draft(client: AsyncClient, mid: str, body: dict[str, Any]) -> None:
    resp = await client.patch(f"{R}/recipes/{mid}", json=body)
    assert resp.status_code == 200, resp.text


# ── confirm: happy path ──────────────────────────────────────────────────────


@pytest.mark.integration
async def test_confirm_happy_creates_version_and_links(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    await _patch_draft(client, mid, _TWO)

    resp = await client.post(f"{R}/recipes/{mid}/confirm")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "confirmed"
    assert {i["name"] for i in body["ingredients"]} == {"Flour", "Milk"}
    assert all(i["inventory_item_id"] for i in body["ingredients"])

    # one version, number 1, tenant-scoped
    rv = (
        await conn.execute(
            text(
                "SELECT id, version_number, tenant_id, name FROM recipe_versions"
                " WHERE tenant_id = :t"
            ),
            {"t": tid},
        )
    ).mappings().all()
    assert len(rv) == 1
    assert rv[0]["version_number"] == 1
    assert str(rv[0]["tenant_id"]) == tid
    rv_id = rv[0]["id"]

    # ingredients carry the right tenant_id; menu item linked; status confirmed; draft gone
    assert await _count(
        conn,
        "SELECT count(*) FROM recipe_ingredients WHERE recipe_version_id=:r AND tenant_id=:t",
        {"r": rv_id, "t": tid},
    ) == 2
    assert await _count(
        conn, "SELECT count(*) FROM menu_items WHERE id=:m AND recipe_version_id=:r",
        {"m": mid, "r": rv_id},
    ) == 1
    assert await _count(
        conn, "SELECT count(*) FROM recipes WHERE menu_item_id=:m AND status='confirmed'",
        {"m": mid},
    ) == 1
    assert await _count(
        conn,
        "SELECT count(*) FROM recipe_drafts rd JOIN recipes r ON r.id=rd.recipe_id"
        " WHERE r.menu_item_id=:m",
        {"m": mid},
    ) == 0
    # inventory items + units auto-created (deduped) for both ingredients
    assert await _count(
        conn, "SELECT count(*) FROM inventory_items WHERE tenant_id=:t", {"t": tid}
    ) == 2
    assert await _count(
        conn, "SELECT count(*) FROM units_of_measure WHERE tenant_id=:t AND name IN ('g','ml')",
        {"t": tid},
    ) == 2


# ── confirm: corrected double-click semantics (one version, then 409) ────────


@pytest.mark.integration
async def test_double_click_confirm_one_version_then_409(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    await _patch_draft(client, mid, _TWO)

    first = await client.post(f"{R}/recipes/{mid}/confirm")
    assert first.status_code == 200
    second = await client.post(f"{R}/recipes/{mid}/confirm")
    assert second.status_code == 409  # not a spurious version 2

    assert await _count(
        conn, "SELECT count(*) FROM recipe_versions WHERE tenant_id=:t", {"t": tid}
    ) == 1


# ── confirm: legitimate version 2 (un-confirm → edit → re-confirm) ───────────


@pytest.mark.integration
async def test_reconfirm_allocates_version_two(app_instance, conn, client) -> None:
    """The positive case of the hard problem: a genuine re-confirm allocates
    version_number=2 (MAX+1 under the lock) while v1 survives byte-identical and the
    menu item is re-pointed at v2."""
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    await _patch_draft(client, mid, _TWO)
    assert (await client.post(f"{R}/recipes/{mid}/confirm")).status_code == 200

    v1_id = (
        await conn.execute(
            text("SELECT id FROM recipe_versions WHERE tenant_id=:t AND version_number=1"),
            {"t": tid},
        )
    ).scalar_one()
    v1_ings = await _count(
        conn, "SELECT count(*) FROM recipe_ingredients WHERE recipe_version_id=:r",
        {"r": v1_id},
    )

    assert (await client.post(f"{R}/recipes/{mid}/unconfirm")).status_code == 200
    # edit the re-opened draft, then re-confirm
    await _patch_draft(
        client,
        mid,
        {
            "ingredients": [
                {"name": "Flour", "quantity": 100, "unit": "g"},
                {"name": "Milk", "quantity": 250, "unit": "ml"},  # changed
            ]
        },
    )
    assert (await client.post(f"{R}/recipes/{mid}/confirm")).status_code == 200

    versions = [
        r["version_number"]
        for r in (
            await conn.execute(
                text(
                    "SELECT version_number FROM recipe_versions"
                    " WHERE tenant_id=:t ORDER BY version_number"
                ),
                {"t": tid},
            )
        ).mappings().all()
    ]
    assert versions == [1, 2]  # MAX+1 incremented, not collided
    # v1 survived byte-identical
    assert await _count(
        conn, "SELECT count(*) FROM recipe_versions WHERE id=:r AND version_number=1",
        {"r": v1_id},
    ) == 1
    assert await _count(
        conn, "SELECT count(*) FROM recipe_ingredients WHERE recipe_version_id=:r",
        {"r": v1_id},
    ) == v1_ings
    # menu item now points at v2; draft consumed
    v2_id = (
        await conn.execute(
            text("SELECT id FROM recipe_versions WHERE tenant_id=:t AND version_number=2"),
            {"t": tid},
        )
    ).scalar_one()
    assert await _count(
        conn, "SELECT count(*) FROM menu_items WHERE id=:m AND recipe_version_id=:r",
        {"m": mid, "r": v2_id},
    ) == 1
    assert await _count(
        conn,
        "SELECT count(*) FROM recipe_drafts rd JOIN recipes r ON r.id=rd.recipe_id"
        " WHERE r.menu_item_id=:m",
        {"m": mid},
    ) == 0


# ── confirm: case-insensitive dedup LINK (what 0019 exists for) ──────────────


@pytest.mark.integration
async def test_confirm_dedups_inventory_item_case_insensitively(
    app_instance, conn, client
) -> None:
    """Two recipes whose ingredient names differ only in case/space link to ONE
    inventory_item via the 0019 unique index — the dedup-link the constraint exists
    for (distinct from the within-draft duplicate rejection)."""
    tid, uid = await _seed_tenant_user(conn)
    mid1 = await _menu_item(conn, tid, "Bruschetta")
    mid2 = await _menu_item(conn, tid, "Marinara")
    _as(app_instance, tid, uid, "manager")
    await _patch_draft(client, mid1, {"ingredients": [{"name": "Tomato", "quantity": 50, "unit": "g"}]})
    await _patch_draft(client, mid2, {"ingredients": [{"name": " tomato ", "quantity": 30, "unit": "g"}]})

    assert (await client.post(f"{R}/recipes/{mid1}/confirm")).status_code == 200
    assert (await client.post(f"{R}/recipes/{mid2}/confirm")).status_code == 200

    # exactly one inventory_item for the normalized name
    assert await _count(
        conn,
        "SELECT count(*) FROM inventory_items"
        " WHERE tenant_id=:t AND lower(btrim(name))='tomato'",
        {"t": tid},
    ) == 1
    item_id = (
        await conn.execute(
            text(
                "SELECT id FROM inventory_items"
                " WHERE tenant_id=:t AND lower(btrim(name))='tomato'"
            ),
            {"t": tid},
        )
    ).scalar_one()
    # both versions' ingredients point at that one item
    assert await _count(
        conn,
        "SELECT count(*) FROM recipe_ingredients WHERE tenant_id=:t AND inventory_item_id=:i",
        {"t": tid, "i": item_id},
    ) == 2


# ── confirm: rejections ──────────────────────────────────────────────────────


@pytest.mark.integration
async def test_confirm_no_draft_400(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    resp = await client.post(f"{R}/recipes/{mid}/confirm")
    assert resp.status_code == 400


@pytest.mark.integration
async def test_confirm_duplicate_ingredient_400(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    # "Flour" and " flour " both normalize to "flour" → would collide on dedup
    await _patch_draft(
        client,
        mid,
        {
            "ingredients": [
                {"name": "Flour", "quantity": 100, "unit": "g"},
                {"name": " flour ", "quantity": 50, "unit": "g"},
            ]
        },
    )
    resp = await client.post(f"{R}/recipes/{mid}/confirm")
    assert resp.status_code == 400
    assert await _count(
        conn, "SELECT count(*) FROM recipe_versions WHERE tenant_id=:t", {"t": tid}
    ) == 0


@pytest.mark.integration
async def test_confirm_skipped_409(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    await _patch_draft(client, mid, _TWO)
    assert (await client.post(f"{R}/recipes/{mid}/skip")).status_code == 200
    resp = await client.post(f"{R}/recipes/{mid}/confirm")
    assert resp.status_code == 409  # un-skip first


@pytest.mark.integration
async def test_patch_409_after_real_confirm(app_instance, conn, client) -> None:
    """Phase 3 tested the PATCH→409 guard against an injected confirmed row; now
    re-test it against a genuinely confirmed recipe (the state Phase 4 produces)."""
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    await _patch_draft(client, mid, _TWO)
    assert (await client.post(f"{R}/recipes/{mid}/confirm")).status_code == 200
    resp = await client.patch(f"{R}/recipes/{mid}", json=_TWO)
    assert resp.status_code == 409


# ── un-confirm: draft copy + version immutability ────────────────────────────


@pytest.mark.integration
async def test_unconfirm_copies_draft_and_keeps_version_immutable(
    app_instance, conn, client
) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    await _patch_draft(client, mid, _TWO)
    assert (await client.post(f"{R}/recipes/{mid}/confirm")).status_code == 200

    rv = (
        await conn.execute(
            text("SELECT id, version_number, name FROM recipe_versions WHERE tenant_id=:t"),
            {"t": tid},
        )
    ).mappings().one()
    rv_id = rv["id"]
    ri_before = [
        (str(r["inventory_item_id"]), float(r["quantity"]), r["unit"])
        for r in (
            await conn.execute(
                text(
                    "SELECT inventory_item_id, quantity, unit FROM recipe_ingredients"
                    " WHERE recipe_version_id=:r ORDER BY unit"
                ),
                {"r": rv_id},
            )
        ).mappings().all()
    ]

    unc = await client.post(f"{R}/recipes/{mid}/unconfirm")
    assert unc.status_code == 200
    assert unc.json()["status"] == "draft"

    # the version + its ingredients are byte-identical (never mutated)
    rv_after = (
        await conn.execute(
            text("SELECT id, version_number, name FROM recipe_versions WHERE tenant_id=:t"),
            {"t": tid},
        )
    ).mappings().all()
    assert len(rv_after) == 1
    assert rv_after[0]["id"] == rv_id
    assert rv_after[0]["version_number"] == rv["version_number"]
    assert rv_after[0]["name"] == rv["name"]
    ri_after = [
        (str(r["inventory_item_id"]), float(r["quantity"]), r["unit"])
        for r in (
            await conn.execute(
                text(
                    "SELECT inventory_item_id, quantity, unit FROM recipe_ingredients"
                    " WHERE recipe_version_id=:r ORDER BY unit"
                ),
                {"r": rv_id},
            )
        ).mappings().all()
    ]
    assert ri_after == ri_before

    # recipe re-opened: status draft, menu link cleared, draft copy points at the version
    assert await _count(
        conn, "SELECT count(*) FROM recipes WHERE menu_item_id=:m AND status='draft'",
        {"m": mid},
    ) == 1
    assert await _count(
        conn,
        "SELECT count(*) FROM menu_items WHERE id=:m AND recipe_version_id IS NULL",
        {"m": mid},
    ) == 1
    draft = (
        await conn.execute(
            text(
                "SELECT rd.parent_recipe_version_id AS pv, rd.draft_ingredients AS di"
                " FROM recipe_drafts rd JOIN recipes r ON r.id=rd.recipe_id"
                " WHERE r.menu_item_id=:m"
            ),
            {"m": mid},
        )
    ).mappings().one()
    assert draft["pv"] == rv_id
    names = {i["name"] for i in repo._as_list(draft["di"])}
    assert names == {"Flour", "Milk"}


@pytest.mark.integration
async def test_double_unconfirm_409(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    await _patch_draft(client, mid, _TWO)
    assert (await client.post(f"{R}/recipes/{mid}/confirm")).status_code == 200
    assert (await client.post(f"{R}/recipes/{mid}/unconfirm")).status_code == 200
    assert (await client.post(f"{R}/recipes/{mid}/unconfirm")).status_code == 409


# ── RBAC + cross-tenant ──────────────────────────────────────────────────────


@pytest.mark.integration
async def test_skip_on_confirmed_409(app_instance, conn, client) -> None:
    """Skip is for un-configured recipes; a confirmed one must be un-confirmed first,
    else 'skipped' would coexist with a live menu_items.recipe_version_id (read by
    Phase 9 base depletion)."""
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    await _patch_draft(client, mid, _TWO)
    assert (await client.post(f"{R}/recipes/{mid}/confirm")).status_code == 200
    assert (await client.post(f"{R}/recipes/{mid}/skip")).status_code == 409
    # link intact, still confirmed (skip rejected, not half-applied)
    assert await _count(
        conn,
        "SELECT count(*) FROM recipes r JOIN menu_items mi ON mi.id=r.menu_item_id"
        " WHERE r.menu_item_id=:m AND r.status='confirmed' AND mi.recipe_version_id IS NOT NULL",
        {"m": mid},
    ) == 1


@pytest.mark.integration
async def test_confirm_unconfirm_staff_403(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "staff")
    assert (await client.post(f"{R}/recipes/{mid}/confirm")).status_code == 403
    assert (await client.post(f"{R}/recipes/{mid}/unconfirm")).status_code == 403


@pytest.mark.integration
async def test_confirm_unconfirm_cross_tenant_404(app_instance, conn, client) -> None:
    tid_b, _ = await _seed_tenant_user(conn)
    mid_b = await _menu_item(conn, tid_b, "B-item")
    tid_a, uid_a = await _seed_tenant_user(conn)
    _as(app_instance, tid_a, uid_a, "manager")
    assert (await client.post(f"{R}/recipes/{mid_b}/confirm")).status_code == 404
    assert (await client.post(f"{R}/recipes/{mid_b}/unconfirm")).status_code == 404


# ── real-transaction atomicity (no partial confirm) ──────────────────────────


async def _seed_for_confirm(
    admin_conn: Any,
    ingredients: list[dict[str, Any]],
    extra_units: tuple[dict[str, str], ...] = (),
) -> tuple[str, str, str, str]:
    tid = str(uuid7())
    await admin_conn.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, 'T', $2)",
        tid, f"t-{uuid.uuid4().hex[:8]}",
    )
    uid = str(
        await admin_conn.fetchval(
            "INSERT INTO users (workos_id, email, email_verified)"
            " VALUES ($1, $2, true) RETURNING id",
            f"u-{uuid.uuid4().hex[:8]}", f"{uuid.uuid4().hex[:8]}@t.com",
        )
    )
    mid = str(
        await admin_conn.fetchval(
            "INSERT INTO menu_items (tenant_id, name, active)"
            " VALUES ($1, 'Latte', true) RETURNING id",
            tid,
        )
    )
    rid = str(
        await admin_conn.fetchval(
            "INSERT INTO recipes (tenant_id, menu_item_id, status)"
            " VALUES ($1, $2, 'draft') RETURNING id",
            tid, mid,
        )
    )
    await admin_conn.execute(
        "INSERT INTO recipe_drafts (tenant_id, recipe_id, draft_ingredients, created_by)"
        " VALUES ($1, $2, $3::jsonb, $4)",
        tid, rid, json.dumps(ingredients), uid,
    )
    for u in extra_units:
        await admin_conn.execute(
            "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type)"
            " VALUES ($1, $2, $2, $3)",
            tid, u["name"], u["unit_type"],
        )
    return tid, uid, mid, rid


async def _cleanup(admin_conn: Any, tid: str, uid: str) -> None:
    for tbl in ("recipe_ingredients", "recipe_versions", "recipe_drafts"):
        await admin_conn.execute(f"DELETE FROM {tbl} WHERE tenant_id=$1", tid)
    await admin_conn.execute(
        "UPDATE menu_items SET recipe_version_id=NULL WHERE tenant_id=$1", tid
    )
    for tbl in ("recipes", "inventory_items", "units_of_measure", "menu_items"):
        await admin_conn.execute(f"DELETE FROM {tbl} WHERE tenant_id=$1", tid)
    await admin_conn.execute("DELETE FROM users WHERE id=$1", uid)
    await admin_conn.execute("DELETE FROM tenants WHERE id=$1", tid)


async def _run_confirm_in_own_txn(
    tid: str, mid: str, *, inject_step4: bool = False
) -> BaseException | None:
    """Run confirm in a real engine transaction; roll back on any error (mirrors the
    production request lifecycle: no commit on error → connection close → rollback).
    Returns the exception raised, or None on success."""
    async with engine.connect() as c:
        trans = await c.begin()
        db = make_bound_session(c)
        if inject_step4:
            orig_execute = db.execute

            async def boom(statement: Any, *a: Any, **k: Any) -> Any:
                if "UPDATE menu_items" in str(statement):
                    raise RuntimeError("injected step-4 failure")
                return await orig_execute(statement, *a, **k)

            db.execute = boom  # type: ignore[method-assign]
        try:
            await repo.confirm_recipe(db, UUID(tid), UUID(mid))
            await trans.commit()
            return None
        except BaseException as exc:  # capture for assertion, then real rollback
            await trans.rollback()
            return exc
        finally:
            await db.close()


@pytest.mark.integration
async def test_no_partial_confirm_unit_type_conflict(admin_conn) -> None:
    """A real failure inside step 1 (second ingredient's unit exists with the wrong
    unit_type) must abort the whole confirm: the FIRST ingredient's auto-created unit
    and inventory_item — created moments earlier — must roll back too (no orphans)."""
    tid, uid, mid, rid = await _seed_for_confirm(
        admin_conn,
        [
            {"name": "Flour", "quantity": 100, "unit": "g"},   # clean → creates 'g' + Flour
            {"name": "Milk", "quantity": 200, "unit": "ml"},   # 'ml' pre-exists as weight
        ],
        extra_units=({"name": "ml", "unit_type": "weight"},),  # wrong: ml is volume
    )
    try:
        exc = await _run_confirm_in_own_txn(tid, mid)
        assert isinstance(exc, repo.UnitTypeConflict)

        # nothing from the confirm persisted — including step-1 side effects
        assert await admin_conn.fetchval(
            "SELECT count(*) FROM recipe_versions WHERE tenant_id=$1", tid
        ) == 0
        assert await admin_conn.fetchval(
            "SELECT count(*) FROM recipe_ingredients WHERE tenant_id=$1", tid
        ) == 0
        assert await admin_conn.fetchval(
            "SELECT count(*) FROM inventory_items WHERE tenant_id=$1", tid
        ) == 0  # Flour's auto-created item rolled back — no orphan
        assert await admin_conn.fetchval(
            "SELECT count(*) FROM units_of_measure WHERE tenant_id=$1 AND name='g'", tid
        ) == 0  # Flour's auto-created unit rolled back too
        # the draft is intact and the recipe is still a draft
        assert await admin_conn.fetchval(
            "SELECT count(*) FROM recipe_drafts WHERE recipe_id=$1", rid
        ) == 1
        assert await admin_conn.fetchval(
            "SELECT status FROM recipes WHERE id=$1", rid
        ) == "draft"
    finally:
        await _cleanup(admin_conn, tid, uid)


@pytest.mark.integration
async def test_no_partial_confirm_injected_late_failure(admin_conn) -> None:
    """A failure at step 4 (after the version + ingredients are written in steps 2–3)
    must roll the whole transaction back: no version, no ingredients, no items, draft
    intact, menu item unlinked."""
    tid, uid, mid, rid = await _seed_for_confirm(
        admin_conn, [{"name": "Flour", "quantity": 100, "unit": "g"}]
    )
    try:
        exc = await _run_confirm_in_own_txn(tid, mid, inject_step4=True)
        assert isinstance(exc, RuntimeError)

        assert await admin_conn.fetchval(
            "SELECT count(*) FROM recipe_versions WHERE tenant_id=$1", tid
        ) == 0  # the version created in step 2 rolled back
        assert await admin_conn.fetchval(
            "SELECT count(*) FROM recipe_ingredients WHERE tenant_id=$1", tid
        ) == 0
        assert await admin_conn.fetchval(
            "SELECT count(*) FROM inventory_items WHERE tenant_id=$1", tid
        ) == 0
        assert await admin_conn.fetchval(
            "SELECT count(*) FROM recipe_drafts WHERE recipe_id=$1", rid
        ) == 1
        assert await admin_conn.fetchval(
            "SELECT status FROM recipes WHERE id=$1", rid
        ) == "draft"
        assert await admin_conn.fetchval(
            "SELECT recipe_version_id FROM menu_items WHERE id=$1", mid
        ) is None
    finally:
        await _cleanup(admin_conn, tid, uid)
