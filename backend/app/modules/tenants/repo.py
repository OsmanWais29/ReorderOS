"""Tenant CRUD and principal resolution."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_sessionmaker
from app.core.rls import set_identity_context, set_rls_context
from app.core.security import Identity, Principal, Role
from app.modules.auth.models import User
from app.modules.auth.repo import upsert_user
from app.modules.tenants.models import Tenant, UserTenant


async def resolve_principal(
    identity: Identity,
    x_tenant_id: str | None,
) -> Principal:
    """Look up the DB user + membership and return a fully resolved Principal.

    Called from ``get_principal`` on every authenticated request that needs
    a tenant context.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        # Users table has no RLS — safe to query in any context.
        user_result = await session.execute(
            select(User).where(User.workos_id == identity.workos_id)
        )
        user = user_result.scalar_one_or_none()
        if user is None:
            raise HTTPException(
                status_code=400,
                detail="User not found; call /auth/register-tenant first",
            )

        if not x_tenant_id:
            raise HTTPException(
                status_code=400,
                detail="X-Tenant-Id header is required",
            )

        try:
            tenant_uuid = uuid.UUID(x_tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="X-Tenant-Id must be a valid UUID") from exc

        # Check membership directly (no RLS context needed — user_tenants
        # SELECT policy allows rows where user_id matches app.user_id, but
        # we set it here only for verification).
        membership = await session.execute(
            select(UserTenant).where(
                UserTenant.user_id == user.id,
                UserTenant.tenant_id == tenant_uuid,
                UserTenant.active.is_(True),
            )
        )
        ut = membership.scalar_one_or_none()
        if ut is None:
            raise HTTPException(
                status_code=403,
                detail="No active membership for this tenant",
            )

        role: Role = ut.role  # type: ignore[assignment]
        principal = Principal(
            user_id=str(user.id),
            workos_id=identity.workos_id,
            email=identity.email,
            tenant_id=str(tenant_uuid),
            role=role,
        )
        await set_rls_context(session, principal)
        return principal


async def register_tenant(
    session: AsyncSession,
    identity: Identity,
    *,
    slug: str,
    name: str,
) -> tuple[Tenant, User, UserTenant]:
    """Create tenant + owner membership atomically inside register RLS mode."""
    user = await upsert_user(session, identity)
    await set_identity_context(session, user_id=str(user.id))

    tenant = Tenant(slug=slug, name=name)
    session.add(tenant)
    await session.flush()  # populate tenant.id

    membership = UserTenant(
        user_id=user.id,
        tenant_id=tenant.id,
        role="owner",
        active=True,
    )
    session.add(membership)
    await session.flush()

    return tenant, user, membership


async def list_tenants_for_user(session: AsyncSession, user_id: uuid.UUID) -> list[Tenant]:
    """All active tenants the given user belongs to (no RLS context needed)."""
    result = await session.execute(
        select(Tenant)
        .join(UserTenant, UserTenant.tenant_id == Tenant.id)
        .where(
            UserTenant.user_id == user_id,
            UserTenant.active.is_(True),
        )
        .order_by(Tenant.name)
    )
    return list(result.scalars().all())


async def get_tenant_membership(
    session: AsyncSession,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> UserTenant | None:
    result = await session.execute(
        select(UserTenant).where(
            UserTenant.user_id == user_id,
            UserTenant.tenant_id == tenant_id,
            UserTenant.active.is_(True),
        )
    )
    return result.scalar_one_or_none()
