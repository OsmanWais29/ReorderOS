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

# Async DSN the migrate job would use (alembic env reads DATABASE_URL via Settings).
_ASYNC_DB_URL = DB_URL_SYNC.replace("postgresql://", "postgresql+asyncpg://")

# The migrate job runs with ONLY DATABASE_URL — no runtime request/worker secrets. This is
# the END-TO-END run (a production-env migration reaches 0035 with just DATABASE_URL set).
# The DETERMINISTIC decouple guard is test_env_check.test_alembic_env_reads_url_without_
# constructing_settings (a static check that env.py never calls get_settings()); this
# subprocess inherits neither os.environ nor forces .env absence, so it is not, by itself,
# proof that the secrets are gone — the static guard is. Manual airtight check: run alembic
# under `env -i APP_ENV=production DATABASE_URL=… ` and confirm it reaches 0035.
_PROD_ENV = {
    "APP_ENV": "production",
    "DATABASE_URL": _ASYNC_DB_URL,
}


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["alembic", *args], cwd=_BACKEND, capture_output=True, text=True)


def _alembic_env(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    # Hermetic base: only the vars needed to run the tool + the caller's explicit env. Does
    # NOT spread os.environ, so inherited runtime secrets can't leak into the subprocess.
    # (Note: Settings would still read a .env in the CWD if env.py were re-coupled — the
    # deterministic decouple guard is test_env_check.test_alembic_env_reads_url_without_
    # constructing_settings; this run is the end-to-end that the migration applies.)
    base = {
        k: os.environ[k]
        for k in ("PATH", "HOME", "VIRTUAL_ENV", "LANG", "LC_ALL")
        if k in os.environ
    }
    return subprocess.run(
        ["alembic", *args], cwd=_BACKEND, capture_output=True, text=True, env={**base, **env}
    )


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


@pytest.mark.asyncio
async def test_migration_persists_under_production_env() -> None:
    """REGRESSION (security PR): under APP_ENV=production the DDL-capability preflight
    runs. If that preflight queried the MIGRATION connection, it would open a
    transaction Alembic declines to commit — the migration would log 'Running
    upgrade' and exit 0 while SILENTLY ROLLING BACK (head never advances). This test
    proves 0035 actually PERSISTS under the production env: head advances AND the
    policy/grant it defines are really in place. Fails against the pre-fix env.py."""
    down = _alembic_env(_PROD_ENV, "downgrade", "0034_stock_insights_support")
    assert down.returncode == 0, f"downgrade→0034 failed:\n{down.stderr}"
    up = _alembic_env(_PROD_ENV, "upgrade", "head")
    assert up.returncode == 0, f"upgrade→head failed:\n{up.stderr}"

    conn = await asyncpg.connect(DB_URL_SYNC)
    try:
        head = await conn.fetchval("SELECT version_num FROM alembic_version")
        roles = await conn.fetchval(
            "SELECT roles::text FROM pg_policies WHERE tablename='tenants' AND policyname='tenant_select'"
        )
        grant = await conn.fetchval(
            "SELECT has_table_privilege('app_user','alembic_version','SELECT')"
        )
    finally:
        await conn.close()
    # The migration must have COMMITTED, not silently rolled back:
    assert head == "0035_restricted_runtime_role", "0035 did not persist under production env"
    assert roles == "{app_user}", "tenant_select not scoped TO app_user → 0035 body rolled back"
    assert grant is True, "alembic_version grant missing → 0035 body rolled back"
