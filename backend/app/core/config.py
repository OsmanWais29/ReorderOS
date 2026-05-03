"""Typed runtime configuration loaded from env / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationError
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
    # Async DSN (asyncpg). Stored as plain str so SQLAlchemy can consume it
    # directly; we only need it to be syntactically valid.
    database_url: str = Field(
        default="postgresql+asyncpg://reorderos:reorderos@localhost:5432/reorderos",
        alias="DATABASE_URL",
    )

    # ── Auth (filled in Sprint 2) ────────────────────────────────────────────
    clerk_jwks_url: str | None = Field(default=None, alias="CLERK_JWKS_URL")
    clerk_issuer: str | None = Field(default=None, alias="CLERK_ISSUER")

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"Invalid configuration: {exc}") from exc
