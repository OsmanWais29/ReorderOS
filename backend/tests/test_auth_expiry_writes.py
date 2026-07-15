"""Expired-token writes — a 401 must never look like a save.

The Lauzon smoke lost a line link to a silent expiry mid-review. These tests
drive the REAL auth dependency chain (a verifier that rejects, exactly like an
expired WorkOS JWT) through the write endpoints the review flow uses — line
edit, conversion confirm, commit — and prove: 401 out, ZERO state change.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import engine, get_db_session, make_bound_session
from app.core.security import get_jwt_verifier
from app.main import create_app

pytestmark = pytest.mark.integration


class _ExpiredVerifier:
    async def verify(self, token: str) -> dict[str, Any]:
        raise HTTPException(status_code=401, detail="Token expired")


@pytest.fixture(scope="module")
def app_instance() -> Any:
    app = create_app()
    app.dependency_overrides[get_jwt_verifier] = lambda: _ExpiredVerifier()
    return app


@pytest.fixture
async def conn(app_instance: Any) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as connection:
        await connection.begin()
        bound = make_bound_session(connection)
        app_instance.dependency_overrides[get_db_session] = lambda: bound
        try:
            yield connection
        finally:
            app_instance.dependency_overrides.pop(get_db_session, None)
            await connection.rollback()


@pytest.fixture
async def client(app_instance: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app_instance), base_url="http://test") as c:
        yield c


async def _seed(conn: AsyncConnection) -> dict[str, Any]:
    tid, rid, lid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'X', :slug)"),
        {"id": tid, "slug": f"x-{tid.hex[:8]}"},
    )
    await conn.execute(
        text(
            "INSERT INTO receipts (id, tenant_id, commit_state, source, extraction_status) "
            "VALUES (:id, :t, 'draft', 'mobile_photo', 'complete')"
        ),
        {"id": rid, "t": tid},
    )
    await conn.execute(
        text("""
            INSERT INTO receipt_lines
                (id, tenant_id, receipt_id, extracted_name, received_quantity,
                 extracted_unit, match_status, line_ordinal)
            VALUES (:id, :t, :r, 'LAIT 3.25% 4x4L', 3, 'CS', 'unmatched', 0)
        """),
        {"id": lid, "t": tid, "r": rid},
    )
    return {"tenant_id": tid, "receipt_id": rid, "line_id": lid}


async def _line_state(conn: AsyncConnection, lid: uuid.UUID) -> tuple[Any, ...]:
    row = (
        await conn.execute(
            text(
                "SELECT received_quantity, received_unit, match_status, manually_corrected "
                "FROM receipt_lines WHERE id = :id"
            ),
            {"id": lid},
        )
    ).fetchone()
    assert row is not None
    return tuple(row)


@pytest.mark.parametrize(
    "payload",
    [
        {"received_quantity": 5},  # plain edit
        {"received_quantity": 48, "received_unit": "L", "conversion_factor": 16},  # confirm
    ],
    ids=["line-edit", "conversion-confirm"],
)
async def test_expired_token_line_writes_401_and_change_nothing(
    conn: AsyncConnection, client: AsyncClient, payload: dict[str, Any]
) -> None:
    s = await _seed(conn)
    before = await _line_state(conn, s["line_id"])
    r = await client.put(
        f"/api/v1/receipts/{s['receipt_id']}/lines/{s['line_id']}",
        json=payload,
        headers={"Authorization": "Bearer expired.jwt.here", "X-Tenant-Id": str(s["tenant_id"])},
    )
    assert r.status_code == 401
    assert await _line_state(conn, s["line_id"]) == before  # nothing saved


async def test_expired_token_commit_401_and_no_movements(
    conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    r = await client.post(
        f"/api/v1/receipts/{s['receipt_id']}/commit",
        json={"confirm": True, "reviewed_affirmation": True},
        headers={"Authorization": "Bearer expired.jwt.here", "X-Tenant-Id": str(s["tenant_id"])},
    )
    assert r.status_code == 401
    state = (
        await conn.execute(
            text("SELECT commit_state FROM receipts WHERE id = :r"), {"r": s["receipt_id"]}
        )
    ).scalar_one()
    assert state == "draft"
    n = (
        await conn.execute(
            text("SELECT count(*) FROM inventory_movements WHERE tenant_id = :t"),
            {"t": s["tenant_id"]},
        )
    ).scalar_one()
    assert n == 0
