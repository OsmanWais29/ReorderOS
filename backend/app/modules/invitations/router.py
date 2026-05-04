"""Invitation routes: POST /invitations, POST /invitations/accept."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy.exc import IntegrityError

from app.core.database import get_session
from app.core.rls import set_rls_context
from app.core.security import Identity, Principal, Role, get_identity, get_principal
from app.modules.auth.repo import get_user_by_workos_id
from app.modules.invitations.models import Invitation
from app.modules.invitations.repo import (
    accept_invitation,
    create_invitation,
    list_invitations,
    revoke_invitation,
)

router = APIRouter(prefix="/invitations", tags=["invitations"])

# ── Schemas ───────────────────────────────────────────────────────────────────


class CreateInvitationRequest(BaseModel):
    email: EmailStr
    role: Role = Field(default="staff")


class InvitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    role: str
    token: str
    accepted_at: str | None
    expires_at: str

    @classmethod
    def from_orm(cls, inv: Invitation) -> InvitationOut:
        return cls(
            id=inv.id,
            tenant_id=inv.tenant_id,
            email=inv.email,
            role=inv.role,
            token=inv.token,
            accepted_at=inv.accepted_at.isoformat() if inv.accepted_at else None,
            expires_at=inv.expires_at.isoformat(),
        )


class AcceptInvitationRequest(BaseModel):
    token: str


class AcceptInvitationResponse(BaseModel):
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str


# ── Routes ────────────────────────────────────────────────────────────────────


@router.post("", response_model=InvitationOut, status_code=201)
async def create_invite(
    body: CreateInvitationRequest,
    principal: Annotated[Principal, Depends(get_principal)],
    session: Annotated[object, Depends(get_session)],
) -> InvitationOut:
    """Create an invitation — owner only."""
    from sqlalchemy.ext.asyncio import AsyncSession as _AS

    db: _AS = session  # type: ignore[assignment]

    if principal.role != "owner":
        raise HTTPException(status_code=403, detail="Only owners can create invitations")

    await set_rls_context(db, principal)

    user = await get_user_by_workos_id(db, principal.workos_id)
    if user is None:
        raise HTTPException(status_code=400, detail="User not found")

    try:
        inv = await create_invitation(
            db,
            tenant_id=uuid.UUID(principal.tenant_id),
            email=str(body.email),
            role=body.role,
            created_by=user.id,
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Conflict") from exc

    return InvitationOut.from_orm(inv)


@router.get("", response_model=list[InvitationOut])
async def list_invites(
    principal: Annotated[Principal, Depends(get_principal)],
    session: Annotated[object, Depends(get_session)],
) -> list[InvitationOut]:
    """List invitations for the active tenant — owner only."""
    from sqlalchemy.ext.asyncio import AsyncSession as _AS

    db: _AS = session  # type: ignore[assignment]

    if principal.role != "owner":
        raise HTTPException(status_code=403, detail="Only owners can list invitations")

    await set_rls_context(db, principal)
    invs = await list_invitations(db, uuid.UUID(principal.tenant_id))
    return [InvitationOut.from_orm(i) for i in invs]


@router.post("/accept", response_model=AcceptInvitationResponse, status_code=200)
async def accept_invite(
    body: AcceptInvitationRequest,
    identity: Annotated[Identity, Depends(get_identity)],
    session: Annotated[object, Depends(get_session)],
) -> AcceptInvitationResponse:
    """Accept an invitation — requires a verified email."""
    from sqlalchemy.ext.asyncio import AsyncSession as _AS

    db: _AS = session  # type: ignore[assignment]

    if not identity.email_verified:
        raise HTTPException(status_code=403, detail="Email must be verified to accept invitations")

    try:
        membership = await accept_invitation(
            db,
            token=body.token,
            identity_email=identity.email,
            identity_workos_id=identity.workos_id,
        )
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except IntegrityError as exc:
        await db.rollback()
        # UNIQUE violation on (user_id, tenant_id) → already a member
        raise HTTPException(status_code=409, detail="Already a member of this tenant") from exc

    return AcceptInvitationResponse(
        user_id=membership.user_id,
        tenant_id=membership.tenant_id,
        role=membership.role,
    )


@router.delete("/{invitation_id}", status_code=204)
async def revoke_invite(
    invitation_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    session: Annotated[object, Depends(get_session)],
) -> None:
    """Revoke a pending invitation — owner only."""
    from sqlalchemy.ext.asyncio import AsyncSession as _AS

    db: _AS = session  # type: ignore[assignment]

    if principal.role != "owner":
        raise HTTPException(status_code=403, detail="Only owners can revoke invitations")

    await set_rls_context(db, principal)
    await revoke_invitation(db, invitation_id)
    await db.commit()
