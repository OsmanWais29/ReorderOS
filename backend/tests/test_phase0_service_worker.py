"""Phase 0 — Test 5: service_worker role attributes.

Requires a live database (integration test).
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_service_worker_role_exists(admin_conn):
    """service_worker role must exist in pg_roles.

    Why: Phase 1's migration GRANTs privileges to service_worker.
    PostgreSQL rejects GRANTs to nonexistent roles — the Phase 1
    migration would fail immediately with 'role does not exist'.
    This test catches a missing Phase 0 before Phase 1 crashes.
    """
    row = await admin_conn.fetchval(
        "SELECT 1 FROM pg_roles WHERE rolname = 'service_worker'"
    )
    assert row == 1, "service_worker role does not exist in pg_roles"


@pytest.mark.asyncio
async def test_service_worker_has_login(admin_conn):
    """service_worker must have LOGIN, not NOLOGIN.

    Why: The worker process connects via SERVICE_DATABASE_URL as
    service_worker. NOLOGIN causes every connection attempt to fail
    with 'role is not permitted to log in'. This is a silent killer:
    the migration succeeds, the grants succeed, the app starts — but
    the worker can never claim a single inbox event.

    Note: app_user is intentionally NOLOGIN (tests use SET ROLE).
    service_worker is different — it is the actual runtime identity
    for an OS-level background process.
    """
    can_login = await admin_conn.fetchval(
        "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'service_worker'"
    )
    assert can_login is True, (
        "service_worker has NOLOGIN — the worker process cannot connect "
        "via SERVICE_DATABASE_URL"
    )


@pytest.mark.asyncio
async def test_service_worker_has_schema_usage(admin_conn):
    """service_worker must have USAGE on the public schema.

    Why: Without USAGE on public, every query from service_worker fails
    with 'permission denied for schema public' — even if table-level
    grants are correct. Schema USAGE is the gating privilege.
    """
    has_usage = await admin_conn.fetchval(
        "SELECT has_schema_privilege('service_worker', 'public', 'USAGE')"
    )
    assert has_usage is True, "service_worker lacks USAGE on public schema"


@pytest.mark.asyncio
async def test_service_worker_has_connect_on_database(admin_conn):
    """service_worker must have CONNECT on the reorderos database.

    Why: CONNECT is the first privilege gate. Without it, service_worker
    is rejected before it even reaches schema-level or table-level checks.
    GRANT CONNECT is explicit in the Phase 0 migration and must be tested
    here rather than discovered at worker startup time.
    """
    has_connect = await admin_conn.fetchval(
        "SELECT has_database_privilege('service_worker', 'reorderos', 'CONNECT')"
    )
    assert has_connect is True, "service_worker lacks CONNECT on database reorderos"
