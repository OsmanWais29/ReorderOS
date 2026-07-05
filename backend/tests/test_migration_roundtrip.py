"""MIG-1 — migration round-trip (fail-9 rollback leg).

The full chain must downgrade head→base and upgrade base→head cleanly — proving a bad deploy
can be rolled back. This is DESTRUCTIVE (downgrades the target DB to base, dropping all data and
roles), so it is skipped by default and runs only as a dedicated CI step (RUN_MIGRATION_TESTS=1)
against a DISPOSABLE database — never the shared dev/test DB during a normal run.
"""

from __future__ import annotations

import os
import subprocess

import asyncpg
import pytest

from tests.conftest import DB_URL_SYNC

_BACKEND = os.path.dirname(os.path.dirname(__file__))

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MIGRATION_TESTS") != "1",
    reason="destructive (downgrades DB to base); run in a dedicated CI step on a disposable DB",
)


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["alembic", *args], cwd=_BACKEND, capture_output=True, text=True)


@pytest.mark.asyncio
async def test_migration_round_trip_head_base_head() -> None:
    # downgrade the never-exercised leg…
    down = _alembic("downgrade", "base")
    assert down.returncode == 0, f"downgrade head→base failed:\n{down.stderr}"
    # …then rebuild
    up = _alembic("upgrade", "head")
    assert up.returncode == 0, f"upgrade base→head failed:\n{up.stderr}"

    # round-trip restores schema + roles + RLS + the 0014 reference seed
    conn = await asyncpg.connect(DB_URL_SYNC)
    try:
        roles = await conn.fetchval(
            "SELECT count(*) FROM pg_roles WHERE rolname IN ('app_user','service_worker')"
        )
        forced = await conn.fetchval(
            "SELECT relforcerowsecurity FROM pg_class WHERE relname='inventory_movements'"
        )
        seed = await conn.fetchval("SELECT count(*) FROM unit_conversions WHERE tenant_id IS NULL")
    finally:
        await conn.close()
    assert roles == 2, "round-trip did not recreate app_user + service_worker"
    assert forced is True, "round-trip lost FORCE RLS on inventory_movements"
    assert seed > 0, "round-trip did not restore the 0014 global unit_conversions seed"
