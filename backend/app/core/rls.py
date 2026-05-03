"""Row-Level Security session helpers.

Sprint 2 will use ``set_rls_context`` per-request to set
``app.tenant_id`` and ``app.user_role`` on the connection. Sprint 1 only
defines the surface; nothing calls it yet.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal


async def set_rls_context(session: AsyncSession, principal: Principal) -> None:
    """Bind the current principal to RLS session variables.

    Uses ``SET LOCAL`` so the values reset at transaction end. RLS policies
    read ``current_setting('app.tenant_id', true)``.
    """
    await session.execute(
        text("SET LOCAL app.tenant_id = :tid"),
        {"tid": principal.tenant_id},
    )
    await session.execute(
        text("SET LOCAL app.user_role = :role"),
        {"role": principal.role},
    )


async def clear_rls_context(session: AsyncSession) -> None:
    """Reset RLS session variables (useful between tests)."""
    await session.execute(text("RESET app.tenant_id"))
    await session.execute(text("RESET app.user_role"))
