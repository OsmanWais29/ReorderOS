"""Sprint 5 Phase 16 — read-only GET modifier detail (the consumer-less-API gap fill).

The modifier config UI (slice 3) needs to READ a modifier's draft ingredients to display/edit
them and to re-hydrate after app-close (FE-5). The only modifier route that returned
ingredients was PATCH (a write) — using it as a read would implicitly unskip a skipped
modifier and 409 a confirmed one. This adds the symmetric read endpoint
(GET /onboarding/recipes/{menu_item_id}/modifiers/{modifier_id}), the partner of the
recipe-detail GET.

Covers: draft read; PURE-READ no-side-effect on a skipped modifier (the implicit-unskip
hazard); confirmed read (draft empty, mirrors the recipe analogue); parent-scoping 404 (the
composite-FK boundary applies to GETs); cross-tenant 404.
"""

from __future__ import annotations

import json
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

R = "/api/v1/onboarding/recipes"


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


def _as(app_instance: Any, tenant_id: str, user_id: str, role: str = "staff") -> None:
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
    return str(
        (
            await conn.execute(
                text(
                    "INSERT INTO menu_items (tenant_id, name, active)"
                    " VALUES (:t, :n, true) RETURNING id"
                ),
                {"t": tid, "n": name},
            )
        ).scalar_one()
    )


async def _modifier(
    conn: AsyncConnection, tid: str, mid: str, *, status: str = "draft", mtype: str = "additive"
) -> str:
    return str(
        (
            await conn.execute(
                text(
                    "INSERT INTO modifiers (tenant_id, menu_item_id, name, modifier_type, status)"
                    " VALUES (:t, :m, 'Extra shot', :ty, :st) RETURNING id"
                ),
                {"t": tid, "m": mid, "ty": mtype, "st": status},
            )
        ).scalar_one()
    )


async def _draft(conn: AsyncConnection, tid: str, modid: str, uid: str, ings: list[dict]) -> None:
    await conn.execute(
        text(
            "INSERT INTO modifier_drafts (tenant_id, modifier_id, draft_ingredients, created_by)"
            " VALUES (:t, :m, CAST(:di AS jsonb), :u)"
        ),
        {"t": tid, "m": modid, "di": json.dumps(ings), "u": uid},
    )


def _url(mid: str, modid: str) -> str:
    return f"{R}/{mid}/modifiers/{modid}"


# ── draft read ────────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_get_modifier_returns_draft_ingredients(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid)
    await _draft(conn, tid, modid, uid, [{"name": "Espresso", "quantity": 7, "unit": "g"}])
    _as(app_instance, tid, uid, "staff")

    resp = await client.get(_url(mid, modid))
    assert resp.status_code == 200
    body = resp.json()
    assert body["modifier_id"] == modid
    assert body["status"] == "draft"
    assert body["ingredients"] == [{"name": "Espresso", "quantity": 7, "unit": "g"}]


# ── PURE READ: GET on a skipped modifier must not change its status ─────────────


@pytest.mark.integration
async def test_get_modifier_on_skipped_has_no_side_effect(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid, status="skipped")
    _as(app_instance, tid, uid, "staff")

    resp = await client.get(_url(mid, modid))
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"
    # the read must NOT implicitly unskip (the hazard that disqualified PATCH-as-read)
    after = (
        await conn.execute(text("SELECT status FROM modifiers WHERE id = :m"), {"m": modid})
    ).scalar_one()
    assert after == "skipped"


# ── confirmed read mirrors the recipe analogue (draft empty once confirmed) ─────


@pytest.mark.integration
async def test_get_modifier_confirmed_has_empty_draft(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid, status="confirmed")
    mvid = (
        await conn.execute(
            text(
                "INSERT INTO modifier_versions (tenant_id, modifier_id, version_number,"
                " yield_quantity) VALUES (:t, :m, 1, 1.0) RETURNING id"
            ),
            {"t": tid, "m": modid},
        )
    ).scalar_one()
    await conn.execute(
        text("UPDATE modifiers SET current_version_id = :v WHERE id = :m"),
        {"v": mvid, "m": modid},
    )
    _as(app_instance, tid, uid, "staff")

    resp = await client.get(_url(mid, modid))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["ingredients"] == []  # draft empty post-confirm; operator un-confirms to edit


# ── parent-scoping: a modifier of a DIFFERENT menu item → 404 ───────────────────


@pytest.mark.integration
async def test_get_modifier_wrong_parent_is_404(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid_a = await _menu_item(conn, tid, "Latte")
    mid_b = await _menu_item(conn, tid, "Mocha")
    modid = await _modifier(conn, tid, mid_a)  # belongs to A
    _as(app_instance, tid, uid, "staff")

    resp = await client.get(_url(mid_b, modid))  # ask via B's path
    assert resp.status_code == 404


# ── cross-tenant → 404 ──────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_get_modifier_cross_tenant_is_404(app_instance, conn, client) -> None:
    tid_a, _ = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid_a)
    modid = await _modifier(conn, tid_a, mid)
    tid_b, uid_b = await _seed_tenant_user(conn)
    _as(app_instance, tid_b, uid_b, "staff")  # different tenant

    resp = await client.get(_url(mid, modid))
    assert resp.status_code == 404
