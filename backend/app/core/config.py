"""Typed runtime configuration loaded from env / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "ci", "staging", "production"]


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
    token_encryption_key_previous: str | None = Field(
        default=None, alias="TOKEN_ENCRYPTION_KEY_PREVIOUS"
    )

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
        from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

        if not isinstance(v, str):
            return v
        if v.startswith("postgresql://"):
            v = "postgresql+asyncpg://" + v[len("postgresql://") :]
        elif v.startswith("postgres://"):
            v = "postgresql+asyncpg://" + v[len("postgres://") :]
        # asyncpg uses connect_args={"ssl": True} — strip libpq-style sslmode
        if "sslmode" in v:
            parsed = urlparse(v)
            params = {k: vals for k, vals in parse_qs(parsed.query).items() if k != "sslmode"}
            v = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @model_validator(mode="after")
    def _require_secrets_in_production(self) -> Settings:
        """Fail-closed config (F1.3). The security/core secrets below default to None so
        local/dev/test run without them — but a PRODUCTION deploy missing any would boot
        SILENTLY and fail only at the first OAuth / webhook / worker call (silently-deaf-on-
        misconfig). Raise at construction instead, so a misconfigured prod deploy never starts.
        (ValueError → pydantic ValidationError → get_settings() RuntimeError.)"""
        if self.app_env != "production":
            return self
        # Required at every production launch: auth (WorkOS), token encryption, Clover OAuth +
        # webhook auth, and the worker DB URL.
        required: dict[str, object] = {
            "TOKEN_ENCRYPTION_KEY": self.token_encryption_key,
            "SERVICE_DATABASE_URL": self.service_database_url,
            "WORKOS_CLIENT_ID": self.workos_client_id,
            "WORKOS_JWKS_URL": self.workos_jwks_url,
            "CLOVER_APP_ID": self.clover_app_id,
            "CLOVER_APP_SECRET": self.clover_app_secret,
            "CLOVER_WEBHOOK_AUTH_CODE": self.clover_webhook_auth_code,
        }
        # Sprint 6 (receipts / photo extraction) makes these REQUIRED the moment it ships —
        # move them into `required` above at receipts launch (or NOW if receipts are in the
        # first pilot):
        #     "ANTHROPIC_API_KEY":  self.anthropic_api_key,
        #     "DO_SPACES_KEY":      self.spaces_key,
        #     "DO_SPACES_SECRET":   self.spaces_secret,
        #     "DO_SPACES_BUCKET":   self.spaces_bucket,
        #     "DO_SPACES_ENDPOINT": self.spaces_endpoint,
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError(
                "Production config is missing required secrets (fail-closed): " + ", ".join(missing)
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"Invalid configuration: {exc}") from exc


# Module-level alias used by tests and modules that import settings directly.
# Always goes through the cache — call get_settings.cache_clear() in tests
# that monkeypatch env vars before importing this symbol.
settings = get_settings()
