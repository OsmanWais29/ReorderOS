"""Typed runtime configuration loaded from env / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationError, field_validator
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
    clover_webhook_auth_code: str | None = Field(
        default=None, alias="CLOVER_WEBHOOK_AUTH_CODE"
    )
    clover_post_connect_redirect: str = Field(
        default="http://localhost:8081/onboarding/found-summary",
        alias="CLOVER_POST_CONNECT_REDIRECT",
    )

    # ── Token encryption (Fernet; supports key rotation via _previous) ───────────
    token_encryption_key: str | None = Field(default=None, alias="TOKEN_ENCRYPTION_KEY")
    token_encryption_key_previous: str | None = Field(
        default=None, alias="TOKEN_ENCRYPTION_KEY_PREVIOUS"
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
