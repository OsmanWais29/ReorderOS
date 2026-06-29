"""Receipt service orchestration (Sprint 6 S2).

The API-mediated upload path (D-606-14): the server receives the photo bytes,
validates + EXIF-strips them (validation.validate_and_clean), writes the CLEANED
object to Spaces itself, then creates the draft. Orphan-safe: if the draft INSERT
fails after the PUT, the just-written object is deleted.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.modules.receipts import repo
from app.modules.receipts.validation import extension_for, validate_and_clean


class ReceiptNotFound(Exception):
    """No receipt with that id for this tenant (→ 404)."""


class ReviewInProgress(Exception):
    """Extraction re-trigger blocked because a human has begun review
    (review_started_at set) — a fresh result would be discarded as superseded.
    Use reset-extraction to discard edits and start over (→ 409)."""


class ReceiptNotCommitted(Exception):
    """A post-commit adjustment was attempted on a non-committed receipt (→ 409)."""


# adjustment_type → the compensating movement_type (inventory_accounting_semantics §5:
# corrections are compensating entries, never mutations of the original movement).
_MOVEMENT_TYPE_FOR_ADJUSTMENT = {
    "correction": "adjustment",
    "count_fix": "count_adjust",
    "return": "waste",
    "damage": "waste",
}


def build_photo_key(tenant_id: UUID, receipt_id: UUID, mime_type: str) -> str:
    """Tenant-scoped, non-guessable object key (D-606-24 shape for the photo path)."""
    return f"receipts/{tenant_id}/{receipt_id}/{uuid4()}.{extension_for(mime_type)}"


async def create_receipt_from_upload(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    raw_bytes: bytes,
    filename: str | None,
    created_by: UUID,
) -> dict[str, Any]:
    """Validate + clean the uploaded bytes, store the cleaned object, create the
    draft. Raises ReceiptValidationError (terminal) before any storage write; raises
    SpacesNotConfigured if Spaces isn't set up. Commits nothing — the caller owns the
    transaction (the FastAPI session dependency commits on success)."""
    mime_type, cleaned = validate_and_clean(raw_bytes, filename=filename)

    receipt_id = uuid4()
    key = build_photo_key(tenant_id, receipt_id, mime_type)

    # PUT the cleaned bytes first, then INSERT; on INSERT failure remove the object
    # so a failed draft never leaves an orphan in Spaces.
    storage.put_bytes(key, cleaned, content_type=mime_type)
    try:
        await repo.create_draft(
            db,
            tenant_id=tenant_id,
            source="mobile_photo",
            receipt_id=receipt_id,
            photo_object_key=key,
            original_filename=filename,
            mime_type=mime_type,
            file_size_bytes=len(cleaned),
            created_by=created_by,
        )
    except Exception:
        # Best-effort orphan cleanup; the original error re-raises.
        with contextlib.suppress(Exception):
            storage.delete_object(key)
        raise

    return {
        "receipt_id": receipt_id,
        "photo_object_key": key,
        "mime_type": mime_type,
        "extraction_status": "none",
    }


async def enqueue_extraction(
    db: AsyncSession, *, tenant_id: UUID, receipt_id: UUID
) -> dict[str, Any]:
    """Enqueue an extraction job for a draft receipt (idempotent re-trigger). Raises
    ReceiptNotFound (404) / ReviewInProgress (409). Caller commits.

    Re-extraction is supported but only BEFORE a human edits: once review_started_at
    is set, the worker would discard any result as superseded, so we reject here and
    point the operator at reset-extraction. Prior MACHINE lines (extraction_job_id
    set) are cleared so a re-run doesn't accumulate duplicates; operator-added lines
    (extraction_job_id NULL) are preserved."""
    row = (
        await db.execute(
            text(
                "SELECT review_started_at FROM receipts "
                "WHERE id = :rid AND tenant_id = :tid"
            ),
            {"rid": receipt_id, "tid": tenant_id},
        )
    ).mappings().fetchone()
    if row is None:
        raise ReceiptNotFound
    if row["review_started_at"] is not None:
        raise ReviewInProgress

    # Clear stale machine lines from a prior extraction (preserve operator lines).
    await db.execute(
        text(
            "DELETE FROM receipt_lines "
            "WHERE tenant_id = :tid AND receipt_id = :rid AND extraction_job_id IS NOT NULL"
        ),
        {"tid": tenant_id, "rid": receipt_id},
    )

    next_attempt = (
        await db.execute(
            text(
                "SELECT COALESCE(MAX(job_attempt), 0) + 1 FROM receipt_extraction_jobs "
                "WHERE tenant_id = :tid AND receipt_id = :rid"
            ),
            {"tid": tenant_id, "rid": receipt_id},
        )
    ).scalar_one()

    job_id = (
        await db.execute(
            text("""
                INSERT INTO receipt_extraction_jobs (tenant_id, receipt_id, job_attempt, status)
                VALUES (:tid, :rid, :att, 'pending')
                RETURNING id
            """),
            {"tid": tenant_id, "rid": receipt_id, "att": next_attempt},
        )
    ).scalar_one()

    await db.execute(
        text(
            "UPDATE receipts SET extraction_status = 'pending', quota_blocked = false, "
            "updated_at = now() WHERE id = :rid AND tenant_id = :tid"
        ),
        {"rid": receipt_id, "tid": tenant_id},
    )
    return {"job_id": job_id, "status": "pending"}


async def create_adjustment(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    receipt_id: UUID,
    adjustment_type: str,
    inventory_item_id: UUID,
    delta_quantity: Decimal,
    delta_unit: str,
    reason: str | None,
    receipt_line_id: UUID | None,
    delta_cost_cents: int | None,
    created_by: UUID,
) -> dict[str, Any]:
    """Record a post-commit correction (Phase 5) — APPEND-ONLY. Inserts a
    receipt_adjustments row plus a compensating inventory_movement, linked via
    compensating_movement_id; NEVER mutates the original committed lines/movements
    (inventory_accounting_semantics §5). Raises ReceiptNotFound (404) /
    ReceiptNotCommitted (409). delta_quantity is signed and already in storage units.

    Both ids are generated up front so the movement (source_id = the adjustment) and
    the adjustment row (compensating_movement_id = the movement) reference each other
    in one direction each — no post-insert UPDATE, which the append-only grant forbids.
    """
    state = (
        await db.execute(
            text("SELECT commit_state FROM receipts WHERE id = :r AND tenant_id = :t"),
            {"r": receipt_id, "t": tenant_id},
        )
    ).scalar()
    if state is None:
        raise ReceiptNotFound
    if state != "committed":
        raise ReceiptNotCommitted

    movement_type = _MOVEMENT_TYPE_FOR_ADJUSTMENT[adjustment_type]
    adjustment_id = uuid4()
    movement_id = uuid4()

    await db.execute(
        text("""
            INSERT INTO inventory_movements
                (id, tenant_id, inventory_item_id, movement_type, delta,
                 source_type, source_id, idempotency_key, notes)
            VALUES (:id, :tid, :iid, :mt, :delta,
                    'receipt_adjustment', :aid, :key, :reason)
        """),
        {
            "id": movement_id,
            "tid": tenant_id,
            "iid": inventory_item_id,
            "mt": movement_type,
            "delta": delta_quantity,
            "aid": adjustment_id,
            "key": f"receipt_adjustment:{adjustment_id}",
            "reason": reason,
        },
    )
    await db.execute(
        text("""
            INSERT INTO receipt_adjustments
                (id, tenant_id, receipt_id, receipt_line_id, inventory_item_id,
                 adjustment_type, delta_quantity, delta_unit, reason, delta_cost_cents,
                 compensating_movement_id, created_by)
            VALUES (:id, :tid, :rid, :lid, :iid,
                    :atype, :dq, :du, :reason, :cost,
                    :mid, :by)
        """),
        {
            "id": adjustment_id,
            "tid": tenant_id,
            "rid": receipt_id,
            "lid": receipt_line_id,
            "iid": inventory_item_id,
            "atype": adjustment_type,
            "dq": delta_quantity,
            "du": delta_unit,
            "reason": reason,
            "cost": delta_cost_cents,
            "mid": movement_id,
            "by": created_by,
        },
    )
    return {
        "adjustment_id": adjustment_id,
        "compensating_movement_id": movement_id,
        "movement_type": movement_type,
    }
