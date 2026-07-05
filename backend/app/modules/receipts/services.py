"""Receipt service orchestration (Sprint 6 S2).

The API-mediated upload path (D-606-14): the server receives the photo bytes,
validates + EXIF-strips them (validation.validate_and_clean), writes the CLEANED
object to Spaces itself, then creates the draft. Orphan-safe: if the draft INSERT
fails after the PUT, the just-written object is deleted.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.modules.inventory.item_resolver import resolve_inventory_item
from app.modules.receipts import repo
from app.modules.receipts.schemas import LineCreate, LineUpdate, NoteCreate
from app.modules.receipts.validation import extension_for, validate_and_clean


class ReceiptNotFound(Exception):
    """No receipt with that id for this tenant (→ 404)."""


class ReviewInProgress(Exception):
    """Extraction re-trigger blocked because a human has begun review
    (review_started_at set) — a fresh result would be discarded as superseded.
    Use reset-extraction to discard edits and start over (→ 409)."""


class ReceiptNotCommitted(Exception):
    """A post-commit adjustment was attempted on a non-committed receipt (→ 409)."""


class ReceiptImmutable(Exception):
    """A review mutation was attempted on a receipt that is no longer editable
    (committed/dismissed/cancelled → 409)."""


class LineNotFound(Exception):
    """No line with that id on this receipt for this tenant (→ 404)."""


class UnknownInventoryItem(Exception):
    """inventory_item_id does not resolve to an active item for this tenant (→ 422)."""


class ResetNeedsConfirm(Exception):
    """reset-extraction called without discard_edits=true — destructive action
    requires the explicit flag (→ 409 RECEIPT_RESET_NEEDS_CONFIRM)."""


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
        (
            await db.execute(
                text("SELECT review_started_at FROM receipts WHERE id = :rid AND tenant_id = :tid"),
                {"rid": receipt_id, "tid": tenant_id},
            )
        )
        .mappings()
        .fetchone()
    )
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


# commit_states in which a receipt's lines are still editable.
_EDITABLE_STATES = ("draft", "pending_review")


async def _lock_editable_receipt(db: AsyncSession, tenant_id: UUID, receipt_id: UUID) -> None:
    """FOR UPDATE the receipt and assert it is still review-editable. Serializes
    concurrent line mutations against each other AND against commit (which also
    takes FOR UPDATE), so the D-606-22/25 freshness rules see a stable state."""
    state = (
        await db.execute(
            text(
                "SELECT commit_state FROM receipts WHERE tenant_id = :tid AND id = :rid FOR UPDATE"
            ),
            {"tid": tenant_id, "rid": receipt_id},
        )
    ).scalar()
    if state is None:
        raise ReceiptNotFound
    if state not in _EDITABLE_STATES:
        raise ReceiptImmutable


async def _touch_review(db: AsyncSession, tenant_id: UUID, receipt_id: UUID) -> None:
    """The D-606-25 side-effect of EVERY line mutation: review_started_at is set on
    the first active edit (never on open/poll — the worker's hard-stop keys on it),
    and reviewed_affirmation is cleared so the D-606-22 guard must be re-satisfied
    against the post-edit line set."""
    await db.execute(
        text("""
            UPDATE receipts
               SET review_started_at = COALESCE(review_started_at, now()),
                   reviewed_affirmation = false,
                   updated_at = now()
             WHERE tenant_id = :tid AND id = :rid
        """),
        {"tid": tenant_id, "rid": receipt_id},
    )


async def _assert_active_item(db: AsyncSession, tenant_id: UUID, item_id: UUID) -> None:
    found = (
        await db.execute(
            text(
                "SELECT 1 FROM inventory_items "
                "WHERE tenant_id = :tid AND id = :iid AND active = true"
            ),
            {"tid": tenant_id, "iid": item_id},
        )
    ).scalar()
    if found is None:
        raise UnknownInventoryItem


async def update_line(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    receipt_id: UUID,
    line_id: UUID,
    patch: LineUpdate,
) -> dict[str, Any]:
    """Edit one line per the D-606-25/26 lifecycle (schema-validated combinations):

    - link existing item  → match_status='matched',  manually_corrected=true
    - create-and-link     → match_status='created',  manually_corrected=true
      (shared resolver — same race-safe path recipe confirm uses; UnitTypeConflict
      propagates to a 409)
    - clear item (null)   → match_status='unmatched', manually_corrected=false
    - qty/unit/price/name → manually_corrected=true
    - skipped=true        → match_status='skipped' (item/corrected untouched);
      skipped=false       → 'matched' if an item is set else 'unmatched'
    Every variant runs _touch_review (D-606-25). Returns the updated line row."""
    await _lock_editable_receipt(db, tenant_id, receipt_id)

    line = (
        (
            await db.execute(
                text("""
                    SELECT id, inventory_item_id, match_status
                      FROM receipt_lines
                     WHERE tenant_id = :tid AND receipt_id = :rid AND id = :lid
                     FOR UPDATE
                """),
                {"tid": tenant_id, "rid": receipt_id, "lid": line_id},
            )
        )
        .mappings()
        .fetchone()
    )
    if line is None:
        raise LineNotFound

    sets: list[str] = []
    params: dict[str, Any] = {"tid": tenant_id, "rid": receipt_id, "lid": line_id}
    fields = patch.model_fields_set

    if "skipped" in fields:
        if patch.skipped:
            sets.append("match_status = 'skipped'")
        else:
            restored = "matched" if line["inventory_item_id"] is not None else "unmatched"
            sets.append("match_status = :ms")
            params["ms"] = restored
    elif "inventory_item_id" in fields:
        if patch.inventory_item_id is None:
            # D-606-26: revert to machine state — never a matched row with NULL item.
            sets.append("inventory_item_id = NULL")
            sets.append("match_status = 'unmatched'")
            sets.append("manually_corrected = false")
        else:
            await _assert_active_item(db, tenant_id, patch.inventory_item_id)
            sets.append("inventory_item_id = :iid")
            sets.append("match_status = 'matched'")
            sets.append("manually_corrected = true")
            params["iid"] = patch.inventory_item_id
    elif patch.new_item_name is not None:
        assert patch.new_item_unit is not None  # schema-enforced
        item_id = await resolve_inventory_item(
            db, tenant_id, patch.new_item_name, patch.new_item_unit
        )
        sets.append("inventory_item_id = :iid")
        sets.append("match_status = 'created'")
        sets.append("manually_corrected = true")
        params["iid"] = item_id

    field_edits = fields & {
        "received_quantity",
        "extracted_unit",
        "unit_cost_cents",
        "extracted_name",
    }
    for col in field_edits:
        sets.append(f"{col} = :{col}")
        params[col] = getattr(patch, col)
    if field_edits:
        sets.append("manually_corrected = true")

    await db.execute(
        text(f"""
            UPDATE receipt_lines SET {", ".join(sets)}
             WHERE tenant_id = :tid AND receipt_id = :rid AND id = :lid
        """),  # noqa: S608 — `sets` is built ONLY from hardcoded column literals above
        params,
    )
    await _touch_review(db, tenant_id, receipt_id)

    updated = (
        (
            await db.execute(
                text("""
                    SELECT id, extracted_name, inventory_item_id, received_quantity,
                           extracted_unit, unit_cost_cents, confidence,
                           manually_corrected, match_status, line_ordinal
                      FROM receipt_lines
                     WHERE tenant_id = :tid AND receipt_id = :rid AND id = :lid
                """),
                {"tid": tenant_id, "rid": receipt_id, "lid": line_id},
            )
        )
        .mappings()
        .one()
    )
    return dict(updated)


async def add_line(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    receipt_id: UUID,
    body: LineCreate,
) -> dict[str, Any]:
    """Append an operator line (extraction_job_id NULL — never collides with the
    machine lines' job-keyed unique index). Ordinal continues after the existing
    lines so display order is stable. D-606-26: 'matched' + manually_corrected only
    when an item is linked at creation; otherwise 'unmatched' until a later PUT."""
    await _lock_editable_receipt(db, tenant_id, receipt_id)

    if body.inventory_item_id is not None:
        await _assert_active_item(db, tenant_id, body.inventory_item_id)

    next_ordinal = (
        await db.execute(
            text(
                "SELECT COALESCE(MAX(line_ordinal), -1) + 1 FROM receipt_lines "
                "WHERE tenant_id = :tid AND receipt_id = :rid"
            ),
            {"tid": tenant_id, "rid": receipt_id},
        )
    ).scalar_one()

    line_id = uuid4()
    await db.execute(
        text("""
            INSERT INTO receipt_lines
                (id, tenant_id, receipt_id, inventory_item_id, extracted_name,
                 received_quantity, extracted_unit, unit_cost_cents,
                 match_status, manually_corrected, line_ordinal)
            VALUES
                (:id, :tid, :rid, :iid, :name,
                 :qty, :unit, :cost,
                 :ms, :mc, :ord)
        """),
        {
            "id": line_id,
            "tid": tenant_id,
            "rid": receipt_id,
            "iid": body.inventory_item_id,
            "name": body.extracted_name,
            "qty": body.received_quantity,
            "unit": body.extracted_unit,
            "cost": body.unit_cost_cents,
            "ms": "matched" if body.inventory_item_id is not None else "unmatched",
            "mc": body.inventory_item_id is not None,
            "ord": next_ordinal,
        },
    )
    await _touch_review(db, tenant_id, receipt_id)

    return {
        "id": line_id,
        "extracted_name": body.extracted_name,
        "inventory_item_id": body.inventory_item_id,
        "received_quantity": float(body.received_quantity),
        "extracted_unit": body.extracted_unit,
        "unit_cost_cents": body.unit_cost_cents,
        "confidence": None,
        "manually_corrected": body.inventory_item_id is not None,
        "match_status": "matched" if body.inventory_item_id is not None else "unmatched",
        "line_ordinal": next_ordinal,
    }


async def reset_extraction(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    receipt_id: UUID,
    discard_edits: bool,
) -> dict[str, Any]:
    """Destructive start-over (spec §5, v6.6): requires discard_edits=true. Deletes
    ALL lines (machine + operator), clears review_started_at / reviewed_affirmation /
    quota_blocked(+until), supersedes any still-queued jobs (a stale pending job must
    not resurrect the pre-reset state), then re-enqueues. Notes are PRESERVED (audit).
    """
    if not discard_edits:
        raise ResetNeedsConfirm
    await _lock_editable_receipt(db, tenant_id, receipt_id)

    await db.execute(
        text("DELETE FROM receipt_lines WHERE tenant_id = :tid AND receipt_id = :rid"),
        {"tid": tenant_id, "rid": receipt_id},
    )
    await db.execute(
        text("""
            UPDATE receipts
               SET review_started_at = NULL,
                   reviewed_affirmation = false,
                   quota_blocked = false,
                   quota_blocked_until = NULL,
                   updated_at = now()
             WHERE tenant_id = :tid AND id = :rid
        """),
        {"tid": tenant_id, "rid": receipt_id},
    )
    await db.execute(
        text("""
            UPDATE receipt_extraction_jobs
               SET status = 'superseded'
             WHERE tenant_id = :tid AND receipt_id = :rid
               AND status IN ('pending', 'quota_blocked')
        """),
        {"tid": tenant_id, "rid": receipt_id},
    )
    # review_started_at is now NULL, so the normal enqueue path applies (it also
    # sets extraction_status='pending' and clears quota_blocked).
    return await enqueue_extraction(db, tenant_id=tenant_id, receipt_id=receipt_id)


async def append_note(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    receipt_id: UUID,
    body: NoteCreate,
    user_id: UUID,
) -> dict[str, Any]:
    """Append to the notes_log JSONB audit trail. Allowed in ANY commit_state —
    notes on a committed receipt (e.g. recording why an adjustment was made) are
    legitimate audit entries, and reset-extraction deliberately preserves them."""
    note = {
        "id": str(uuid4()),
        "user_id": str(user_id),
        "text": body.text,
        "created_at": datetime.now(UTC).isoformat(),
    }
    updated = (
        await db.execute(
            text("""
                UPDATE receipts
                   SET notes_log = notes_log || CAST(:note AS jsonb),
                       updated_at = now()
                 WHERE tenant_id = :tid AND id = :rid
                 RETURNING id
            """),
            {"tid": tenant_id, "rid": receipt_id, "note": json.dumps(note)},
        )
    ).scalar()
    if updated is None:
        raise ReceiptNotFound
    return note


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
