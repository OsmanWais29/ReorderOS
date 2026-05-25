"""Shared seed fixture for Phase 2+ inbox tests.

Creates the FK prerequisites (tenant + active pos_connection) that
pos_event_inbox rows depend on.
"""

from __future__ import annotations

import uuid
from typing import Any

from uuid6 import uuid7

from app.core.encryption import TokenEncryption

enc = TokenEncryption()


async def seed_inbox_prereqs(admin_conn: Any) -> dict:
    """Create a tenant and an active tenant_pos_connection.

    Returns dict with tenant_id and connection_id.
    Both are unique per call so parallel tests don't collide on the
    cross-tenant merchant uniqueness index.
    """
    tenant_id = str(uuid.uuid4())
    connection_id = str(uuid7())

    await admin_conn.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tenant_id,
        f"InboxTest_{tenant_id[:8]}",
        f"inbox-{tenant_id[:8]}",
    )
    await admin_conn.execute(
        """
        INSERT INTO tenant_pos_connections (
            connection_id, tenant_id, vendor, merchant_id, environment,
            access_token_enc, access_token_expires_at,
            refresh_token_enc, refresh_token_expires_at, state
        ) VALUES ($1,$2,'clover',$3,'sandbox',$4,
                  now()+interval'1 hour',$5,now()+interval'8 hours','active')
    """,
        connection_id,
        tenant_id,
        f"merch_{tenant_id[:8]}",
        enc.encrypt("seed_access_token"),
        enc.encrypt("seed_refresh_token"),
    )
    return {"tenant_id": tenant_id, "connection_id": connection_id}
