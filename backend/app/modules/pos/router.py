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
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.core.config import get_settings
from app.core.database import get_session
from app.core.encryption import TokenEncryption
from app.core.security import Principal, get_principal
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
    await db.execute(
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
    await db.commit()

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


@router.get("/sync-progress")
async def sync_progress(
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return step-by-step sync progress for the connecting screen.

    Steps:
      1. oauth — always true if connection exists
      2. menu  — count of menu_items synced
      3. sales — 30-day sales total from orders
      4. inventory — count of inventory_items
    """
    tid = user["tenant_id"]
    await db.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": tid},
    )

    conn = await db.execute(
        text("""
            SELECT state, initial_sync_started_at, initial_sync_completed_at
            FROM tenant_pos_connections
            WHERE tenant_id = :tid AND vendor = 'clover'
              AND state IN ('active', 'error')
            ORDER BY created_at DESC LIMIT 1
        """),
        {"tid": tid},
    )
    row = conn.fetchone()
    if row is None:
        return {"connected": False, "steps": []}

    menu_count_row = await db.execute(
        text("SELECT COUNT(*) FROM menu_items WHERE tenant_id = :tid AND active = true"),
        {"tid": tid},
    )
    menu_count: int = menu_count_row.scalar() or 0

    sales_row = await db.execute(
        text("""
            SELECT COALESCE(SUM(total_amount_cents), 0), COUNT(*)
            FROM orders
            WHERE tenant_id = :tid
              AND closed_at >= now() - interval '30 days'
              AND state = 'locked'
        """),
        {"tid": tid},
    )
    sales_data = sales_row.fetchone()
    sales_cents: int = sales_data[0] if sales_data else 0
    order_count: int = sales_data[1] if sales_data else 0

    inv_row = await db.execute(
        text("SELECT COUNT(*) FROM inventory_items WHERE tenant_id = :tid AND active = true"),
        {"tid": tid},
    )
    inv_count: int = inv_row.scalar() or 0

    sync_started = row.initial_sync_started_at is not None
    sync_done = row.initial_sync_completed_at is not None

    steps = [
        {"key": "oauth", "label": "Authenticating with OAuth", "done": True},
        {
            "key": "menu",
            "label": f"Pulling menu ({menu_count} items)",
            "done": sync_done and menu_count > 0,
            "count": menu_count,
        },
        {
            "key": "sales",
            "label": f"30 days sales (${sales_cents / 100:,.0f})",
            "done": sync_done and order_count > 0,
            "count": order_count,
            "total_cents": sales_cents,
        },
        {
            "key": "inventory",
            "label": f"Inventory levels ({inv_count} SKUs)",
            "done": sync_done and inv_count > 0,
            "count": inv_count,
        },
    ]

    return {
        "connected": True,
        "sync_started": sync_started,
        "sync_completed": sync_done,
        "steps": steps,
    }


@router.get("/sync-summary")
async def sync_summary(
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return aggregated summary for the found-summary screen.

    Includes merchant info (from tenant), menu count, 30d sales,
    and top 3 selling items.
    """
    tid = user["tenant_id"]
    await db.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": tid},
    )

    conn_row = await db.execute(
        text("""
            SELECT connection_id, merchant_id, access_token_enc, environment
            FROM tenant_pos_connections
            WHERE tenant_id = :tid AND vendor = 'clover'
              AND state IN ('active', 'error')
            ORDER BY created_at DESC LIMIT 1
        """),
        {"tid": tid},
    )
    conn = conn_row.fetchone()
    if conn is None:
        return {"connected": False}

    merchant_name: str | None = None
    merchant_location: str | None = None
    try:
        enc = TokenEncryption()
        access_token = enc.decrypt(conn.access_token_enc)
        from app.modules.pos.clover_client import CloverClient

        clover = CloverClient(
            access_token=access_token,
            merchant_id=conn.merchant_id,
            environment=conn.environment,
        )
        merchant = await clover.get_merchant()
        merchant_name = merchant.get("name")
        addr = merchant.get("address") or {}
        city = addr.get("city", "")
        state_code = addr.get("state", "")
        if city and state_code:
            merchant_location = f"{city}, {state_code}"
        elif city:
            merchant_location = city
    except Exception:
        pass

    menu_row = await db.execute(
        text("SELECT COUNT(*) FROM menu_items WHERE tenant_id = :tid AND active = true"),
        {"tid": tid},
    )
    menu_count: int = menu_row.scalar() or 0

    sales_row = await db.execute(
        text("""
            SELECT COALESCE(SUM(total_amount_cents), 0)
            FROM orders
            WHERE tenant_id = :tid
              AND closed_at >= now() - interval '30 days'
              AND state = 'locked'
        """),
        {"tid": tid},
    )
    net_sales_cents: int = sales_row.scalar() or 0

    top_items_row = await db.execute(
        text("""
            SELECT sli.name_at_sale, SUM(sli.quantity)::int AS total_qty
            FROM sale_line_items sli
            JOIN orders o ON o.id = sli.order_id AND o.tenant_id = sli.tenant_id
            WHERE sli.tenant_id = :tid
              AND o.closed_at >= now() - interval '30 days'
              AND o.state = 'locked'
              AND NOT sli.is_refunded
              AND NOT sli.is_voided
            GROUP BY sli.name_at_sale
            ORDER BY total_qty DESC
            LIMIT 3
        """),
        {"tid": tid},
    )
    top_items = [
        {"name": r[0], "quantity": r[1]}
        for r in top_items_row.fetchall()
    ]

    return {
        "connected": True,
        "merchant_id": conn.merchant_id,
        "merchant_name": merchant_name,
        "merchant_location": merchant_location,
        "menu_item_count": menu_count,
        "net_sales_30d_cents": net_sales_cents,
        "top_items": top_items,
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
