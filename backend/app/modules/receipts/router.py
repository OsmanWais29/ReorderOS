"""Receipts API (Sprint 6 S2) — canonical /api/v1/receipts surface.

Staff+ create/upload/read; Manager+ list/delete. Tenant scope comes from the
principal and is applied explicitly in every repo query. Upload is API-mediated
(D-606-14): bytes flow through the server, which validates + EXIF-strips before the
single Spaces PUT — there is no client presigned PUT.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.deps import get_rls_session
from app.core.security import Principal, require_role
from app.modules.receipts import repo, services
from app.modules.receipts.schemas import (
    DismissRequest,
    ReceiptCreate,
    ReceiptDetail,
    ReceiptListItem,
    UploadResponse,
)
from app.modules.receipts.validation import ReceiptValidationError

router = APIRouter(prefix="/receipts", tags=["receipts"])


@router.post("/uploads", response_model=UploadResponse, status_code=201)
async def upload_receipt(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("staff"),
) -> dict[str, Any]:
    raw = await file.read()
    try:
        result = await services.create_receipt_from_upload(
            db,
            tenant_id=UUID(principal.tenant_id),
            raw_bytes=raw,
            filename=file.filename,
            created_by=UUID(principal.user_id),
        )
    except ReceiptValidationError as exc:
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "message": exc.message}
        ) from None
    except storage.SpacesNotConfigured as exc:
        raise HTTPException(status_code=503, detail="Receipt storage is not configured") from exc
    return result


@router.post("", response_model=UploadResponse, status_code=201)
async def create_manual_receipt(
    body: ReceiptCreate,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("staff"),
) -> dict[str, Any]:
    """Create a manual draft (no photo) — the operator types the lines later."""
    receipt_id = await repo.create_draft(
        db,
        tenant_id=UUID(principal.tenant_id),
        source="manual",
        supplier_name=body.supplier_name,
        invoice_date=body.invoice_date,
        created_by=UUID(principal.user_id),
    )
    return {
        "receipt_id": receipt_id,
        "photo_object_key": "",
        "mime_type": "",
        "extraction_status": "none",
    }


@router.get("", response_model=list[ReceiptListItem])
async def list_receipts(
    commit_state: str | None = Query(default=None),
    extraction_status: str | None = Query(default=None),
    source: str | None = Query(default=None),
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("manager"),
) -> list[dict[str, Any]]:
    return await repo.list_receipts(
        db,
        UUID(principal.tenant_id),
        commit_state=commit_state,
        extraction_status=extraction_status,
        source=source,
    )


@router.get("/{receipt_id}", response_model=ReceiptDetail)
async def get_receipt(
    receipt_id: UUID,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("staff"),
) -> dict[str, Any]:
    receipt = await repo.get_receipt(db, UUID(principal.tenant_id), receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    # Signed, short-lived GET URL for the photo (download stays presigned, D-606-14).
    key = receipt.get("photo_object_key")
    receipt["photo_url"] = (
        storage.presigned_get_url(key, expires_in=timedelta(minutes=15))
        if key and storage.is_configured()
        else None
    )
    return receipt


@router.delete("/{receipt_id}", status_code=204)
async def delete_receipt(
    receipt_id: UUID,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("manager"),
) -> None:
    outcome = await repo.delete_draft(db, UUID(principal.tenant_id), receipt_id)
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Receipt not found")
    if outcome == "conflict":
        raise HTTPException(status_code=409, detail="Only a draft receipt can be deleted")


@router.post("/{receipt_id}/cancel", status_code=200)
async def cancel_receipt(
    receipt_id: UUID,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("staff"),
) -> dict[str, str]:
    outcome = await repo.set_commit_state(
        db, UUID(principal.tenant_id), receipt_id, new_state="cancelled"
    )
    _raise_for_state_outcome(outcome)
    return {"status": "cancelled"}


@router.post("/{receipt_id}/dismiss", status_code=200)
async def dismiss_receipt(
    receipt_id: UUID,
    body: DismissRequest,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("staff"),
) -> dict[str, str]:
    outcome = await repo.set_commit_state(
        db,
        UUID(principal.tenant_id),
        receipt_id,
        new_state="dismissed",
        dismissed_reason=body.reason,
    )
    _raise_for_state_outcome(outcome)
    return {"status": "dismissed"}


def _raise_for_state_outcome(outcome: str) -> None:
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Receipt not found")
    if outcome == "conflict":
        raise HTTPException(status_code=409, detail="Receipt is no longer a draft")
