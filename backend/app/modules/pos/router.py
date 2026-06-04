"""Clover POS OAuth routes.

  GET  /api/v1/pos/clover/connect     — owner-only; creates CSRF state, redirects to Clover
  GET  /api/v1/pos/clover/callback    — unauthenticated; validates state, stores tokens
  POST /api/v1/pos/clover/disconnect  — owner-only; sets connection state='revoked'

The callback is unauthenticated at the HTTP layer but still tenant-authenticated
via the CSRF state token, which was created by an authenticated /connect call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.core.config import get_settings
from app.core.database import get_session
from app.core.encryption import TokenEncryption
from app.core.security import Principal, get_principal
from app.modules.pos.catalog_sync import CatalogSyncService
from app.modules.pos.state_manager import OAuthStateManager

router = APIRouter(prefix="/pos/clover", tags=["pos"])
_state_mgr = OAuthStateManager()


def _from_unix_ms(ms: int) -> datetime:
    """Convert Clover's millisecond Unix timestamp to UTC datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


# ── Auth dependencies — exported so tests can override them ──────────────────


async def get_current_user(principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    """Resolve the authenticated principal to a plain dict.

    Returning a dict (not Principal) lets tests override this dependency with
    a simple async function that returns a dict — no Principal construction needed.
    """
    return {
        "tenant_id": principal.tenant_id,
        "user_id": principal.user_id,
        "role": principal.role,
    }


def _require_owner(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """403 for any role that is not 'owner'."""
    if user.get("role") != "owner":
        raise HTTPException(
            status_code=403,
            detail=f"Owner role required; you have '{user.get('role')}'",
        )
    return user


def _require_manager(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """403 for any role below manager (manager or owner pass).

    Catalog/menu operations are Manager-allowed per v1-scope (Manager owns
    recipes/items operationally).
    """
    if user.get("role") not in ("owner", "manager"):
        raise HTTPException(
            status_code=403,
            detail=f"Manager role or higher required; you have '{user.get('role')}'",
        )
    return user


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/connect")
async def connect(
    user: dict[str, Any] = Depends(_require_owner),
    db: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Initiate Clover OAuth flow (server-side redirect, for browser navigation)."""
    settings = get_settings()
    if not settings.clover_app_id:
        raise HTTPException(status_code=503, detail="Clover integration not configured")

    state_token = await _state_mgr.generate(user["tenant_id"], user["user_id"])
    authorize_url = (
        f"{settings.clover_oauth_base_url}/oauth/authorize"
        f"?client_id={settings.clover_app_id}"
        f"&redirect_uri={settings.clover_oauth_callback_url}"
        f"&state={state_token}"
    )
    return RedirectResponse(url=authorize_url, status_code=302)


@router.get("/connect-url")
async def connect_url(
    user: dict[str, Any] = Depends(_require_owner),
) -> dict[str, Any]:
    """Return the Clover OAuth URL as JSON.

    SPA / mobile clients call this with an Authorization header (which a browser
    redirect cannot send), then redirect the browser to the returned URL themselves.
    """
    settings = get_settings()
    if not settings.clover_app_id:
        raise HTTPException(status_code=503, detail="Clover integration not configured")

    state_token = await _state_mgr.generate(user["tenant_id"], user["user_id"])
    authorize_url = (
        f"{settings.clover_oauth_base_url}/oauth/authorize"
        f"?client_id={settings.clover_app_id}"
        f"&redirect_uri={settings.clover_oauth_callback_url}"
        f"&state={state_token}"
    )
    return {"url": authorize_url}


@router.get("/callback")
@router.post("/callback")
async def callback(
    merchant_id: str,
    code: str,
    state: str,
    background_tasks: BackgroundTasks,
    client_id: str | None = None,
    db: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Handle Clover OAuth callback.

    1. Validates and atomically consumes the CSRF state token.
    2. Optionally checks the client_id echo.
    3. Exchanges the authorization code for tokens.
    4. Writes encrypted tokens using SET LOCAL app.tenant_id (T1 RLS).
    """
    settings = get_settings()

    # Step 1: validate state → recovers tenant_id + user_id
    tenant_id, user_id = await _state_mgr.validate_and_consume(state)

    # Step 2: client_id guard
    if client_id and client_id != settings.clover_app_id:
        raise HTTPException(status_code=400, detail="client_id mismatch")

    # Step 3: exchange authorization code for tokens
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{settings.clover_api_base_url}/oauth/v2/token",
            json={
                "client_id": settings.clover_app_id,
                "client_secret": settings.clover_app_secret,
                "code": code,
            },
        )
    resp.raise_for_status()
    tokens = resp.json()

    enc = TokenEncryption()
    conn_id = uuid7()

    # Step 4: set_config with is_local=true before writing — app_user T1 RLS
    # requires the tenant context. SET LOCAL doesn't accept parameters; use
    # set_config(setting, value, is_local) which does.
    await db.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": tenant_id},
    )
    result = await db.execute(
        text("""
            INSERT INTO tenant_pos_connections (
                connection_id, tenant_id, vendor, merchant_id, environment,
                access_token_enc, access_token_expires_at,
                refresh_token_enc, refresh_token_expires_at,
                configured_permissions, state, connected_by_user_id
            ) VALUES (
                :cid, :tid, 'clover', :mid, :env,
                :at_enc, :at_exp, :rt_enc, :rt_exp,
                :perms, 'active', :uid
            )
            ON CONFLICT (tenant_id, vendor) WHERE state = 'active'
            DO UPDATE SET
                merchant_id              = EXCLUDED.merchant_id,
                access_token_enc         = EXCLUDED.access_token_enc,
                access_token_expires_at  = EXCLUDED.access_token_expires_at,
                refresh_token_enc        = EXCLUDED.refresh_token_enc,
                refresh_token_expires_at = EXCLUDED.refresh_token_expires_at,
                state                    = 'active',
                state_reason             = NULL,
                refresh_failure_count    = 0,
                updated_at               = now()
            RETURNING connection_id
        """),
        {
            "cid": str(conn_id),
            "tid": tenant_id,
            "mid": merchant_id,
            "env": settings.clover_environment,
            "at_enc": enc.encrypt(tokens["access_token"]),
            "at_exp": _from_unix_ms(tokens["access_token_expiration"]),
            "rt_enc": enc.encrypt(tokens["refresh_token"]),
            "rt_exp": _from_unix_ms(tokens["refresh_token_expiration"]),
            "perms": "ORDERS_R,EMPLOYEES_R,PAYMENTS_R,INVENTORY_R,MERCHANT_R",
            "uid": user_id,
        },
    )
    # The real connection_id: on reconnect the ON CONFLICT DO UPDATE keeps the
    # existing row's id, not the freshly-generated conn_id — so capture it.
    active_conn_id = result.scalar_one()
    await db.commit()

    # Kick the catalog sync off the HTTP path so the menu populates immediately
    # for the Recipes screen. Passes only the connection_id (a scalar); the task
    # opens its own service-worker session, never the request-scoped db.
    background_tasks.add_task(CatalogSyncService().sync_connection, str(active_conn_id))

    return RedirectResponse(
        url=f"{settings.clover_post_connect_redirect}?connected=true",
        status_code=302,
    )


@router.get("/status")
async def status(
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return the tenant's current Clover connection state.

    Used by the frontend settings page on every load — tells it whether to
    show "Connect Clover" or "Connected to <merchant_id>" and whether the
    initial order sync has completed.
    """
    await db.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": user["tenant_id"]},
    )
    result = await db.execute(
        text("""
            SELECT
                connection_id,
                merchant_id,
                state,
                initial_sync_completed_at,
                last_reconciliation_at,
                last_reconciliation_cursor,
                refresh_failure_count,
                created_at
            FROM tenant_pos_connections
            WHERE tenant_id = :tid AND vendor = 'clover'
            ORDER BY created_at DESC
            LIMIT 1
        """),
        {"tid": user["tenant_id"]},
    )
    row = result.fetchone()
    if row is None or row.state == "revoked":
        return {"connected": False}

    return {
        "connected": row.state == "active",
        "state": row.state,
        "merchant_id": row.merchant_id,
        "initial_sync_completed": row.initial_sync_completed_at is not None,
        "last_reconciliation_at": (
            row.last_reconciliation_at.isoformat() if row.last_reconciliation_at else None
        ),
        "refresh_failure_count": row.refresh_failure_count,
    }


@router.post("/disconnect")
async def disconnect(
    user: dict[str, Any] = Depends(_require_owner),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Revoke the tenant's active Clover connection.

    Sets state='revoked' on the active connection row. The connection
    row is preserved for audit history. lookup_tenant_by_merchant
    will no longer resolve this merchant (revoked excluded from filter).
    """
    await db.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": user["tenant_id"]},
    )
    await db.execute(
        text("""
            UPDATE tenant_pos_connections
            SET state      = 'revoked',
                revoked_at = now(),
                updated_at = now()
            WHERE tenant_id = :tid
              AND vendor    = 'clover'
              AND state     = 'active'
        """),
        {"tid": user["tenant_id"]},
    )
    await db.commit()
    return {"status": "disconnected"}


@router.post("/sync-menu", status_code=202)
async def sync_menu(
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(_require_manager),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Operator-triggered catalog re-sync from Clover (Manager+).

    Recovery path for a lost initial sync, and a way to pull menu changes made in
    Clover. Schedules the sync off the HTTP path and returns 202 immediately;
    404 if the tenant has no active Clover connection.
    """
    await db.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": user["tenant_id"]},
    )
    row = (
        await db.execute(
            text("""
                SELECT connection_id
                FROM tenant_pos_connections
                WHERE tenant_id = :tid AND vendor = 'clover' AND state = 'active'
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {"tid": user["tenant_id"]},
        )
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No active Clover connection")

    background_tasks.add_task(CatalogSyncService().sync_connection, str(row[0]))
    return {"status": "sync_started"}
