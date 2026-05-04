"""Tenant routes: GET /tenants."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from app.core.database import get_session
from app.core.security import Identity, get_identity
from app.modules.auth.repo import get_user_by_workos_id
from app.modules.tenants.models import Tenant
from app.modules.tenants.repo import list_tenants_for_user

router = APIRouter(prefix="/tenants", tags=["tenants"])


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    slug: str
    name: str
    plan: str

    @classmethod
    def from_orm(cls, t: Tenant) -> TenantOut:
        return cls(id=t.id, slug=t.slug, name=t.name, plan=t.plan)


@router.get("", response_model=list[TenantOut])
async def list_tenants(
    identity: Annotated[Identity, Depends(get_identity)],
    session: Annotated[object, Depends(get_session)],
) -> list[TenantOut]:
    """Return all tenants the caller is an active member of."""
    from sqlalchemy.ext.asyncio import AsyncSession as _AS

    db: _AS = session  # type: ignore[assignment]

    user = await get_user_by_workos_id(db, identity.workos_id)
    if user is None:
        return []
    tenants = await list_tenants_for_user(db, user.id)
    return [TenantOut.from_orm(t) for t in tenants]
