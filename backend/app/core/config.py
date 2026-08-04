"""Typed runtime configuration loaded from env / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "ci", "staging", "production"]

# Env-var name → Settings field name, for the per-component fail-closed check.
# Covers every Var that appears in app.core.component_requirements.COMPONENTS;
# tests/test_component_requirements.py asserts the two can never drift.
ENV_TO_FIELD: dict[str, str] = {
    "DATABASE_URL": "database_url",
    "SERVICE_DATABASE_URL": "service_database_url",
    "TOKEN_ENCRYPTION_KEY": "token_encryption_key",
    "WORKOS_CLIENT_ID": "workos_client_id",
    "WORKOS_JWKS_URL": "workos_jwks_url",
    "WORKOS_ISSUER": "workos_issuer",
    "WORKOS_SECRET_KEY": "workos_secret_key",
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "CLOVER_APP_ID": "clover_app_id",
    "CLOVER_APP_SECRET": "clover_app_secret",
    "CLOVER_WEBHOOK_AUTH_CODE": "clover_webhook_auth_code",
    "DO_SPACES_ENDPOINT": "spaces_endpoint",
    "DO_SPACES_REGION": "spaces_region",
    "DO_SPACES_BUCKET": "spaces_bucket",
    "DO_SPACES_KEY": "spaces_key",
    "DO_SPACES_SECRET": "spaces_secret",
    "POSTMARK_WEBHOOK_USER": "postmark_webhook_user",
    "POSTMARK_WEBHOOK_PASSWORD": "postmark_webhook_password",
}


def normalize_postgres_url(v: object) -> object:
    """Normalize a Postgres DSN to the asyncpg driver scheme and strip libpq sslmode.

    Module-level (not just a validator) so Alembic can normalize DATABASE_URL WITHOUT
    constructing full Settings — the migrate job must not be forced to supply runtime
    request/worker secrets just to import. See alembic/env.py.
    """
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    if not isinstance(v, str):
        return v
    if v.startswith("postgresql://"):
        v = "postgresql+asyncpg://" + v[len("postgresql://") :]
    elif v.startswith("postgres://"):
        v = "postgresql+asyncpg://" + v[len("postgres://") :]
    # asyncpg uses connect_args={"ssl": ...} — strip libpq-style sslmode
    if "sslmode" in v:
        parsed = urlparse(v)
        params = {k: vals for k, vals in parse_qs(parsed.query).items() if k != "sslmode"}
        v = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))
    return v


class Settings(BaseSettings):
    """Process-wide settings.

    Read once at startup via ``get_settings()`` so unit tests can override by
    clearing the lru_cache and re-reading env.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        case_sensitive=False,
    )

    # ── App ──────────────────────────────────────────────────────────────────
    app_env: Environment = Field(default="local", alias="APP_ENV")
    app_log_level: str = Field(default="INFO", alias="APP_LOG_LEVEL")
    app_port: int = Field(default=8000, alias="APP_PORT")

    # WorkOS-free dev sign-in for laptop smoke tests (modules/auth/dev_local.py).
    # DOUBLE-GATED: this flag AND app_env in {local, ci}. Staging/production never
    # qualify regardless of the flag; the gate is re-checked on every request.
    local_dev_auth: bool = Field(default=False, alias="LOCAL_DEV_AUTH")

    # ── Restricted runtime roles (security cutover flag) ─────────────────────
    # Gates ONLY the fail-closed startup role assertions (API request pool must be
    # `reorderos_app`, service/worker pool must be `service_worker`, both
    # non-superuser + non-bypassrls). Default OFF: production stays on its current
    # admin-bound DATABASE_URL and must keep booting unchanged — APP_ENV=production
    # alone must NOT activate these assertions. The staging restricted-role cutover
    # sets this to "true"; a wrong/missing role then PREVENTS startup (fail-closed).
    # This flag never touches the RLS policies themselves — those are always active
    # per migration 0022/0035; it only controls the cutover-time startup assertion.
    restricted_runtime_roles_enabled: bool = Field(
        default=False, alias="RESTRICTED_RUNTIME_ROLES_ENABLED"
    )

    # Which deployment component this process is (api / inbox_worker /
    # reconciliation_worker / receipt_extraction_worker / inbound_email_worker /
    # migrate_job) — selects the component's production fail-closed requirement set
    # from app.core.component_requirements. Transition semantics (production only):
    #   unset + restricted flag OFF  → the 'legacy' compatibility profile (today's
    #                                  exact global behavior — pre-cutover safety)
    #   unset/unknown + flag ON      → startup FAILS with a named error (no fallback)
    #   valid value                  → that component's exact set, no more, no less
    app_component: str | None = Field(default=None, alias="APP_COMPONENT")

    # The login role every service pool must run as when the restricted flag is on.
    # Default "service_worker". A VERSIONED replacement role (service_worker_vN — the
    # outage-safe rotation path, runbook Phase B-alt) is a LOGIN member of
    # service_worker that inherits its grants/policies; cutting over to it sets this
    # to the versioned name in the same coordinated deployment as its DSN, so the
    # startup role assertion follows the credential — never a password-first rotation.
    service_role_name: str = Field(default="service_worker", alias="SERVICE_ROLE_NAME")

    # ── Database ─────────────────────────────────────────────────────────────
    # Async DSN (asyncpg). DigitalOcean App Platform injects ``postgresql://``
    # when binding a managed database; the validator upgrades that to the
    # asyncpg driver scheme so the engine works without a separate env var.
    database_url: str = Field(
        default="postgresql+asyncpg://reorderos:reorderos@localhost:5432/reorderos",
        alias="DATABASE_URL",
    )

    # ── Auth (WorkOS) ────────────────────────────────────────────────────────
    # Sprint 2 sets these. Empty in Sprint 1; auth middleware no-ops when unset.
    workos_client_id: str | None = Field(default=None, alias="WORKOS_CLIENT_ID")
    workos_jwks_url: str | None = Field(default=None, alias="WORKOS_JWKS_URL")
    # User Management JWTs use a client-scoped issuer:
    #   https://api.workos.com/user_management/<client_id>
    # This must match the `iss` claim in the token exactly.
    workos_issuer: str = Field(default="https://api.workos.com", alias="WORKOS_ISSUER")
    workos_verify_audience: bool = Field(default=False, alias="WORKOS_VERIFY_AUDIENCE")
    # Secret key used to fetch user profiles from WorkOS API when the JWT
    # does not include an email claim (standard for User Management tokens).
    workos_secret_key: str | None = Field(default=None, alias="WORKOS_SECRET_KEY")

    # ── Service worker DB (service_worker role — used by webhook + inbox worker) ─
    service_database_url: str | None = Field(default=None, alias="SERVICE_DATABASE_URL")

    # ── Clover POS ───────────────────────────────────────────────────────────────
    clover_app_id: str | None = Field(default=None, alias="CLOVER_APP_ID")
    clover_app_secret: str | None = Field(default=None, alias="CLOVER_APP_SECRET")
    clover_environment: str = Field(default="sandbox", alias="CLOVER_ENVIRONMENT")
    clover_api_base_url: str = Field(
        default="https://apisandbox.dev.clover.com", alias="CLOVER_API_BASE_URL"
    )
    clover_oauth_base_url: str = Field(
        default="https://sandbox.dev.clover.com", alias="CLOVER_OAUTH_BASE_URL"
    )
    clover_oauth_callback_url: str = Field(
        default="http://localhost:8000/api/v1/pos/clover/callback",
        alias="CLOVER_OAUTH_CALLBACK_URL",
    )
    clover_webhook_auth_code: str | None = Field(default=None, alias="CLOVER_WEBHOOK_AUTH_CODE")
    clover_post_connect_redirect: str = Field(
        default="https://reorderos.com/onboarding/found-summary",
        alias="CLOVER_POST_CONNECT_REDIRECT",
    )

    # ── Anthropic (Sprint 5 recipe LLM suggestion — quarantined to recipes/) ─────
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-4-6", alias="ANTHROPIC_MODEL")

    # ── Token encryption (Fernet; supports key rotation via _previous) ───────────
    token_encryption_key: str | None = Field(default=None, alias="TOKEN_ENCRYPTION_KEY")
    # Clover POS integration master switch. Default OFF: receipts (upload →
    # review → commit) must run without any Clover credentials. Environments
    # that intentionally run Clover (staging sandbox, prod pilots) set
    # CLOVER_ENABLED=true, which restores the fail-closed secret requirements.
    clover_enabled: bool = Field(default=False, alias="CLOVER_ENABLED")
    token_encryption_key_previous: str | None = Field(
        default=None, alias="TOKEN_ENCRYPTION_KEY_PREVIOUS"
    )

    # ── Postmark inbound email (Sprint 6 Phase 3b) ───────────────────────────
    # Same optionality pattern as CLOVER_ENABLED: OFF (default) the webhook 503s
    # and no Postmark credentials are required anywhere; ON restores fail-closed
    # (both Basic Auth halves required at production boot).
    postmark_inbound_enabled: bool = Field(default=False, alias="POSTMARK_INBOUND_ENABLED")
    postmark_webhook_user: str | None = Field(default=None, alias="POSTMARK_WEBHOOK_USER")
    postmark_webhook_password: str | None = Field(default=None, alias="POSTMARK_WEBHOOK_PASSWORD")
    # Base inbound address for the tenant forwarding-address endpoint, e.g.
    # "a1b2c3@inbound.postmarkapp.com" (Postmark default) or
    # "receipts@inbound.reorderos.com" (custom MX). Tenant addresses are
    # local+<token>@domain. Optional — endpoint reports configured:false without it.
    postmark_inbound_address: str | None = Field(default=None, alias="POSTMARK_INBOUND_ADDRESS")

    # ── CORS ─────────────────────────────────────────────────────────────────
    cors_origins: list[str] = Field(
        default=["http://localhost:8081"],
        alias="CORS_ORIGINS",
    )

    # ── Object storage (DigitalOcean Spaces; lazy-init in app.core.storage) ──
    spaces_endpoint: str | None = Field(default=None, alias="DO_SPACES_ENDPOINT")
    spaces_region: str | None = Field(default=None, alias="DO_SPACES_REGION")
    spaces_bucket: str | None = Field(default=None, alias="DO_SPACES_BUCKET")
    spaces_key: str | None = Field(default=None, alias="DO_SPACES_KEY")
    spaces_secret: str | None = Field(default=None, alias="DO_SPACES_SECRET")

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_postgres_url(cls, v: object) -> object:
        return normalize_postgres_url(v)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @model_validator(mode="after")
    def _require_secrets_in_production(self) -> Settings:
        """Fail-closed config (F1.3), per-component. Secrets default to None so local/dev/
        test run without them — but a PRODUCTION deploy missing one its component actually
        consumes would boot SILENTLY and fail only at the first OAuth / webhook / worker
        call. Raise at construction instead, so a misconfigured prod deploy never starts.

        The required set comes from app.core.component_requirements (shared with
        app.ops.env_check — one source of truth). APP_COMPONENT selects it; when unset:
        'legacy' (today's exact global set) if the restricted-role flag is off, a hard
        failure if the flag is on. Unknown values always fail — a typo must not silently
        weaken (or change) the requirement set.
        (ValueError → pydantic ValidationError → get_settings() RuntimeError.)"""
        if self.app_env != "production":
            return self
        from app.core.component_requirements import (
            COMPONENTS,
            RESTRICTED_COMPONENTS,
            required_env_names,
        )

        component = (self.app_component or "").strip()
        if not component:
            if self.restricted_runtime_roles_enabled:
                raise ValueError(
                    "APP_COMPONENT is required when RESTRICTED_RUNTIME_ROLES_ENABLED is "
                    "true (no legacy fallback under the cutover flag); set it to one of: "
                    + ", ".join(sorted(RESTRICTED_COMPONENTS))
                )
            component = "legacy"
        elif component not in COMPONENTS or (
            self.restricted_runtime_roles_enabled and component not in RESTRICTED_COMPONENTS
        ):
            raise ValueError(
                f"unknown APP_COMPONENT {component!r} (valid: "
                + ", ".join(sorted(RESTRICTED_COMPONENTS))
                + ")"
            )
        flags = {
            "CLOVER_ENABLED": self.clover_enabled,
            "POSTMARK_INBOUND_ENABLED": self.postmark_inbound_enabled,
        }
        missing = [
            name
            for name in required_env_names(component, flags)
            if not getattr(self, ENV_TO_FIELD[name])
        ]
        if missing:
            raise ValueError(
                f"Production config for component {component!r} is missing required "
                "secrets (fail-closed): " + ", ".join(missing)
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"Invalid configuration: {exc}") from exc


class _SettingsProxy:
    """Lazy settings alias — attribute access resolves ``get_settings()`` on demand.

    Deliberately does NOT construct Settings at import time. Merely importing
    ``app.core.config`` (which Alembic does transitively via models → Base → database)
    must NOT run the production fail-closed secret check: the migrate PRE_DEPLOY job runs
    DDL only and must not be handed live WorkOS/Clover/Postmark/token/service secrets just
    to import. Callers that use ``from app.core.config import settings`` still work — the
    first attribute access builds (and caches) the real Settings. Mirrors ``_EngineProxy``
    in app/core/database.py. Tests that monkeypatch env call ``get_settings.cache_clear()``.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(get_settings(), name)


settings = _SettingsProxy()
