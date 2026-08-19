"""Executable proof of the per-component configuration contract.

Two layers, both driven from app.core.component_requirements (the single source of
truth shared by Settings and env_check):

  1. BOOT MATRIX — for every component: Settings constructs in production with EXACTLY
     the component's declared required set (no superset slack), and fails closed naming
     the key when any single required key is removed. The same matrix runs at the
     env_check layer.
  2. GATING — APP_COMPONENT resolution: legacy fallback only when the restricted-role
     flag is off; flag on + missing/unknown/legacy APP_COMPONENT fails startup; unknown
     value always fails (a typo must never silently change the requirement set).

Least-privilege negatives are explicit: the inbox worker boots WITHOUT any Clover
credential (CLOVER_APP_SECRET's only consumer is the API OAuth exchange —
pos/router.py), the inbound-email worker boots with ONLY its service DSN, and the
reconciliation worker needs CLOVER_APP_ID but never the app secret
(pos/token_refresh.py sends client_id only).

Feature-path-to-fake-provider proof lives in the existing suites cited in
docs/security/restricted-runtime-role-matrix.md (recipes LLM fakes, receipts storage
monkeypatch, WorkOS respx, Clover OAuth/webhook respx, Postmark webhook, extraction
fake client, inbox e2e, reconciliation suite, token refresh respx).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.component_requirements import (
    COMPONENTS,
    OPTIONAL_VARS,
    RESTRICTED_COMPONENTS,
    required_env_names,
)
from app.core.config import ENV_TO_FIELD, Settings
from app.ops.env_check import PROFILES, check_env

_ALL_FLAGS_ON = {"CLOVER_ENABLED": True, "POSTMARK_INBOUND_ENABLED": True}

# Realistic-but-fake values per env key (placeholder-safe: env_check rejects tokens
# like "test"/"dummy" on SECRET keys in production, so these must not look like those).
_FAKE = {
    "DATABASE_URL": "postgresql+asyncpg://u:pw-4f8a@db.internal:5432/app",
    "SERVICE_DATABASE_URL": "postgresql+asyncpg://svc:pw-9c2e@db.internal:5432/app",
    "TOKEN_ENCRYPTION_KEY": "fernet-key-unit-7d1b",
    "WORKOS_CLIENT_ID": "client_unit_01ABC",
    "WORKOS_JWKS_URL": "https://api.workos.com/sso/jwks/client_unit_01ABC",
    "WORKOS_ISSUER": "https://api.workos.com/user_management/client_unit_01ABC",
    "WORKOS_SECRET_KEY": "sk_unit_3e5f",
    "ANTHROPIC_API_KEY": "anthropic-key-unit-6a9d",
    "CLOVER_APP_ID": "CLOVERAPPID01",
    "CLOVER_APP_SECRET": "clover-secret-unit-2b7c",
    "CLOVER_WEBHOOK_AUTH_CODE": "webhook-auth-unit-8e4a",
    "DO_SPACES_ENDPOINT": "https://tor1.digitaloceanspaces.com",
    "DO_SPACES_REGION": "tor1",
    "DO_SPACES_BUCKET": "unit-bucket",
    "DO_SPACES_KEY": "spaces-key-unit-5c1d",
    "DO_SPACES_SECRET": "spaces-secret-unit-0f6b",
    "POSTMARK_WEBHOOK_USER": "inbound-user-unit",
    "POSTMARK_WEBHOOK_PASSWORD": "inbound-pass-unit-4d8e",
}

_BOOT_CASES = [
    (component, key)
    for component in sorted(RESTRICTED_COMPONENTS)
    for key in required_env_names(component, _ALL_FLAGS_ON)
]


def _exact_env(component: str, *, drop: str | None = None) -> dict[str, str]:
    """EXACTLY the component's required env (all flags on) + the flag/gating vars —
    nothing else. Optionally with one required key removed."""
    env = {
        "APP_ENV": "production",
        "APP_COMPONENT": component,
        "RESTRICTED_RUNTIME_ROLES_ENABLED": "true",
        "CLOVER_ENABLED": "true",
        "POSTMARK_INBOUND_ENABLED": "true",
    }
    for key in required_env_names(component, _ALL_FLAGS_ON):
        env[key] = _FAKE[key]
    if drop is not None:
        env.pop(drop, None)
    return env


def _settings(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Settings:
    # Hermetic: clear every var either layer could read, then set exactly `env`.
    for key in (
        set(_FAKE)
        | {v.name for v in OPTIONAL_VARS}
        | {"APP_ENV", "APP_COMPONENT", "RESTRICTED_RUNTIME_ROLES_ENABLED"}
        | {"CLOVER_ENABLED", "POSTMARK_INBOUND_ENABLED"}
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


# ── 1. boot matrix ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("component", sorted(RESTRICTED_COMPONENTS))
def test_component_boots_with_exactly_its_declared_env(
    component: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings constructs in production with EXACTLY the declared set — proof the set
    is sufficient, with zero superset slack."""
    s = _settings(_exact_env(component), monkeypatch)
    assert s.is_production and s.app_component == component
    # env_check agrees (same source of truth, independent checker):
    report = check_env(component, _exact_env(component))
    assert report.ready, report.failures


