"""Invitation CRUD with pessimistic locking on accept."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rls import set_accept_invite_context
from app.core.security import Role
from app.modules.auth.models import User
from app.modules.auth.repo import upsert_user
from app.modules.invitations.models import Invitation
from app.modules.tenants.models import UserTenant

_DEFAULT_EXPIRY_DAYS = 7


async def create_invitation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    email: str,
    role: Role,
    created_by: uuid.UUID,
) -> Invitation:
    inv = Invitation(
        tenant_id=tenant_id,
        email=email.lower().strip(),
        role=role,
        token=secrets.token_hex(32),
        expires_at=datetime.now(UTC) + timedelta(days=_DEFAULT_EXPIRY_DAYS),
        created_by=created_by,
    )
    session.add(inv)
    await session.flush()
    return inv


async def list_invitations(session: AsyncSession, tenant_id: uuid.UUID) -> list[Invitation]:
    result = await session.execute(
        select(Invitation)
        .where(Invitation.tenant_id == tenant_id)
        .order_by(Invitation.created_at.desc())
    )
    return list(result.scalars().all())


async def accept_invitation(
    session: AsyncSession,
    *,
    token: str,
    identity_email: str,
    identity_workos_id: str,
) -> UserTenant:
    """Accept an invitation with pessimistic row-lock to prevent double-accept.

    Uses SELECT ... FOR UPDATE so concurrent requests serialise — only one
    will succeed in updating ``accepted_at``.
    """
    from app.core.security import Identity

    email_lower = identity_email.lower().strip()
    await set_accept_invite_context(session, email=email_lower)

    # Lock the invitation row before any state check.
    result = await session.execute(
        select(Invitation).where(Invitation.token == token).with_for_update()
    )
    inv = result.scalar_one_or_none()

    if inv is None:
        raise HTTPException(status_code=404, detail="Invitation not found")

    if inv.email.lower() != email_lower:
        raise HTTPException(status_code=404, detail="Invitation not found")

    now = datetime.now(UTC)
    if inv.expires_at < now:
        raise HTTPException(status_code=410, detail="Invitation has expired")

    if inv.accepted_at is not None:
        raise HTTPException(status_code=409, detail="Invitation already accepted")

    # Mark accepted.
    await session.execute(update(Invitation).where(Invitation.id == inv.id).values(accepted_at=now))

    # Upsert user (fake identity for upsert — only workos_id matters here).
    fake_identity = Identity(
        workos_id=identity_workos_id,
        email=identity_email,
        email_verified=True,
        first_name=None,
        last_name=None,
    )
    user: User = await upsert_user(session, fake_identity)

    # Now that the invitee's user id is known, bind app.user_id to it so the
    # membership INSERT ... RETURNING below is readable via the narrow
    # `user_id = app.user_id` arm of user_tenant_select — no unconditional
    # bootstrap carve-out (which would expose every tenant's memberships while
    # accept_invite mode is live). user_tenant_insert's WITH CHECK still keys on
    # rls_mode='accept_invite'.
    await session.execute(
        text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user.id)}
    )

    # Insert membership.
    role: Role = inv.role  # type: ignore[assignment]
    membership = UserTenant(
        user_id=user.id,
        tenant_id=inv.tenant_id,
        role=role,
        active=True,
    )
    session.add(membership)
    await session.flush()  # INSERT ... RETURNING, visible via user_id = app.user_id
    return membership


async def revoke_invitation(
    session: AsyncSession, tenant_id: uuid.UUID, invitation_id: uuid.UUID
) -> None:
    # App-layer tenant scoping (F2.2): the fetch is filtered by tenant_id so a revoke can only
    # touch the caller's own invitation. RLS is a bypassed backstop when the app runs as a
    # superuser role, so this explicit filter — not RLS — is the actual cross-tenant guard
    # (invitations has no DELETE policy; the only working protection is this WHERE).
    result = await session.execute(
        select(Invitation).where(
            Invitation.id == invitation_id,
            Invitation.tenant_id == tenant_id,
        )
    )
    inv = result.scalar_one_or_none()
    if inv is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if inv.accepted_at is not None:
        raise HTTPException(status_code=409, detail="Cannot revoke accepted invitation")
    await session.delete(inv)
    await session.flush()
