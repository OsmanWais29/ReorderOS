"""Pool-leak sentinel — request-scoped sessions must be CLOSED, not GC'd.

WHY THIS EXISTS: the 2026-07-14 staging smoke test took the whole API down with
nothing but the review screen's 2.5s poll. get_db_session returned a session
and left close() to the garbage collector; checkouts outran GC, the QueuePool
(size 5 + overflow 5) drained, and every endpoint — including auth — 500'd with
"QueuePool limit reached". The bound-session harness can never catch this
(it bypasses the pool), so this test drives the app with REAL pooled sessions,
with gc disabled so only deterministic close() can return connections.
"""

from __future__ import annotations

import gc
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.database import get_engine
from app.core.security import Principal, get_principal
from app.main import create_app

pytestmark = pytest.mark.integration

# Comfortably more than pool_size 5 + max_overflow 5: with the old GC-reliant
# dependency (and gc disabled) request 11 hangs on an empty pool.
_REQUESTS = 25


async def test_repeated_tenant_scoped_requests_do_not_leak_pool_connections() -> None:
    app = create_app()
    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=str(uuid.uuid4()),
        workos_id="w_pool_leak",
        email="pool-leak@test.com",
        tenant_id=str(uuid.uuid4()),
        role="manager",
    )
    pool = get_engine().sync_engine.pool
    baseline = pool.checkedout()
    gc.disable()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for _ in range(_REQUESTS):
                resp = await client.get("/api/v1/receipts", params={"commit_state": "draft"})
                assert resp.status_code == 200
        assert pool.checkedout() == baseline
    finally:
        gc.enable()
        app.dependency_overrides.clear()
