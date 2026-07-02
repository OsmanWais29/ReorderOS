"""Sprint 5 Phase 2 — Clover catalog sync.

Covers the CloverClient catalog methods, CatalogSyncService (populate, re-sync
preserving operator-owned fields, complete-pull-before-soft-delete with loud
abort on partial failure, dedup, draft-modifier inertness), and the /sync-menu
endpoint (Manager+, 404, 403).
"""

from __future__ import annotations

import uuid

import asyncpg
import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from uuid6 import uuid7

from app.main import app
from app.modules.pos.catalog_sync import CatalogSyncService
from app.modules.pos.clover_client import CloverClient
from app.modules.pos.router import get_current_user
from tests.helpers.pos_connection import insert_connection, make_connection_row, seed_tenant
from tests.helpers.sprint5 import seed_recipe_version

# Clover response fixtures — shaped per Clover v3 docs: items carry their modifier
# GROUP associations (expand=modifierGroups); modifier DETAIL comes from the
# modifier_groups endpoint (expand=modifiers). (Verify-don't-assume: tests use
# this documented shape, and the sync derives modifier names from the groups call,
# not from collection-level item expansion.)
ITEMS = [
    {"id": "CLITEM1", "name": "Latte", "modifierGroups": {"elements": [{"id": "GRP1"}]}},
    {"id": "CLITEM2", "name": "Muffin", "modifierGroups": {"elements": []}},
]
GROUPS = [
    {
        "id": "GRP1",
        "name": "Add-ons",
        "modifiers": {
            "elements": [{"id": "MOD1", "name": "Extra shot"}, {"id": "MOD2", "name": "Oat milk"}]
        },
    },
]


def _items_route(elements: list[dict]) -> None:
    respx.get(url__regex=r".*/items.*").mock(
        return_value=httpx.Response(200, json={"elements": elements})
    )


def _groups_route(elements: list[dict]) -> None:
    respx.get(url__regex=r".*/modifier_groups.*").mock(
        return_value=httpx.Response(200, json={"elements": elements})
    )


async def _seed_conn(admin_conn: object) -> tuple[str, str]:
    tid = str(uuid7())
    await seed_tenant(admin_conn, tid, "Catalog T", f"cat-{uuid.uuid4().hex[:8]}")
    row = make_connection_row({"tenant_id": tid})
    await insert_connection(admin_conn, row)
    return tid, str(row["connection_id"])


# ── CloverClient catalog methods ─────────────────────────────────────────────


@pytest.mark.integration
@respx.mock
async def test_list_inventory_items() -> None:
    _items_route(ITEMS)
    c = CloverClient(access_token="t", merchant_id="M", environment="sandbox")
    res = await c.list_inventory_items()
    assert [i["id"] for i in res] == ["CLITEM1", "CLITEM2"]


@pytest.mark.integration
@respx.mock
async def test_list_modifier_groups() -> None:
    _groups_route(GROUPS)
    c = CloverClient(access_token="t", merchant_id="M", environment="sandbox")
    res = await c.list_modifier_groups()
    assert res[0]["modifiers"]["elements"][0]["id"] == "MOD1"


# ── CatalogSyncService ───────────────────────────────────────────────────────


@pytest.mark.integration
@respx.mock
async def test_sync_populates_menu_items_and_modifiers(admin_conn) -> None:
    tid, cid = await _seed_conn(admin_conn)
    _items_route(ITEMS)
    _groups_route(GROUPS)

    await CatalogSyncService().sync_connection(cid)

    items = await admin_conn.fetch(
        "SELECT pos_item_id, name, active FROM menu_items WHERE tenant_id=$1", tid
    )
    assert {r["pos_item_id"] for r in items} == {"CLITEM1", "CLITEM2"}
    assert all(r["active"] for r in items)

    mods = await admin_conn.fetch(
        "SELECT pos_modifier_id, status, name FROM modifiers WHERE tenant_id=$1", tid
    )
    assert {r["pos_modifier_id"] for r in mods} == {"MOD1", "MOD2"}
    assert all(r["status"] == "draft" for r in mods)


@pytest.mark.integration
@respx.mock
async def test_resync_preserves_recipe_version_id(admin_conn) -> None:
    tid, cid = await _seed_conn(admin_conn)
    _items_route(ITEMS)
    _groups_route(GROUPS)
    await CatalogSyncService().sync_connection(cid)

    # Operator confirms a recipe -> menu_items.recipe_version_id is set.
    seeded = await seed_recipe_version(admin_conn, tid)
    await admin_conn.execute(
        "UPDATE menu_items SET recipe_version_id=$1 WHERE tenant_id=$2 AND pos_item_id='CLITEM1'",
        uuid.UUID(seeded.recipe_version_id),
        tid,
    )

    # Re-sync (same catalog) must update name/active but NEVER touch recipe_version_id.
    _items_route(
        [{"id": "CLITEM1", "name": "Latte (renamed)", "modifierGroups": {"elements": []}}, ITEMS[1]]
    )
    await CatalogSyncService().sync_connection(cid)

    row = await admin_conn.fetchrow(
        "SELECT name, recipe_version_id FROM menu_items WHERE tenant_id=$1 AND pos_item_id='CLITEM1'",
        tid,
    )
    assert row["name"] == "Latte (renamed)"
    assert str(row["recipe_version_id"]) == seeded.recipe_version_id  # preserved


