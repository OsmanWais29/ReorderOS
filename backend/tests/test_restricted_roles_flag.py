"""RESTRICTED_RUNTIME_ROLES_ENABLED — the cutover flag that gates the startup role
assertions (and ONLY those; RLS policies are always active regardless).

Production safety contract (these fail against an APP_ENV-gated implementation):
  - the flag defaults to false;
  - with the flag false, API startup does NOT invoke the strict role assertion even under
    APP_ENV=production — so production's admin-bound request pool keeps booting unchanged
    (the whole test suite is the live proof of the same: every `client` fixture boots the
    app as the local superuser with the flag unset);
  - with the flag true, the assertion IS invoked (and it is fail-closed: wrong roles raise —
    proven against real DB roles in tests/test_startup_role_gate.py).

Worker contract: all four workers assert their service-pool role through the flag-gated
helper `assert_service_pool_role_if_enabled` — a no-op (no session opened) when the flag is
off, mandatory fail-closed when on.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

import app.core.rls_assert as rls_assert
from app.core.config import get_settings
from app.core.rls_assert import assert_service_pool_role_if_enabled

_WORKERS_DIR = pathlib.Path(__file__).resolve().parents[1] / "app" / "workers"
_WORKER_MODULES = (
    "inbox_worker.py",
    "reconciliation_worker.py",
    "receipt_extraction_worker.py",
    "inbound_email_worker.py",
)


@pytest.fixture(autouse=True)
def _fresh_settings_cache() -> object:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _production_env(monkeypatch: pytest.MonkeyPatch, flag: str | None) -> None:
    """A production env that satisfies Settings' fail-closed check (Clover/Postmark off).

    flag None  → legacy posture (no APP_COMPONENT, no flag): the pre-cutover shape.
    flag "true"→ cutover posture: APP_COMPONENT=api is set (the flag forbids running
                 without one) with the api component's full required set."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "unit-test-fernet-key-not-real")
    monkeypatch.setenv("SERVICE_DATABASE_URL", "postgresql+asyncpg://svc:pw@db.example/app")
    monkeypatch.setenv("WORKOS_CLIENT_ID", "client_unit_test")
    monkeypatch.setenv("WORKOS_JWKS_URL", "https://api.workos.com/sso/jwks/client_unit_test")
    monkeypatch.setenv("CLOVER_ENABLED", "false")  # conftest pins it true suite-wide
    monkeypatch.delenv("POSTMARK_INBOUND_ENABLED", raising=False)
    if flag is None:
        monkeypatch.delenv("RESTRICTED_RUNTIME_ROLES_ENABLED", raising=False)
        monkeypatch.delenv("APP_COMPONENT", raising=False)
    else:
        monkeypatch.setenv("RESTRICTED_RUNTIME_ROLES_ENABLED", flag)
        monkeypatch.setenv("APP_COMPONENT", "api")
        monkeypatch.setenv("WORKOS_SECRET_KEY", "workos-secret-unit")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key-unit")
        monkeypatch.setenv("DO_SPACES_ENDPOINT", "https://tor1.digitaloceanspaces.com")
        monkeypatch.setenv("DO_SPACES_REGION", "tor1")
        monkeypatch.setenv("DO_SPACES_BUCKET", "unit-bucket")
        monkeypatch.setenv("DO_SPACES_KEY", "spaces-key-unit")
        monkeypatch.setenv("DO_SPACES_SECRET", "spaces-secret-unit")


# ── the flag itself ───────────────────────────────────────────────────────────
def test_flag_defaults_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RESTRICTED_RUNTIME_ROLES_ENABLED", raising=False)
    get_settings.cache_clear()
    assert get_settings().restricted_runtime_roles_enabled is False


def test_production_env_alone_does_not_enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """APP_ENV=production must NOT activate restricted-role enforcement by itself."""
    _production_env(monkeypatch, flag=None)
    get_settings.cache_clear()
    s = get_settings()
    assert s.app_env == "production"
    assert s.restricted_runtime_roles_enabled is False


