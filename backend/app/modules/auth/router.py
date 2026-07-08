"""Auth routes: /auth/register-tenant, /auth/me, /auth/active-tenant, /auth/exchange."""

from __future__ import annotations

import uuid
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.database import get_session
from app.core.security import Identity, Principal, get_identity, get_principal
from app.modules.auth.models import User
from app.modules.auth.repo import upsert_user
from app.modules.tenants.models import Tenant, UserTenant
from app.modules.tenants.repo import (
    list_tenants_for_user,
    register_tenant,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# ── Request / response schemas ────────────────────────────────────────────────


class RegisterTenantRequest(BaseModel):
    slug: str = Field(..., min_length=2, max_length=63, pattern=r"^[a-z0-9-]+$")
    name: str = Field(..., min_length=1, max_length=200)


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    slug: str
    name: str
    plan: str

    @classmethod
    def from_orm(cls, t: Tenant) -> TenantOut:
        return cls(id=t.id, slug=t.slug, name=t.name, plan=t.plan)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    workos_id: str
    email: str
    email_verified: bool
    first_name: str | None
    last_name: str | None

    @classmethod
    def from_orm(cls, u: User) -> UserOut:
        return cls(
            id=u.id,
            workos_id=u.workos_id,
            email=u.email,
            email_verified=u.email_verified,
            first_name=u.first_name,
            last_name=u.last_name,
        )


class MembershipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str

    @classmethod
    def from_orm(cls, m: UserTenant) -> MembershipOut:
        return cls(user_id=m.user_id, tenant_id=m.tenant_id, role=m.role)


class RegisterTenantResponse(BaseModel):
    tenant: TenantOut
    user: UserOut
    membership: MembershipOut


class MeResponse(BaseModel):
    user: UserOut
    tenants: list[TenantOut]
    active_tenant_id: str | None


class ActiveTenantRequest(BaseModel):
    tenant_id: uuid.UUID


class ActiveTenantResponse(BaseModel):
    tenant_id: uuid.UUID
    role: str


# ── Routes ────────────────────────────────────────────────────────────────────


@router.post("/register-tenant", response_model=RegisterTenantResponse, status_code=201)
async def register_tenant_route(
    body: RegisterTenantRequest,
    identity: Annotated[Identity, Depends(get_identity)],
    session: Annotated[object, Depends(get_session)],
) -> RegisterTenantResponse:
    """Create a new tenant and make the caller its owner.

    Uses register-mode RLS context so the insert bypasses the normal
    tenant_id policy.  Duplicate slug returns 409.
    """
    from sqlalchemy.ext.asyncio import AsyncSession as _AS

    db: _AS = session  # type: ignore[assignment]

    try:
        tenant, user, membership = await register_tenant(
            db, identity, slug=body.slug, name=body.name
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if "slug" in str(exc.orig) or "uq_" in str(exc.orig) or "unique" in str(exc.orig).lower():
            raise HTTPException(status_code=409, detail="Slug already taken") from exc
        raise HTTPException(status_code=409, detail="Conflict") from exc

    return RegisterTenantResponse(
        tenant=TenantOut.from_orm(tenant),
        user=UserOut.from_orm(user),
        membership=MembershipOut.from_orm(membership),
    )


@router.get("/me", response_model=MeResponse)
async def me(
    identity: Annotated[Identity, Depends(get_identity)],
    session: Annotated[object, Depends(get_session)],
) -> MeResponse:
    from sqlalchemy.ext.asyncio import AsyncSession as _AS

    db: _AS = session  # type: ignore[assignment]

    user = await upsert_user(db, identity)
    await db.commit()

    tenants = await list_tenants_for_user(db, user.id)
    return MeResponse(
        user=UserOut.from_orm(user),
        tenants=[TenantOut.from_orm(t) for t in tenants],
        active_tenant_id=None,
    )


@router.patch("/active-tenant", response_model=ActiveTenantResponse)
async def set_active_tenant(
    body: ActiveTenantRequest,
    principal: Annotated[Principal, Depends(get_principal)],
) -> ActiveTenantResponse:
    """Switch the caller's active tenant.

    The X-Tenant-Id header + get_principal already verified membership,
    so this just echoes back the resolved tenant + role.
    """
    return ActiveTenantResponse(
        tenant_id=uuid.UUID(principal.tenant_id),
        role=principal.role,
    )


# ── LOCAL-ONLY dev sign-in (WorkOS-free laptop smoke tests) ───────────────────


class DevSignInResponse(BaseModel):
    access_token: str
    tenant_id: str
    email: str
    role: str


@router.post("/dev-sign-in", response_model=DevSignInResponse)
async def dev_sign_in(
    session: Annotated[object, Depends(get_session)],
) -> DevSignInResponse:
    """Create-or-reuse the local dev tenant + owner user and mint a dev token.

    DOUBLE-GATED (modules/auth/dev_local.py): requires LOCAL_DEV_AUTH=true AND
    app_env in {local, ci} — re-checked here on every call. Outside the gate the
    route answers 404 (indistinguishable from not existing), so an accidentally
    deployed flag exposes nothing. No secrets involved beyond the pre-existing
    env-provided TOKEN_ENCRYPTION_KEY the token key derives from.
    """
    from app.modules.auth.dev_local import (
        DEV_EMAIL,
        DEV_TENANT_NAME,
        DEV_TENANT_SLUG,
        DEV_WORKOS_ID,
        dev_local_auth_enabled,
        mint_dev_token,
    )

    s = get_settings()
    if not dev_local_auth_enabled(s):
        raise HTTPException(status_code=404, detail="Not Found")

    from sqlalchemy.ext.asyncio import AsyncSession as _AS

    db: _AS = session  # type: ignore[assignment]

    identity = Identity(
        workos_id=DEV_WORKOS_ID,
        email=DEV_EMAIL,
        email_verified=True,
        first_name="Dev",
        last_name="Manager",
    )
    user = await upsert_user(db, identity)

    # Reuse the dev tenant when this user already owns one; otherwise create it.
    tenants = await list_tenants_for_user(db, user.id)
    tenant = next((t for t in tenants if t.slug == DEV_TENANT_SLUG), None)
    if tenant is None:
        try:
            tenant, user, _membership = await register_tenant(
                db, identity, slug=DEV_TENANT_SLUG, name=DEV_TENANT_NAME
            )
        except IntegrityError:
            # Slug exists but this user isn't a member (partially wiped dev DB):
            # adopt the existing tenant by adding the owner membership.
            await db.rollback()
            from sqlalchemy import select

            user = await upsert_user(db, identity)
            found = await db.execute(select(Tenant).where(Tenant.slug == DEV_TENANT_SLUG))
            tenant = found.scalar_one()
            db.add(UserTenant(user_id=user.id, tenant_id=tenant.id, role="owner", active=True))
    await db.commit()

    return DevSignInResponse(
        access_token=mint_dev_token(s),
        tenant_id=str(tenant.id),
        email=DEV_EMAIL,
        role="owner",
    )


# ── PKCE code exchange ────────────────────────────────────────────────────────


class ExchangeResponse(BaseModel):
    access_token: str


class SignInRequest(BaseModel):
    email: str
    password: str


@router.post("/sign-in", response_model=ExchangeResponse)
async def sign_in(body: SignInRequest) -> ExchangeResponse:
    """Embedded email/password sign-in — calls WorkOS password grant directly."""
    s = get_settings()
    if not s.workos_secret_key or not s.workos_client_id:
        raise HTTPException(status_code=503, detail="Auth not configured")

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://api.workos.com/user_management/authenticate",
            json={
                "client_id": s.workos_client_id,
                "client_secret": s.workos_secret_key,
                "grant_type": "password",
                "email": body.email,
                "password": body.password,
            },
        )

    if r.status_code != 200:
        detail = r.json().get("message") or r.json().get("error", "Invalid credentials")
        raise HTTPException(status_code=401, detail=detail)

    return ExchangeResponse(access_token=r.json()["access_token"])


class ExchangeRequest(BaseModel):
    code: str
    code_verifier: str | None = None
    redirect_uri: str


@router.post("/exchange", response_model=ExchangeResponse)
async def exchange_code(body: ExchangeRequest) -> ExchangeResponse:
    """Exchange a PKCE authorization code for a WorkOS access token.

    The mobile/web client sends code + code_verifier here so the client_secret
    never leaves the server.
    """
    s = get_settings()
    if not s.workos_secret_key or not s.workos_client_id:
        raise HTTPException(status_code=503, detail="Auth not configured")

    payload: dict[str, str] = {
        "client_id": s.workos_client_id,
        "client_secret": s.workos_secret_key,
        "grant_type": "authorization_code",
        "code": body.code,
        "redirect_uri": body.redirect_uri,
    }
    if body.code_verifier:
        payload["code_verifier"] = body.code_verifier

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://api.workos.com/user_management/authenticate",
            json=payload,
        )

    if r.status_code != 200:
        detail = r.json().get("message") or r.json().get("error", "authentication failed")
        raise HTTPException(status_code=401, detail=detail)

    return ExchangeResponse(access_token=r.json()["access_token"])
