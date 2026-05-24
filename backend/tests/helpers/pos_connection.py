"""Shared helpers for Phase 1+ tests: tenant_pos_connections row factory."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from uuid6 import uuid7

from app.core.encryption import TokenEncryption

FUTURE_1H = datetime.now(timezone.utc) + timedelta(hours=1)
FUTURE_8H = datetime.now(timezone.utc) + timedelta(hours=8)

enc = TokenEncryption()


def make_connection_row(overrides: dict | None = None) -> dict:
    """Build a valid tenant_pos_connections row with sensible defaults.

    Every field that isn't overridden gets a safe default. The merchant_id
    is uuid-based so parallel test runs and repeated runs against the same
    DB don't conflict on the partial unique index.
    """
    defaults: dict[str, Any] = {
        "connection_id": str(uuid7()),
        "tenant_id": None,
        "vendor": "clover",
        "merchant_id": f"m_{uuid.uuid4().hex[:12]}",
        "environment": "sandbox",
        "access_token_enc": enc.encrypt("default_access_token"),
        "access_token_expires_at": FUTURE_1H,
        "refresh_token_enc": enc.encrypt("default_refresh_token"),
        "refresh_token_expires_at": FUTURE_8H,
        "state": "active",
        "state_reason": None,
        "refresh_failure_count": 0,
    }
    if overrides:
        defaults.update(overrides)
    return defaults


async def insert_connection(conn: Any, row: dict) -> None:
    await conn.execute("""
        INSERT INTO tenant_pos_connections (
            connection_id, tenant_id, vendor, merchant_id, environment,
            access_token_enc, access_token_expires_at,
            refresh_token_enc, refresh_token_expires_at,
            state, state_reason, refresh_failure_count
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
    """,
        row["connection_id"], row["tenant_id"], row["vendor"],
        row["merchant_id"], row["environment"],
        row["access_token_enc"], row["access_token_expires_at"],
        row["refresh_token_enc"], row["refresh_token_expires_at"],
        row["state"], row.get("state_reason"), row.get("refresh_failure_count", 0),
    )


async def seed_tenant(conn: Any, tenant_id: str, name: str, slug: str) -> None:
    """Insert a tenant with explicit id — needed since tenant_id is a FK."""
    await conn.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tenant_id, name, slug,
    )
