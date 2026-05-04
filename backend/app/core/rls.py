"""Row-Level Security session helpers.

Every request that touches tenant-scoped tables must call one of these helpers
before any query.  All helpers use ``SET LOCAL`` so variables reset at the end
of the current transaction — they never leak across requests from the pool.

RLS modes
─────────
``app.rls_mode``  Controls which bootstrap policies are active:
  ''              Normal operation (default).
  'register'      register-tenant bootstrap: allows INSERT on tenants and
                  user_tenants without an existing tenant_id context.
  'accept_invite' Accept-invite bootstrap: allows reading an invitation by
                  email (stored in ``app.invite_email``) and inserting a
                  new user_tenants row.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal


async def set_rls_context(session: AsyncSession, principal: Principal) -> None:
    """Normal request context — user_id + tenant_id + role."""
    await session.execute(
        text(
            "SELECT set_config('app.user_id', :uid, true),"
            "       set_config('app.tenant_id', :tid, true),"
            "       set_config('app.user_role', :role, true),"
            "       set_config('app.rls_mode', '', true)"
        ),
        {"uid": principal.user_id, "tid": principal.tenant_id, "role": principal.role},
    )


async def set_identity_context(session: AsyncSession, user_id: str) -> None:
    """Identity-only context — used by register-tenant (no tenant yet)."""
    await session.execute(
        text(
            "SELECT set_config('app.user_id', :uid, true),"
            "       set_config('app.tenant_id', '', true),"
            "       set_config('app.user_role', '', true),"
            "       set_config('app.rls_mode', 'register', true)"
        ),
        {"uid": user_id},
    )


async def set_accept_invite_context(session: AsyncSession, *, email: str) -> None:
    """Invite-accept bootstrap context — allows reading invite by email."""
    await session.execute(
        text(
            "SELECT set_config('app.user_id', '', true),"
            "       set_config('app.tenant_id', '', true),"
            "       set_config('app.user_role', '', true),"
            "       set_config('app.rls_mode', 'accept_invite', true),"
            "       set_config('app.invite_email', :email, true)"
        ),
        {"email": email},
    )


async def clear_rls_context(session: AsyncSession) -> None:
    """Reset all RLS session variables (useful between tests)."""
    await session.execute(
        text(
            "SELECT set_config('app.user_id', '', true),"
            "       set_config('app.tenant_id', '', true),"
            "       set_config('app.user_role', '', true),"
            "       set_config('app.rls_mode', '', true),"
            "       set_config('app.invite_email', '', true)"
        )
    )
