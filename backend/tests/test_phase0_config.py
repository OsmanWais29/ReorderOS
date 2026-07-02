"""Phase 0 — Test 7: Sprint 4 config keys all present and non-empty.

Uses the module-level `settings` alias from app.core.config so the test
reads from the same cached instance the application uses at runtime.
"""

from __future__ import annotations

import pytest

# These are the Pydantic attribute names (snake_case) on the Settings class.
# The env var names (UPPER_CASE) are the aliases — tested separately if needed.
SPRINT_4_REQUIRED_ATTRS = [
    "service_database_url",
    "clover_app_id",
    "clover_app_secret",
    "clover_api_base_url",
    "clover_oauth_base_url",
    "clover_webhook_auth_code",
    "token_encryption_key",
]


@pytest.mark.parametrize("attr", SPRINT_4_REQUIRED_ATTRS)
def test_sprint4_config_key_is_present(attr):
    """Each Sprint 4 config key must be set and non-empty.

    Why: pydantic-settings defaults to None or empty string for missing
    env vars — it does NOT raise at startup unless required=True. The
    failure happens at runtime:

      - Missing CLOVER_APP_SECRET   → OAuth token exchange returns 401
      - Missing CLOVER_WEBHOOK_AUTH_CODE → webhook rejects every Clover call
      - Missing TOKEN_ENCRYPTION_KEY → first OAuth callback raises RuntimeError
      - Missing SERVICE_DATABASE_URL → worker process crashes on first connect

    Running this test in CI with a .env file that has dummy values proves
    the config class parses every key correctly, so deployment failures
    are visible before production.
    """
    from app.core.config import settings

    value = getattr(settings, attr, None)
    assert value is not None and value != "", (
        f"Sprint 4 config '{attr}' is missing or empty. "
        f"Set the corresponding env var before deploying."
    )


# ── F1.3: production fail-closed on missing security secrets ───────────────────

_PROD_REQUIRED_ENV = [
    "TOKEN_ENCRYPTION_KEY",
    "SERVICE_DATABASE_URL",
    "WORKOS_CLIENT_ID",
    "WORKOS_JWKS_URL",
    "CLOVER_APP_ID",
    "CLOVER_APP_SECRET",
    "CLOVER_WEBHOOK_AUTH_CODE",
]


def _set_all_prod_secrets(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/d")
    vals = {
        "TOKEN_ENCRYPTION_KEY": "k",
        "SERVICE_DATABASE_URL": "postgresql://u:p@h:5432/d",
        "WORKOS_CLIENT_ID": "client",
        "WORKOS_JWKS_URL": "https://jwks",
        "CLOVER_APP_ID": "app",
        "CLOVER_APP_SECRET": "secret",
        "CLOVER_WEBHOOK_AUTH_CODE": "whc",
    }
    for k, v in vals.items():
        monkeypatch.setenv(k, v)


@pytest.mark.parametrize("missing", _PROD_REQUIRED_ENV)
def test_production_fails_closed_on_missing_secret(monkeypatch, missing):
    """F1.3: in production, an absent required secret must FAIL THE BOOT (not boot silently deaf).
    Injects the failure (drops one secret) — reddens if the production validator is removed."""
    from pydantic import ValidationError

    from app.core.config import Settings

    _set_all_prod_secrets(monkeypatch)
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert "fail-closed" in str(exc.value) and missing in str(exc.value)


def test_production_boots_with_all_secrets(monkeypatch):
    """All required secrets present → production boots (no false-positive)."""
    from app.core.config import Settings

    _set_all_prod_secrets(monkeypatch)
    s = Settings(_env_file=None)
    assert s.is_production


def test_non_production_boots_without_secrets(monkeypatch):
    """local/dev/test never require the secrets — the validator is production-only."""
    from app.core.config import Settings

    monkeypatch.setenv("APP_ENV", "local")
    for var in _PROD_REQUIRED_ENV:
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert not s.is_production
