"""Phase 0 — Test 6: App engine and service engine are isolated.

Requires SERVICE_DATABASE_URL to be set and service_worker to have LOGIN.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_engines_are_different_objects():
    """App and service engines must be different Python objects.

    Why: If both variables point to the same engine, they share one
    connection pool. A FastAPI request sets SET LOCAL app.tenant_id =
    'tenant-A' on a pooled connection, then returns it. The worker
    picks up that same connection — it now has tenant-A's context and
    its next INSERT goes into tenant-A's scope even though it's
    processing tenant-B's event. Separate objects = separate pools =
    no RLS context leakage.
    """
    from app.core.database import get_engine
    from app.core.service_db import get_service_engine

    app_engine = get_engine()
    svc_engine = get_service_engine()

    assert app_engine is not svc_engine, (
        "App and service engines are the same object — "
        "RLS context will leak between tenant requests and worker events"
    )


@pytest.mark.asyncio
async def test_engines_connect_as_different_roles():
    """Each engine must connect as a distinct database role.

    Why: Two different URL strings can still produce engines that
    connect as the same role (e.g., if SERVICE_DATABASE_URL was
    accidentally set to the same value as DATABASE_URL). This test
    verifies the runtime identity, not just the config strings.
    The app engine connects as 'reorderos' (superuser for local dev).
    The service engine connects as 'service_worker'.
    """
    from app.core.database import get_engine
    from app.core.service_db import get_service_engine

    app_engine = get_engine()
    svc_engine = get_service_engine()

    async with app_engine.connect() as app_conn:
        app_user = (await app_conn.execute(text("SELECT current_user"))).scalar()

    async with svc_engine.connect() as svc_conn:
        svc_user = (await svc_conn.execute(text("SELECT current_user"))).scalar()

    assert app_user != svc_user, (
        f"Both engines connected as the same user '{app_user}' — service_worker isolation is broken"
    )
    assert svc_user == "service_worker", (
        f"Service engine connected as '{svc_user}' instead of 'service_worker'"
    )
