"""Durability sentinel — writes must survive the request (COMMITTED, not flushed).

WHY THIS EXISTS: the smoke test found POST /receipts/uploads returning 201 while
the draft row silently vanished — get_db_session does NOT auto-commit ("route
handlers call await db.commit() explicitly") and most receipts endpoints never
did. The whole HTTP test suite missed it because the bound-session harness
(make_bound_session) reads its own uncommitted state: a handler that forgets to
commit still looks green there.

This test runs the app with the REAL pooled session dependency (no bound-session
override) and asserts visibility from a SECOND, independent connection — the
only thing that proves a commit happened. It seeds and cleans its own rows with
explicit commits.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core import storage
from app.core.database import get_sessionmaker
from app.core.security import Principal, get_principal
from app.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
async def real_app() -> AsyncIterator[tuple[Any, dict[str, Any]]]:
    """App with REAL pooled sessions + a committed tenant/user; rows cleaned up
    afterwards via tenant CASCADE."""
    app = create_app()
    sm = get_sessionmaker()
    tid, uid = uuid.uuid4(), uuid.uuid4()
    async with sm() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'Durability T', :slug)"),
            {"id": tid, "slug": f"dur-{tid.hex[:10]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, workos_id, email) VALUES (:id, :w, :e)"),
            {"id": uid, "w": f"w_dur_{uid.hex[:8]}", "e": f"dur-{uid.hex[:8]}@test.com"},
        )
        await s.commit()

    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=str(uid),
        workos_id=f"w_dur_{uid.hex[:8]}",
        email="dur@test.com",
        tenant_id=str(tid),
        role="staff",
    )
    try:
        yield app, {"tenant_id": tid, "user_id": uid}
    finally:
        app.dependency_overrides.clear()
        async with sm() as s:
            # receipts/lines/jobs cascade from the tenant; users row separately.
            await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
            await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
            await s.commit()


async def _visible_in_fresh_session(query: str, params: dict[str, Any]) -> Any:
    """The commit proof: a brand-new session (separate transaction) must see it."""
    sm = get_sessionmaker()
    async with sm() as s:
        return (await s.execute(text(query), params)).scalar()


async def test_upload_and_line_add_survive_the_request(
    real_app: tuple[Any, dict[str, Any]], monkeypatch: Any
) -> None:
    app, seed = real_app
    monkeypatch.setattr(storage, "put_bytes", lambda *a, **k: None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Upload → the draft must be COMMITTED, not just flushed.
        from io import BytesIO

        from PIL import Image

        buf = BytesIO()
        Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, format="JPEG")
        r = await client.post(
            "/api/v1/receipts/uploads",
            files={"file": ("r.jpg", buf.getvalue(), "image/jpeg")},
        )
        assert r.status_code == 201, r.text
        receipt_id = r.json()["receipt_id"]

        found = await _visible_in_fresh_session(
            "SELECT count(*) FROM receipts WHERE id = :r AND tenant_id = :t",
            {"r": receipt_id, "t": seed["tenant_id"]},
        )
        assert found == 1, "201 returned but the draft row was not committed"

        # 2. Add a line → committed too (PR #4 endpoint family).
        r2 = await client.post(
            f"/api/v1/receipts/{receipt_id}/lines",
            json={"extracted_name": "Durability", "received_quantity": 1, "extracted_unit": "g"},
        )
        assert r2.status_code == 201, r2.text
        n_lines = await _visible_in_fresh_session(
            "SELECT count(*) FROM receipt_lines WHERE receipt_id = :r", {"r": receipt_id}
        )
        assert n_lines == 1, "201 returned but the line was not committed"

        # 3. Dismiss → terminal state committed.
        r3 = await client.post(
            f"/api/v1/receipts/{receipt_id}/dismiss", json={"reason": "durability check"}
        )
        assert r3.status_code == 200, r3.text
        state = await _visible_in_fresh_session(
            "SELECT commit_state FROM receipts WHERE id = :r", {"r": receipt_id}
        )
        assert state == "dismissed", "200 returned but the dismissal was not committed"
