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

    # ── Auth (filled in Sprint 2) ────────────────────────────────────────────
    clerk_jwks_url: str | None = Field(default=None, alias="CLERK_JWKS_URL")
    clerk_issuer: str | None = Field(default=None, alias="CLERK_ISSUER")

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