@pytest.mark.integration
@respx.mock
async def test_removal_soft_deletes_after_complete_pull(admin_conn) -> None:
    tid, cid = await _seed_conn(admin_conn)
    _items_route(ITEMS)
    _groups_route(GROUPS)
    await CatalogSyncService().sync_connection(cid)

    # Re-sync with CLITEM2 removed from Clover -> it soft-deletes (active=false).
    _items_route([ITEMS[0]])
    await CatalogSyncService().sync_connection(cid)

    rows = {
        r["pos_item_id"]: r["active"]
        for r in await admin_conn.fetch(
            "SELECT pos_item_id, active FROM menu_items WHERE tenant_id=$1", tid
        )
    }
    assert rows["CLITEM1"] is True
    assert rows["CLITEM2"] is False


@pytest.mark.integration
@respx.mock
async def test_partial_pull_does_not_soft_delete(admin_conn) -> None:
    tid, cid = await _seed_conn(admin_conn)
    _items_route(ITEMS)
    _groups_route(GROUPS)
    await CatalogSyncService().sync_connection(cid)

    # A transient API failure on the items pull must NOT be read as "all absent".
    _items_route([])  # base route; override with a 500
    respx.get(url__regex=r".*/items.*").mock(return_value=httpx.Response(500))
    await CatalogSyncService().sync_connection(cid)  # aborts loudly, no writes

    rows = await admin_conn.fetch("SELECT active FROM menu_items WHERE tenant_id=$1", tid)
    assert len(rows) == 2
    assert all(r["active"] for r in rows)  # nothing deactivated by the failed pull


async def test_fetch_all_raises_on_max_pages() -> None:
    """Hitting MAX_PAGES is an INCOMPLETE pull, not a finished one. _fetch_all must
    raise (not return a truncated list) so sync_connection's loud-abort path makes
    no writes — otherwise every item past the cap would be soft-deleted."""
    svc = CatalogSyncService()
    calls = 0

    async def always_full(offset: int, limit: int) -> list[dict]:
        nonlocal calls
        calls += 1
        return [{"id": f"X{offset + i}"} for i in range(limit)]  # always a full page

    with pytest.raises(RuntimeError, match="MAX_PAGES"):
        await svc._fetch_all(always_full)
    assert calls == svc.MAX_PAGES  # stopped at the cap, did not loop forever


@pytest.mark.integration
@respx.mock
async def test_resync_dedups_modifiers(admin_conn) -> None:
    tid, cid = await _seed_conn(admin_conn)
    _items_route(ITEMS)
    _groups_route(GROUPS)
    await CatalogSyncService().sync_connection(cid)
    await CatalogSyncService().sync_connection(cid)  # second sync — no duplicates

    n = await admin_conn.fetchval("SELECT count(*) FROM modifiers WHERE tenant_id=$1", tid)
    assert n == 2


@pytest.mark.integration
@respx.mock
async def test_synced_modifiers_are_inert_for_depletion(admin_conn) -> None:
    # Draft modifiers have no confirmed version, so the depletion engine (which
    # reads modifier_versions / sale_line_item_modifiers) has nothing to walk.
    tid, cid = await _seed_conn(admin_conn)
    _items_route(ITEMS)
    _groups_route(GROUPS)
    await CatalogSyncService().sync_connection(cid)

    rows = await admin_conn.fetch(
        "SELECT status, current_version_id FROM modifiers WHERE tenant_id=$1", tid
    )
    assert all(r["status"] == "draft" and r["current_version_id"] is None for r in rows)
    assert (
        await admin_conn.fetchval("SELECT count(*) FROM modifier_versions WHERE tenant_id=$1", tid)
        == 0
    )


# ── /sync-menu endpoint ──────────────────────────────────────────────────────


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _override_role(role: str, tenant_id: str) -> None:
    async def _user() -> dict:
        return {"role": role, "tenant_id": tenant_id, "user_id": str(uuid7())}

    app.dependency_overrides[get_current_user] = _user


@pytest.mark.integration
async def test_sync_menu_manager_202(client, admin_conn, monkeypatch) -> None:
    tid, _cid = await _seed_conn(admin_conn)

    async def _noop(self, connection_id):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(CatalogSyncService, "sync_connection", _noop)
    _override_role("manager", tid)

    resp = client.post("/api/v1/pos/clover/sync-menu")
    assert resp.status_code == 202
    assert resp.json()["status"] == "sync_started"


@pytest.mark.integration
async def test_sync_menu_no_connection_404(client, admin_conn, monkeypatch) -> None:
    tid = str(uuid7())
    await seed_tenant(admin_conn, tid, "No Conn", f"noconn-{uuid.uuid4().hex[:8]}")

    async def _noop(self, connection_id):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(CatalogSyncService, "sync_connection", _noop)
    _override_role("manager", tid)

    resp = client.post("/api/v1/pos/clover/sync-menu")
    assert resp.status_code == 404


@pytest.mark.integration
async def test_sync_menu_staff_forbidden(client) -> None:
    _override_role("staff", str(uuid7()))
    resp = client.post("/api/v1/pos/clover/sync-menu")
    assert resp.status_code == 403


# ── 0018 uniqueness ──────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_menu_items_pos_item_unique(admin_conn) -> None:
    """The 0018 partial unique blocks duplicate (tenant_id, pos_item_id) — the
    race that concurrent syncs could otherwise produce."""
    tid = str(uuid7())
    await seed_tenant(admin_conn, tid, "Uniq", f"uniq-{uuid.uuid4().hex[:8]}")
    await admin_conn.execute(
        "INSERT INTO menu_items (tenant_id, pos_item_id, name, active) VALUES ($1,'DUP','A',true)",
        tid,
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await admin_conn.execute(
            "INSERT INTO menu_items (tenant_id, pos_item_id, name, active)"
            " VALUES ($1,'DUP','B',true)",
            tid,
        )