# ── API lifespan gating ───────────────────────────────────────────────────────
async def _run_lifespan_and_count(
    monkeypatch: pytest.MonkeyPatch, flag: str | None
) -> dict[str, int]:
    import app.main as m

    _production_env(monkeypatch, flag)
    get_settings.cache_clear()
    calls = {"schema": 0, "roles": 0}

    async def fake_schema() -> None:
        calls["schema"] += 1

    async def fake_roles(log: object) -> None:
        calls["roles"] += 1

    async def fake_dispose() -> None:
        return None

    monkeypatch.setattr(m, "_assert_schema_at_head", fake_schema)
    monkeypatch.setattr(m, "_assert_runtime_roles", fake_roles)
    monkeypatch.setattr(m, "dispose_engine", fake_dispose)
    monkeypatch.setattr(m, "check_env", lambda profile: SimpleNamespace(ready=True, failures={}))
    async with m.lifespan(None):  # type: ignore[arg-type]
        pass
    return calls


async def test_lifespan_skips_role_assertion_when_flag_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRODUCTION SAFETY (biting vs the pre-fix APP_ENV gate): under APP_ENV=production
    with the flag unset, startup must NOT invoke the strict role assertion — an
    admin-bound production request pool must not fail solely for not being reorderos_app."""
    calls = await _run_lifespan_and_count(monkeypatch, flag=None)
    assert calls["schema"] == 1  # the schema-head gate still runs
    assert calls["roles"] == 0  # ← fails against the APP_ENV-gated implementation


async def test_lifespan_runs_role_assertion_when_flag_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = await _run_lifespan_and_count(monkeypatch, flag="true")
    assert calls["roles"] == 1


async def test_lifespan_role_assertion_failure_prevents_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag on + wrong/missing role ⇒ startup must abort (fail-closed), not degrade."""
    import app.main as m

    _production_env(monkeypatch, flag="true")
    get_settings.cache_clear()

    async def fake_schema() -> None:
        return None

    async def failing_roles(log: object) -> None:
        raise RuntimeError("request-pool role invalid")

    async def fake_dispose() -> None:
        return None

    monkeypatch.setattr(m, "_assert_schema_at_head", fake_schema)
    monkeypatch.setattr(m, "_assert_runtime_roles", failing_roles)
    monkeypatch.setattr(m, "dispose_engine", fake_dispose)
    monkeypatch.setattr(m, "check_env", lambda profile: SimpleNamespace(ready=True, failures={}))
    with pytest.raises(RuntimeError, match="role invalid"):
        async with m.lifespan(None):  # type: ignore[arg-type]
            pass


# ── worker helper gating ──────────────────────────────────────────────────────
async def test_helper_is_noop_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag off → returns None WITHOUT opening a service session (production workers on an
    admin service pool keep starting unchanged)."""
    _production_env(monkeypatch, flag=None)
    get_settings.cache_clear()

    async def boom() -> str:
        raise AssertionError("service session must not be opened with the flag off")

    monkeypatch.setattr(rls_assert, "assert_service_pool_role", boom)
    assert await assert_service_pool_role_if_enabled() is None


async def test_helper_delegates_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _production_env(monkeypatch, flag="true")
    get_settings.cache_clear()

    async def fake() -> str:
        return "service_worker"

    monkeypatch.setattr(rls_assert, "assert_service_pool_role", fake)
    assert await assert_service_pool_role_if_enabled() == "service_worker"


async def test_helper_fails_closed_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag on + wrong role ⇒ the helper propagates the failure (worker startup dies)."""
    _production_env(monkeypatch, flag="true")
    get_settings.cache_clear()

    async def wrong_role() -> str:
        raise RuntimeError("service-pool role invalid: current_user=doadmin")

    monkeypatch.setattr(rls_assert, "assert_service_pool_role", wrong_role)
    with pytest.raises(RuntimeError, match="service-pool role invalid"):
        await assert_service_pool_role_if_enabled()


def test_all_workers_use_the_flag_gated_helper() -> None:
    """Every worker entry point must gate its role assertion on the FLAG (via the helper),
    never on APP_ENV, and never call the un-gated assertion directly."""
    for name in _WORKER_MODULES:
        src = (_WORKERS_DIR / name).read_text()
        assert "assert_service_pool_role_if_enabled(" in src, f"{name}: helper not used"
        assert "await assert_service_pool_role()" not in src, (
            f"{name}: calls the un-gated assertion — must go through "
            f"assert_service_pool_role_if_enabled"
        )