# Keys Settings itself can detect as absent: their field default is falsy (None). Keys
# with a truthy default (DATABASE_URL → localhost DSN, WORKOS_ISSUER → api.workos.com)
# are enforceable only at the env_check layer — which is exactly why every production
# entrypoint runs check_env BEFORE constructing Settings (unchanged from today: the
# legacy validator never covered these two either).
_SETTINGS_ENFORCEABLE = {
    key for key, field in ENV_TO_FIELD.items() if not Settings.model_fields[field].default
}


@pytest.mark.parametrize(("component", "drop"), _BOOT_CASES)
def test_component_fails_closed_without_each_required_key(
    component: str, drop: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every single declared key is LOAD-BEARING: removing it fails env_check
    (reason=missing) always, and fails Settings (named, fail-closed) for every key
    Settings can see as absent (falsy field default). Also proves nothing extra is
    demanded — the passing case above uses the identical env minus nothing."""
    report = check_env(component, _exact_env(component, drop=drop))
    assert report.failures.get(drop) == "missing"
    if drop in _SETTINGS_ENFORCEABLE:
        with pytest.raises(ValidationError) as exc:
            _settings(_exact_env(component, drop=drop), monkeypatch)
        assert drop in str(exc.value) and "fail-closed" in str(exc.value)
    else:
        # Defaulted field: Settings boots (uses the default); the env_check boot gate
        # above is the enforcing layer — same division of labor as pre-cutover.
        _settings(_exact_env(component, drop=drop), monkeypatch)


# ── least-privilege negatives (the trace-backed splits) ───────────────────────
def test_inbox_worker_needs_no_clover_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLOVER_APP_SECRET's only consumer is the API OAuth exchange (pos/router.py);
    the inbox worker calls Clover with decrypted MERCHANT tokens. It must boot with
    Clover ON and zero Clover credentials."""
    env = _exact_env("inbox_worker")
    assert "CLOVER_APP_SECRET" not in env and "CLOVER_APP_ID" not in env
    s = _settings(env, monkeypatch)
    assert s.clover_enabled  # flag on, yet no Clover secret demanded


def test_reconciliation_worker_needs_app_id_but_never_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pos/token_refresh.py sends client_id on the refresh grant — no app secret."""
    names = set(required_env_names("reconciliation_worker", _ALL_FLAGS_ON))
    assert "CLOVER_APP_ID" in names
    assert "CLOVER_APP_SECRET" not in names and "CLOVER_WEBHOOK_AUTH_CODE" not in names
    with pytest.raises(ValidationError):
        _settings(_exact_env("reconciliation_worker", drop="CLOVER_APP_ID"), monkeypatch)


def test_inbound_email_worker_needs_only_service_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    assert set(required_env_names("inbound_email_worker", _ALL_FLAGS_ON)) == {
        "SERVICE_DATABASE_URL"
    }
    _settings(_exact_env("inbound_email_worker"), monkeypatch)


def test_migrate_job_needs_only_database_url() -> None:
    assert set(required_env_names("migrate_job", _ALL_FLAGS_ON)) == {"DATABASE_URL"}


# ── 2. APP_COMPONENT gating ───────────────────────────────────────────────────
def _legacy_base() -> dict[str, str]:
    return {
        "APP_ENV": "production",
        "CLOVER_ENABLED": "false",
        "TOKEN_ENCRYPTION_KEY": _FAKE["TOKEN_ENCRYPTION_KEY"],
        "SERVICE_DATABASE_URL": _FAKE["SERVICE_DATABASE_URL"],
        "WORKOS_CLIENT_ID": _FAKE["WORKOS_CLIENT_ID"],
        "WORKOS_JWKS_URL": _FAKE["WORKOS_JWKS_URL"],
    }


def test_legacy_profile_selected_when_unset_and_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-cutover compatibility: no APP_COMPONENT + flag off → today's exact global
    fail-closed set (also pinned key-by-key in test_phase0_config.py). Production keeps
    booting unchanged."""
    s = _settings(_legacy_base(), monkeypatch)
    assert s.is_production and s.app_component is None


def test_legacy_set_is_byte_for_byte_the_pre_cutover_requirements() -> None:
    """The legacy profile IS the old validator's set — with both feature flags on it is
    exactly the seven keys test_phase0_config.py has always enforced, plus the Postmark
    pair its `when` gate adds."""
    assert set(required_env_names("legacy", {"CLOVER_ENABLED": True})) == {
        "TOKEN_ENCRYPTION_KEY",
        "SERVICE_DATABASE_URL",
        "WORKOS_CLIENT_ID",
        "WORKOS_JWKS_URL",
        "CLOVER_APP_ID",
        "CLOVER_APP_SECRET",
        "CLOVER_WEBHOOK_AUTH_CODE",
    }
    assert set(required_env_names("legacy", {})) == {
        "TOKEN_ENCRYPTION_KEY",
        "SERVICE_DATABASE_URL",
        "WORKOS_CLIENT_ID",
        "WORKOS_JWKS_URL",
    }


def test_flag_on_without_component_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """BITING (cutover safety): under the restricted flag there is NO legacy fallback —
    a component that does not self-identify must not boot."""
    env = _legacy_base() | {"RESTRICTED_RUNTIME_ROLES_ENABLED": "true"}
    with pytest.raises(ValidationError, match="APP_COMPONENT is required"):
        _settings(env, monkeypatch)


@pytest.mark.parametrize("flag", ["true", "false"])
def test_unknown_component_always_fails(flag: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must never silently select a different (possibly weaker) requirement set."""
    env = _legacy_base() | {
        "RESTRICTED_RUNTIME_ROLES_ENABLED": flag,
        "APP_COMPONENT": "receipts-extraction-worker",  # hyphens: a realistic typo
    }
    with pytest.raises(ValidationError, match="unknown APP_COMPONENT"):
        _settings(env, monkeypatch)


def test_explicit_legacy_component_rejected_under_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _legacy_base() | {
        "RESTRICTED_RUNTIME_ROLES_ENABLED": "true",
        "APP_COMPONENT": "legacy",
    }
    with pytest.raises(ValidationError, match="unknown APP_COMPONENT"):
        _settings(env, monkeypatch)


def test_non_production_ignores_component_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    """local/CI never require the secrets — unchanged from today."""
    s = _settings({"APP_ENV": "local", "RESTRICTED_RUNTIME_ROLES_ENABLED": "true"}, monkeypatch)
    assert not s.is_production


# ── shared-source-of-truth drift guards ───────────────────────────────────────
def test_env_to_field_covers_every_component_var() -> None:
    """Settings resolves required env names via ENV_TO_FIELD — a Var added to
    component_requirements without a field mapping would crash the validator; this
    catches the drift at test time, and proves each mapping targets a real field."""
    every_var = {v.name for vars_ in COMPONENTS.values() for v in vars_}
    assert every_var <= set(ENV_TO_FIELD), sorted(every_var - set(ENV_TO_FIELD))
    for field_name in ENV_TO_FIELD.values():
        assert field_name in Settings.model_fields, field_name


def test_env_check_profiles_are_the_component_sets() -> None:
    """env_check exposes exactly the components (plus the predeploy union + alias) —
    Settings and env_check can never disagree about a component's requirements."""
    assert {k: v for k, v in PROFILES.items() if k in COMPONENTS} == COMPONENTS
    assert set(PROFILES) - set(COMPONENTS) == {"predeploy_env_check", "production_deploy"}


def test_optional_vars_never_required() -> None:
    """TOKEN_ENCRYPTION_KEY_PREVIOUS (rotation) and POSTMARK_INBOUND_ADDRESS
    (configuration, not a credential) are preserved-when-present but never required —
    a fresh environment must not fail for lacking them."""
    optional = {v.name for v in OPTIONAL_VARS}
    for component in COMPONENTS:
        assert not (optional & set(required_env_names(component, _ALL_FLAGS_ON)))
