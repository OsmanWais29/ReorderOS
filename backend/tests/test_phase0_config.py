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
