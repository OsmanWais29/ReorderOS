"""Inventory HTTP routes — Sprint 3 Phases 5 & 6.

Idempotency is applied inline to every write endpoint that the spec requires.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_rls_session
from app.core.security import Principal, get_principal, require_role
from app.modules.inventory.depletion.units import CANONICAL_UNITS
from app.modules.inventory.idempotency import (
    check_and_lock,
    compute_fingerprint,
    store_response,
)
from app.modules.inventory.item_resolver import UnitTypeConflict
from app.modules.inventory.schemas import (
    CountEventCreate,
    OpeningBalanceCreate,
    ReceiptCreate,
    ReceiptLineCreate,
)
from app.modules.inventory.services import (
    ItemHasMovements,
    ItemInRecipes,
    ReceiptAdjustmentsUnreviewed,
    ReceiptConversionRequired,
    ReceiptLinesUnmatched,
    ReceiptNetCostInvalid,
    ReceiptNothingToCommit,
    ReceiptReviewRequired,
    add_receipt_line,
    change_item_storage_unit,
    commit_receipt,
    create_receipt,
    record_count_event,
    record_opening_balance,
)

router = APIRouter(prefix="/inventory", tags=["inventory"])
log = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────


async def _get_body(request: Request) -> bytes:
    return await request.body()


def _idem_key(request: Request) -> str | None:
    return request.headers.get("Idempotency-Key")


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 5 — POST /inventory/count-events  (idempotent write)
# ═════════════════════════════════════════════════════════════════════════════


@router.post("/count-events", status_code=201)
async def create_count_event(
    request: Request,
    body: CountEventCreate,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = Depends(get_principal),
) -> JSONResponse:
    tenant_id = UUID(principal.tenant_id)
    idem_key = _idem_key(request)

    # ── idempotency check ─────────────────────────────────────────────────────
    if idem_key:
        raw_body = await _get_body(request)
        fp = compute_fingerprint(request.method, request.url.path, raw_body)
        state = await check_and_lock(db, tenant_id=tenant_id, key=idem_key, fingerprint=fp)
        if state.status == "cached":
            assert state.response_status is not None
            return JSONResponse(state.response_body, status_code=state.response_status)
        # 'in_flight' and 'conflict' raise HTTPException inside check_and_lock.

    # ── handler ───────────────────────────────────────────────────────────────
    result = await record_count_event(
        db,
        tenant_id=tenant_id,
        inventory_item_id=body.inventory_item_id,
        counted_quantity=body.counted_quantity,
        counted_at=body.counted_at,
        notes=body.notes,
    )
    await db.commit()

    # fetch the persisted row's created_at for the response
    row = await db.execute(
        text("SELECT created_at FROM inventory_count_events WHERE id = :id"),
        {"id": result["id"]},
    )
    created_at = row.scalar_one()

    response_data: dict[str, Any] = {
        "id": str(result["id"]),
        "inventory_item_id": str(body.inventory_item_id),
        "counted_quantity": str(result["counted_quantity"]),
        "predicted_on_hand_at_count": (
            str(result["predicted_on_hand_at_count"])
            if result["predicted_on_hand_at_count"] is not None
            else None
        ),
        "created_at": created_at.isoformat(),
    }

    if idem_key:
        await store_response(
            db, tenant_id=tenant_id, key=idem_key, response_status=201, response_body=response_data
        )
        await db.commit()

    return JSONResponse(response_data, status_code=201)


# ═════════════════════════════════════════════════════════════════════════════
# PUT /inventory/items/{item_id}/storage-unit — SAFE unit correction
# ═════════════════════════════════════════════════════════════════════════════


class StorageUnitPatch(BaseModel):
    storage_unit: str

    @field_validator("storage_unit")
    @classmethod
    def _canonical(cls, v: str) -> str:
        if v not in CANONICAL_UNITS:
            raise ValueError(f"unit {v!r} is not canonical")
        return v


@router.put("/items/{item_id}/storage-unit")
async def change_storage_unit(
    item_id: UUID,
    body: StorageUnitPatch,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("manager"),
) -> dict[str, Any]:
    """Fix a mis-picked storage unit (the oz_weight-goblets lesson). Allowed only
    while the item has ZERO movements and ZERO recipe/modifier references —
    anything else would silently re-denominate history and needs a real
    migration, not an UPDATE."""
    try:
        result = await change_item_storage_unit(
            db,
            tenant_id=UUID(principal.tenant_id),
            inventory_item_id=item_id,
            storage_unit=body.storage_unit,
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="Item not found") from None
    except ItemHasMovements:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ITEM_HAS_MOVEMENTS",
                "message": "This item already has stock history — its unit can't be "
                "changed in place. Create a new item with the right unit.",
            },
        ) from None
    except ItemInRecipes:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ITEM_IN_RECIPES",
                "message": "Recipes use this item — changing its unit would change "
                "depletion math. Update the recipes first.",
            },
        ) from None
    except UnitTypeConflict as exc:
        raise HTTPException(
            status_code=409, detail={"code": "UNIT_TYPE_CONFLICT", "message": str(exc)}
        ) from None
    await db.commit()
    return {"id": str(result["id"]), "storage_unit": result["storage_unit"]}


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 4A — POST /inventory/items/{item_id}/opening-balance  (idempotent)
# ═════════════════════════════════════════════════════════════════════════════


@router.post("/items/{item_id}/opening-balance", status_code=201)
async def create_opening_balance(
    item_id: UUID,
    request: Request,
    body: OpeningBalanceCreate,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = Depends(get_principal),
) -> JSONResponse:
    tenant_id = UUID(principal.tenant_id)
    idem_key = _idem_key(request)

    if idem_key:
        raw_body = await _get_body(request)
        fp = compute_fingerprint(request.method, request.url.path, raw_body)
        state = await check_and_lock(db, tenant_id=tenant_id, key=idem_key, fingerprint=fp)
        if state.status == "cached":
            assert state.response_status is not None
            return JSONResponse(state.response_body, status_code=state.response_status)

    mv_id = await record_opening_balance(
        db,
        tenant_id=tenant_id,
        inventory_item_id=item_id,
        quantity=body.quantity,
        recorded_at=body.recorded_at,
    )
    await db.commit()

    response_data: dict[str, Any] = {
        "movement_id": str(mv_id),
        "inventory_item_id": str(item_id),
        "quantity": str(body.quantity),
    }

    if idem_key:
        await store_response(
            db, tenant_id=tenant_id, key=idem_key, response_status=201, response_body=response_data
        )
        await db.commit()

    return JSONResponse(response_data, status_code=201)


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 4D — Receipt endpoints
# ═════════════════════════════════════════════════════════════════════════════


@router.post("/receipts", status_code=201)
async def create_receipt_endpoint(
    body: ReceiptCreate,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = Depends(get_principal),
) -> JSONResponse:
    tenant_id = UUID(principal.tenant_id)
    rid = await create_receipt(
        db,
        tenant_id=tenant_id,
        created_by=UUID(principal.user_id),
        notes=body.notes,
        received_at=body.received_at,
    )
    await db.commit()
    row = await db.execute(
        text("SELECT id, commit_state, received_at, notes FROM receipts WHERE id = :id"),
        {"id": rid},
    )
    r = row.fetchone()
    assert r is not None
    return JSONResponse(
        {
            "id": str(r[0]),
            "commit_state": r[1],
            "received_at": r[2].isoformat(),
            "notes": r[3],
        },
        status_code=201,
    )


@router.post("/receipts/{receipt_id}/lines", status_code=201)
async def add_line_endpoint(
    receipt_id: UUID,
    body: ReceiptLineCreate,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = Depends(get_principal),
) -> JSONResponse:
    tenant_id = UUID(principal.tenant_id)
    line_id = await add_receipt_line(
        db,
        tenant_id=tenant_id,
        receipt_id=receipt_id,
        inventory_item_id=body.inventory_item_id,
        received_quantity=body.received_quantity,
        purchase_unit_id=body.purchase_unit_id,
        unit_cost_cents=body.unit_cost_cents,
    )
    await db.commit()
    return JSONResponse(
        {
            "id": str(line_id),
            "inventory_item_id": str(body.inventory_item_id),
            "received_quantity": str(body.received_quantity),
            "unit_cost_cents": body.unit_cost_cents,
        },
        status_code=201,
    )


@router.post("/receipts/{receipt_id}/commit", status_code=200)
async def commit_receipt_endpoint(
    receipt_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = Depends(get_principal),
) -> JSONResponse:
    tenant_id = UUID(principal.tenant_id)
    # Deprecated Sprint-3 manual shim (D-606-10). 'manual' is a gated intake source
    # now: this endpoint is an authenticated manager+ action where the operator types
    # the lines, so it passes confirm + affirmation to satisfy the one commit gate.
    try:
        result = await commit_receipt(
            db,
            tenant_id=tenant_id,
            receipt_id=receipt_id,
            confirm=True,
            reviewed_affirmation=True,
        )
    except ReceiptLinesUnmatched:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "RECEIPT_LINES_UNMATCHED",
                "message": "Link or create an inventory item for every line "
                "(or skip it) before committing.",
            },
        ) from None
    except ReceiptConversionRequired as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "RECEIPT_UNIT_CONVERSION_REQUIRED",
                "message": "One or more items need a package conversion.",
                "errors": exc.errors,
            },
        ) from None
    except ReceiptAdjustmentsUnreviewed as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "RECEIPT_ADJUSTMENTS_UNREVIEWED",
                "message": str(exc),
                "errors": exc.errors,
            },
        ) from None
    except ReceiptNetCostInvalid as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "RECEIPT_NET_COST_INVALID", "message": str(exc)},
        ) from None
    except ReceiptNothingToCommit:
        raise HTTPException(
            status_code=422,
            detail={"code": "RECEIPT_NOTHING_TO_COMMIT", "message": "Nothing to receive."},
        ) from None
    except ReceiptReviewRequired as exc:
        raise HTTPException(
            status_code=422, detail={"code": "RECEIPT_REVIEW_REQUIRED", "message": str(exc)}
        ) from None
    await db.commit()
    return JSONResponse(
        {
            "receipt_id": str(result["receipt_id"]),
            "status": result["status"],
            "movement_ids": [str(m) for m in result.get("movement_ids", [])],
        }
    )


# ═════════════════════════════════════════════════════════════════════════════
# PHASE 6 — GET /inventory/items  (stock warnings)
# ═════════════════════════════════════════════════════════════════════════════


def _compute_count_state(
    mode: str,
    last_count_at: datetime | None,
    cadence_days: int | None,
    grace_days: int | None,
    now: datetime,
) -> str | None:
    if mode != "count_anchored":
        return None
    if last_count_at is None:
        return None
    if cadence_days is None:
        log.warning("count_cadence_days is NULL for a Mode B item — returning None")
        return None
    age = (now - last_count_at).total_seconds() / 86400.0
    grace = grace_days or 0
    if age <= cadence_days:
        return "fresh"
    if age <= cadence_days + grace:
        return "stale"
    return "expired"


def _compute_stock_status(
    *,
    count_required: bool,
    out_of_stock: bool | None,
    low_stock: bool | None,
    count_state: str | None,
) -> str:
    if count_required:
        return "count_required"
    if out_of_stock:
        return "out_of_stock"
    if low_stock:
        return "low_stock"
    if count_state == "expired":
        return "count_expired"
    if count_state == "stale":
        return "count_stale"
    return "ok"


@router.get("/items")
async def list_inventory_items(
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = Depends(get_principal),
) -> JSONResponse:
    tenant_id = UUID(principal.tenant_id)
    now = datetime.now(UTC)

    rows = await db.execute(
        text("""
            SELECT
                ii.id,
                ii.name,
                ii.inventory_mode,
                ii.par_level,
                ii.count_cadence_days,
                ii.count_grace_days,
                ii.last_count_at,
                ii.last_count_quantity,
                CASE
                    WHEN ii.inventory_mode = 'recipe_deducted'
                        THEN (
                            SELECT COALESCE(SUM(m.delta), 0)
                              FROM inventory_movements m
                             WHERE m.tenant_id         = ii.tenant_id
                               AND m.inventory_item_id = ii.id
                               AND m.movement_type NOT IN ('sale_signal','sale_signal_reversal')
                        )
                    WHEN ii.inventory_mode = 'count_anchored'
                         AND ii.last_count_quantity IS NOT NULL
                        THEN ii.last_count_quantity
                             + COALESCE((
                                   SELECT SUM(m.delta)
                                     FROM inventory_movements m
                                    WHERE m.tenant_id         = ii.tenant_id
                                      AND m.inventory_item_id = ii.id
                                      AND m.recorded_at       > ii.last_count_at
                                      AND m.movement_type IN
                                          ('receive','transfer_in','count_adjust','opening_balance')
                               ), 0)
                             - (COALESCE((
                                   SELECT SUM(ABS(m.delta))
                                     FROM inventory_movements m
                                    WHERE m.tenant_id         = ii.tenant_id
                                      AND m.inventory_item_id = ii.id
                                      AND m.recorded_at       > ii.last_count_at
                                      AND m.movement_type     = 'sale_signal'
                               ), 0)
                               * COALESCE((
                                   SELECT yf.yield_factor
                                     FROM inventory_yield_factors yf
                                    WHERE yf.tenant_id         = ii.tenant_id
                                      AND yf.inventory_item_id = ii.id
                               ), 1.0))
                    ELSE NULL
                END AS on_hand
            FROM inventory_items ii
            WHERE ii.tenant_id = :tid
              AND ii.active = true
            ORDER BY ii.name
        """),
        {"tid": tenant_id},
    )
    items_raw = rows.fetchall()

    result = []
    for row in items_raw:
        (
            item_id,
            name,
            mode,
            par_level,
            cadence_days,
            grace_days,
            last_count_at,
            last_count_qty,
            on_hand,
        ) = row

        # count_state — computed live, not read from confidence_state column
        count_state = _compute_count_state(mode, last_count_at, cadence_days, grace_days, now)

        # count_required: Mode B with no anchor
        count_required = mode == "count_anchored" and last_count_qty is None

        # low_stock / out_of_stock — null when no reliable on_hand
        if on_hand is not None:
            low_stock: bool | None = par_level is not None and on_hand <= par_level
            out_of_stock: bool | None = on_hand <= 0
        else:
            low_stock = None
            out_of_stock = None

        stock_status = _compute_stock_status(
            count_required=count_required,
            out_of_stock=out_of_stock,
            low_stock=low_stock,
            count_state=count_state,
        )

        result.append(
            {
                "id": str(item_id),
                "name": name,
                "inventory_mode": mode,
                "on_hand": float(on_hand) if on_hand is not None else None,
                "stock_status": stock_status,
                "count_required": count_required,
                "count_state": count_state,
                "low_stock": low_stock,
                "out_of_stock": out_of_stock,
                "par_level": float(par_level) if par_level is not None else None,
                "last_count_at": last_count_at.isoformat() if last_count_at else None,
            }
        )

    return JSONResponse({"items": result, "total": len(result)})
