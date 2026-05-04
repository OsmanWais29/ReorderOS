"""User upsert and lookup."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Identity
from app.modules.auth.models import User


async def upsert_user(session: AsyncSession, identity: Identity) -> User:
    """Insert or update the user record from JWT claims.

    Uses ON CONFLICT (workos_id) to handle the case where the user already
    exists.  The ``users`` table has no RLS so this works in any context.
    """
    stmt = (
        pg_insert(User)
        .values(
            workos_id=identity.workos_id,
            email=identity.email,
            email_verified=identity.email_verified,
            first_name=identity.first_name,
            last_name=identity.last_name,
        )
        .on_conflict_do_update(
            index_elements=["workos_id"],
            set_={
                "email": identity.email,
                "email_verified": identity.email_verified,
                "first_name": identity.first_name,
                "last_name": identity.last_name,
                "updated_at": func.now(),
            },
        )
        .returning(User)
    )
    result = await session.execute(stmt)
    user = result.scalar_one()
    return user


async def get_user_by_workos_id(session: AsyncSession, workos_id: str) -> User | None:
    result = await session.execute(select(User).where(User.workos_id == workos_id))
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def mark_email_verified(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        update(User).where(User.id == user_id).values(email_verified=True, updated_at=func.now())
    )
