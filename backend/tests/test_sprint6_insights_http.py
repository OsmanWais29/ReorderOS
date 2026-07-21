"""Stock Item Insights — HTTP-layer tests (error codes, RBAC, snapshot happy path).

Exercises GET /api/v1/inventory/items/{id}/insights through the ASGI app with the
principal + bound-session overrides, covering the stable error contract and the
server-side cost redaction for staff.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import engine, get_db_session, make_bound_session
from app.core.security import Principal, get_principal
from app.main import create_app

pytestmark = pytest.mark.integration


async def _seed_min_item(conn: AsyncConnection, tid: uuid.UUID) -> uuid.UUID:
    await conn.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id,:s,'INSH')"),
        {"id": tid, "s": f"insh-{tid.hex[:8]}"},
    )
    unit = (
        await conn.execute(
            text(
                "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
                "VALUES (:t,'ea','ea','count') RETURNING id"
            ),
            {"t": tid},
        )
    ).scalar_one()
    item = (
        await conn.execute(
            text(
                "INSERT INTO inventory_items (tenant_id, name, inventory_mode, storage_unit_id, "
                "recipe_unit_id, par_level) VALUES (:t,'Item','recipe_deducted',:u,:u,10) RETURNING id"
            ),
            {"t": tid, "u": unit},
        )
    ).scalar_one()
    await conn.execute(
        text(
            "INSERT INTO ingredient_cost_snapshots (tenant_id, inventory_item_id, unit_cost_cents, "
            "unit_cost_cents_exact) VALUES (:t,:i,120,120.0000)"
        ),
        {"t": tid, "i": item},
    )
    return item


class _Ctx:
    def __init__(self, conn: AsyncConnection, tid: uuid.UUID, item: uuid.UUID, client: AsyncClient):
        self.conn, self.tid, self.item, self.client = conn, tid, item, client


async def _ctx(role: str) -> AsyncIterator[_Ctx]:
    app = create_app()
    tid, uid = uuid.uuid4(), uuid.uuid4()
    conn: AsyncConnection
    async with engine.connect() as conn:
        await conn.begin()
        bound = make_bound_session(conn)
        app.dependency_overrides[get_db_session] = lambda: bound
        app.dependency_overrides[get_principal] = lambda: Principal(
            user_id=str(uid),
            workos_id=f"w_{uid.hex[:8]}",
            email="x@test.com",
            tenant_id=str(tid),
            role=role,  # type: ignore[arg-type]
        )
        try:
            item = await _seed_min_item(conn, tid)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                yield _Ctx(conn, tid, item, c)
        finally:
            app.dependency_overrides.clear()
            await conn.rollback()


_BASE = "/api/v1/inventory/items"


async def test_window_invalid_422() -> None:
    async for ctx in _ctx("manager"):
        r = await ctx.client.get(f"{_BASE}/{ctx.item}/insights?window=99d")
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "INSIGHTS_WINDOW_INVALID"


async def test_target_cover_invalid_422() -> None:
    async for ctx in _ctx("manager"):
        r = await ctx.client.get(f"{_BASE}/{ctx.item}/insights?target_cover_days=999")
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "TARGET_COVER_INVALID"


async def test_unknown_item_404() -> None:
    async for ctx in _ctx("manager"):
        r = await ctx.client.get(f"{_BASE}/{uuid.uuid4()}/insights")
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "ITEM_NOT_FOUND"


async def test_manager_sees_cost_200() -> None:
    async for ctx in _ctx("manager"):
        r = await ctx.client.get(f"{_BASE}/{ctx.item}/insights?window=14d")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["cost"]["available"] is True
        assert body["cost"]["latest_unit_cost_cents_exact"] == "120.0000"
        assert body["forecast"]["state"] == "NOT_YET_CERTIFIED"
        assert body["snapshot"]["isolation"] == "repeatable_read"


async def test_staff_sees_latest_cost_aggregated_redacted_200() -> None:
    async for ctx in _ctx("staff"):
        r = await ctx.client.get(f"{_BASE}/{ctx.item}/insights?window=14d")
        assert r.status_code == 200, r.text
        cost = r.json()["cost"]
        # Staff see the latest unit cost, not supplier/history.
        assert cost["available"] is True
        assert cost["latest_unit_cost_cents_exact"] == "120.0000"
        assert cost["aggregated"]["available"] is False
        assert cost["aggregated"]["reason"]["code"] == "MANAGER_ONLY"


async def test_no_banned_internal_language_in_response() -> None:
    """Customer-facing safety: no internal jargon or raw ids in the response
    body. Stable reason CODES are allowed; internal terms are not."""
    # Exactly the internal terms the customer-facing contract bans (plus stack
    # traces / raw payloads). NB "reconciliation" alone is allowed — the truthful
    # label is "latest background reconciliation check".
    banned = [
        "inbox",
        "dead_letter",
        "dead letter",
        "lease",
        "reconciliation_cursor",
        "reconciliation cursor",
        "conversion_path",
        "conversion path",
        "raw_payload",
        "traceback",
    ]
    async for ctx in _ctx("manager"):
        r = await ctx.client.get(f"{_BASE}/{ctx.item}/insights?window=14d")
        assert r.status_code == 200
        body_l = r.text.lower()
        for term in banned:
            assert term not in body_l, f"banned internal term leaked: {term!r}"
        # The tenant's own id must never appear in the body.
        assert str(ctx.tid) not in r.text


async def test_scenario_target_cover_days_echoed() -> None:
    async for ctx in _ctx("manager"):
        r = await ctx.client.get(f"{_BASE}/{ctx.item}/insights?target_cover_days=10")
        assert r.status_code == 200
        ro = r.json()["reorder"]
        assert ro["target_cover_days"] == 10
        assert ro["target_source"] == "scenario"


async def test_openapi_documents_full_insights_contract() -> None:
    # Finding 2: the OpenAPI schema must describe the typed insights payload with
    # its status/scope/blocker enums — not merely prove the URL exists.
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        spec = (await c.get("/api/v1/openapi.json")).json()

    schemas = spec["components"]["schemas"]
    assert "InsightsResponse" in schemas
    # The endpoint's 200 response references the model.
    path = spec["paths"]["/api/v1/inventory/items/{item_id}/insights"]["get"]
    ref = path["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/InsightsResponse")

    # Enum surfaces are documented (spot-check the load-bearing ones). Pydantic
    # inlines Literals as nested "enum" arrays or single-value "const", so walk
    # the whole spec.
    all_enum_values: set[str] = set()

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("enum"), list):
                all_enum_values.update(str(x) for x in node["enum"])
            if "const" in node:
                all_enum_values.add(str(node["const"]))
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(spec)
    # Stage, blocker, e2e, confidence, ledger enums present somewhere in the spec.
    for v in (
        "in_progress",
        "failures",
        "unknown",  # stage statuses
        "END_TO_END_COVERAGE_INCOMPLETE",
        "COMPLETENESS_UNPROVEN",  # blockers
        "DATA_INCONSISTENT",
        "RECONCILIATION_UNAVAILABLE",  # ledger states
        "tenant",
        "tenant_proxy",
        "NOT_YET_CERTIFIED",
    ):
        assert v in all_enum_values, f"enum value {v!r} missing from OpenAPI"

    # The response model names the major sections.
    props = set(schemas["InsightsResponse"]["properties"].keys())
    assert {
        "snapshot",
        "item",
        "ledger",
        "pos",
        "consumption",
        "forecast",
        "reorder",
        "cost",
    } <= props
