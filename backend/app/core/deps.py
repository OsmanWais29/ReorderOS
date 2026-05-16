"""Shared FastAPI dependencies used across all tenant-scoped modules.

get_rls_session is the standard session dependency for any endpoint that
touches tenant-scoped tables.  It sets the PostgreSQL session variables
app.tenant_id, app.user_id, and app.user_role so that FORCE ROW LEVEL
SECURITY policies activate correctly for app_user connections.

Every inventory, orders, receipts, and forecast endpoint must inject this
instead of get_db_session directly.
"""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.core.rls import set_rls_context
from app.core.security import Principal, get_principal


async def get_rls_session(
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
) -> AsyncSession:
    """Return a session with RLS context set for the authenticated principal.

    Calling set_rls_context() runs SET LOCAL for app.tenant_id, app.user_id,
    and app.user_role inside the current transaction.  SET LOCAL resets
    automatically at transaction end, so context never leaks across requests.
    """
    await set_rls_context(session, principal)
    return session
