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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.deps import get_rls_session
from app.core.logging import get_logger
from app.core.security import Principal, require_role
from app.modules.inventory.depletion.conversions import ConversionError
from app.modules.inventory.item_resolver import UnitTypeConflict
from app.modules.inventory.services import (
    ReceiptAdjustmentsUnreviewed,
    ReceiptConversionRequired,
    ReceiptLinesUnmatched,
    ReceiptNetCostInvalid,
    ReceiptNothingToCommit,
    ReceiptReviewRequired,
    commit_receipt,
)
from app.modules.receipts import repo, services
from app.modules.receipts.schemas import (
    AdjustRequest,
    CommitRequest,
    DismissRequest,
    LineCreate,
    LineUpdate,
    NoteCreate,
    NoteOut,
    ReceiptCreate,
    ReceiptDetail,
    ReceiptLineOut,
    ReceiptListItem,
    ResetExtractionRequest,
    UploadResponse,
)
from app.modules.receipts.validation import ReceiptValidationError

log = get_logger(__name__)

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
    await db.commit()
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
    await db.commit()
    return {
        "receipt_id": receipt_id,
        "photo_object_key": "",
        "mime_type": "",
        "extraction_status": "none",
    }


@router.post("/{receipt_id}/extract", status_code=202)
async def extract_receipt(
    receipt_id: UUID,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("staff"),
) -> dict[str, Any]:
    """Enqueue extraction (idempotent). 202 — the worker processes it asynchronously.
    409 if a human has already begun review (use reset-extraction instead)."""
    try:
        result = await services.enqueue_extraction(
            db, tenant_id=UUID(principal.tenant_id), receipt_id=receipt_id
        )
    except services.ReceiptNotFound:
        raise HTTPException(status_code=404, detail="Receipt not found") from None
    except services.ReviewInProgress:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "RECEIPT_REVIEW_IN_PROGRESS",
                "message": "Review already started; use reset-extraction to start over.",
            },
        ) from None
    await db.commit()
    return result


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


@router.put("/{receipt_id}/lines/{line_id}", response_model=ReceiptLineOut)
async def update_receipt_line(
    receipt_id: UUID,
    line_id: UUID,
    body: LineUpdate,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("staff"),
) -> dict[str, Any]:
    """Review-edit one line (D-606-25/26): link/create/clear the item, fix
    qty/unit/price/name, or skip/unskip. Every variant sets review_started_at (first
    active edit) and clears reviewed_affirmation."""
    try:
        result = await services.update_line(
            db,
            tenant_id=UUID(principal.tenant_id),
            receipt_id=receipt_id,
            line_id=line_id,
            patch=body,
            confirmed_by=UUID(principal.user_id),
        )
    except services.ReceiptNotFound:
        raise HTTPException(status_code=404, detail="Receipt not found") from None
    except services.LineNotFound:
        raise HTTPException(status_code=404, detail="Line not found") from None
    except services.LineNotLinked:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "RECEIPT_LINE_NOT_LINKED",
                "message": "Link an inventory item before confirming its conversion.",
            },
        ) from None
    except services.AdjustmentLinkInvalid as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "RECEIPT_ADJUSTMENT_LINK_INVALID", "message": str(exc)},
        ) from None
    except services.LineConversionInconsistent:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "RECEIPT_CONVERSION_INCONSISTENT",
                "message": "Received quantity must equal invoice quantity x pack "
                "conversion. Check the numbers and try again.",
            },
        ) from None
    except services.LineUnitMismatch:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "RECEIPT_UNIT_MISMATCH",
                "message": "The invoice packaging and this item's storage unit are "
                "different types. Check the item, or confirm the override to proceed.",
            },
        ) from None
    except services.ReceiptImmutable:
        raise HTTPException(
            status_code=409,
            detail={"code": "RECEIPT_NOT_EDITABLE", "message": "Receipt is no longer editable."},
        ) from None
    except services.UnknownInventoryItem:
        raise HTTPException(
            status_code=422,
            detail={"code": "RECEIPT_UNKNOWN_ITEM", "message": "No such active inventory item."},
        ) from None
    except UnitTypeConflict as exc:
        raise HTTPException(
            status_code=409, detail={"code": "UNIT_TYPE_CONFLICT", "message": str(exc)}
        ) from None
    await db.commit()
    return result


@router.post("/{receipt_id}/lines", response_model=ReceiptLineOut, status_code=201)
async def add_receipt_line(
    receipt_id: UUID,
    body: LineCreate,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("staff"),
) -> dict[str, Any]:
    """Add an operator line. Sets review_started_at / clears reviewed_affirmation
    (D-606-25) like any other line mutation."""
    try:
        result = await services.add_line(
            db, tenant_id=UUID(principal.tenant_id), receipt_id=receipt_id, body=body
        )
    except services.ReceiptNotFound:
        raise HTTPException(status_code=404, detail="Receipt not found") from None
    except services.ReceiptImmutable:
        raise HTTPException(
            status_code=409,
            detail={"code": "RECEIPT_NOT_EDITABLE", "message": "Receipt is no longer editable."},
        ) from None
    except services.UnknownInventoryItem:
        raise HTTPException(
            status_code=422,
            detail={"code": "RECEIPT_UNKNOWN_ITEM", "message": "No such active inventory item."},
        ) from None
    await db.commit()
    return result


