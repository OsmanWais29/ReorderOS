"""Settings tests."""

from __future__ import annotations

import pytest

from app.core.config import Settings


@pytest.mark.parametrize(
    ("input_url", "expected"),
    [
        # DigitalOcean App Platform injects this form when binding a managed DB.
        (
            "postgresql://u:p@db-postgresql-tor1-12345.b.db.ondigitalocean.com:25060/d?sslmode=require",
            "postgresql+asyncpg://u:p@db-postgresql-tor1-12345.b.db.ondigitalocean.com:25060/d?sslmode=require",
        ),
        # Heroku-style alias.
        (
            "postgres://u:p@host:5432/d",
            "postgresql+asyncpg://u:p@host:5432/d",
        ),
        # Already normalized — must pass through untouched.
        (
            "postgresql+asyncpg://u:p@host:5432/d",
            "postgresql+asyncpg://u:p@host:5432/d",
        ),
    ],
)
def test_database_url_normalizes_to_asyncpg(input_url: str, expected: str) -> None:
    s = Settings(DATABASE_URL=input_url)  # type: ignore[call-arg]
    assert s.database_url == expected
