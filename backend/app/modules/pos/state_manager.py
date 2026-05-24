"""OAuth CSRF state manager.

Creates and consumes single-use state tokens stored in oauth_states.
The state token (a random UUID, the primary key) is the security mechanism.
Tokens expire after 10 minutes; deletion is atomic via DELETE...RETURNING.

Both methods manage their own sessions so callers don't need to pass one.
This allows tests to call generate()/validate_and_consume() directly without
setting up a session fixture.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import text

from app.core.database import get_sessionmaker


class OAuthStateManager:
    """Stateless helper — all state lives in the oauth_states table."""

    async def generate(self, tenant_id: str, user_id: str) -> str:
        """Insert a new CSRF state token. Returns the token string."""
        state = str(uuid.uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        async with get_sessionmaker()() as session:
            async with session.begin():
                await session.execute(
                    text("""
                        INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
                        VALUES (:state, :tenant_id, :user_id, :expires_at)
                    """),
                    {
                        "state": state,
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "expires_at": expires_at,
                    },
                )
        return state

    async def validate_and_consume(self, state_token: str) -> tuple[str, str]:
        """Atomically delete state and return (tenant_id, user_id).

        Raises HTTP 400 if the token is missing, already consumed, or expired.
        DELETE...RETURNING is atomic — no race condition between check and delete.
        """
        async with get_sessionmaker()() as session:
            async with session.begin():
                result = await session.execute(
                    text("""
                        DELETE FROM oauth_states
                        WHERE state = :state AND expires_at > now()
                        RETURNING tenant_id, user_id
                    """),
                    {"state": state_token},
                )
                row = result.fetchone()
        if row is None:
            raise HTTPException(status_code=400, detail="Invalid or expired state token")
        return str(row.tenant_id), str(row.user_id)