@router.post("/{receipt_id}/reset-extraction", status_code=202)
async def reset_receipt_extraction(
    receipt_id: UUID,
    body: ResetExtractionRequest,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("staff"),
) -> dict[str, Any]:
    """Destructive start-over-from-the-machine: requires {discard_edits: true} or
    409s — operator work is never discarded implicitly. Notes are preserved."""
    try:
        result = await services.reset_extraction(
            db,
            tenant_id=UUID(principal.tenant_id),
            receipt_id=receipt_id,
            discard_edits=body.discard_edits,
        )
    except services.ResetNeedsConfirm:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "RECEIPT_RESET_NEEDS_CONFIRM",
                "message": "Resetting discards all lines and edits; pass discard_edits=true.",
            },
        ) from None
    except services.ReceiptNotFound:
        raise HTTPException(status_code=404, detail="Receipt not found") from None
    except services.ReceiptImmutable:
        raise HTTPException(
            status_code=409,
            detail={"code": "RECEIPT_NOT_EDITABLE", "message": "Receipt is no longer editable."},
        ) from None
    await db.commit()
    return result


@router.post("/{receipt_id}/notes", response_model=NoteOut, status_code=201)
async def add_receipt_note(
    receipt_id: UUID,
    body: NoteCreate,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("staff"),
) -> dict[str, Any]:
    """Append to the notes_log audit trail (any commit_state)."""
    try:
        note = await services.append_note(
            db,
            tenant_id=UUID(principal.tenant_id),
            receipt_id=receipt_id,
            body=body,
            user_id=UUID(principal.user_id),
        )
    except services.ReceiptNotFound:
        raise HTTPException(status_code=404, detail="Receipt not found") from None
    await db.commit()
    return note


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
    await db.commit()


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
    await db.commit()
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
    await db.commit()
    return {"status": "dismissed"}


@router.post("/{receipt_id}/commit", status_code=200)
async def commit_receipt_endpoint(
    receipt_id: UUID,
    body: CommitRequest,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("manager"),
) -> dict[str, Any]:
    """Commit a reviewed receipt into the ledger (the one commit path). The server
    stamps confirmed_at (D-606-04); the body's confirm/affirmation satisfy the gate."""
    tenant_id = UUID(principal.tenant_id)
    # Affirm-at-commit counts as active engagement → set review_started_at if NULL
    # BEFORE flipping, so a racing extraction is correctly superseded (D-606-22 #8).
    await db.execute(
        text(
            "UPDATE receipts SET review_started_at = COALESCE(review_started_at, now()) "
            "WHERE id = :r AND tenant_id = :t"
        ),
        {"r": receipt_id, "t": tenant_id},
    )
    try:
        result = await commit_receipt(
            db,
            tenant_id=tenant_id,
            receipt_id=receipt_id,
            confirm=body.confirm,
            reviewed_affirmation=body.reviewed_affirmation,
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="Receipt not found") from None
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
                # Structured per-line blockers: machine-readable ids + tenant-safe
                # names + package evidence + safe suggestion. ALL failing lines in
                # one response — the client marks each and jumps to the first.
                "errors": exc.errors,
            },
        ) from None
    except ReceiptAdjustmentsUnreviewed as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "RECEIPT_ADJUSTMENTS_UNREVIEWED",
                "message": str(exc),
                # Per-adjustment blockers: id + invoice text + signed amount +
                # a suggested target only when unambiguous. Zero writes happened.
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
    except ConversionError as exc:
        # Last-resort net — the pre-validation gate above should make this
        # unreachable. NEVER echo str(exc): it embeds unit names, item and
        # tenant UUIDs (the live-cert leak). Log it, return a generic message.
        log.error("receipt.commit.conversion_error", error_class=type(exc).__name__)
        raise HTTPException(
            status_code=422,
            detail={
                "code": "RECEIPT_UNIT_CONVERSION_REQUIRED",
                "message": "One or more items need a package conversion.",
                "errors": [],
            },
        ) from None
    await db.commit()
    return {
        "receipt_id": str(result["receipt_id"]),
        "status": result["status"],
        "movement_ids": [str(m) for m in result.get("movement_ids", [])],
        "confirmed": result.get("confirmed", False),
    }


@router.post("/{receipt_id}/adjust", status_code=201)
async def adjust_receipt(
    receipt_id: UUID,
    body: AdjustRequest,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("manager"),
) -> dict[str, Any]:
    """Post-commit correction (Phase 5) — append-only; writes a compensating movement,
    never mutates the committed receipt. 409 if the receipt isn't committed yet."""
    tenant_id = UUID(principal.tenant_id)
    try:
        result = await services.create_adjustment(
            db,
            tenant_id=tenant_id,
            receipt_id=receipt_id,
            adjustment_type=body.adjustment_type,
            inventory_item_id=body.inventory_item_id,
            delta_quantity=body.delta_quantity,
            delta_unit=body.delta_unit,
            reason=body.reason,
            receipt_line_id=body.receipt_line_id,
            delta_cost_cents=body.delta_cost_cents,
            created_by=UUID(principal.user_id),
        )
    except services.ReceiptNotFound:
        raise HTTPException(status_code=404, detail="Receipt not found") from None
    except services.ReceiptNotCommitted:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "RECEIPT_NOT_COMMITTED",
                "message": "Adjustments apply only to a committed receipt.",
            },
        ) from None
    await db.commit()
    return {
        "adjustment_id": str(result["adjustment_id"]),
        "compensating_movement_id": str(result["compensating_movement_id"]),
        "movement_type": result["movement_type"],
    }


def _raise_for_state_outcome(outcome: str) -> None:
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Receipt not found")
    if outcome == "conflict":
        raise HTTPException(status_code=409, detail="Receipt is no longer a draft")
