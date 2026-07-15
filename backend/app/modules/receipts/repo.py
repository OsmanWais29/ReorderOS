"""Data access for the receipts API (Sprint 6 S2).

Every query is explicitly scoped by tenant_id (not relying on RLS alone — the app
may connect as a role that bypasses RLS): a receipt belonging to another tenant
simply isn't found → 404, never leaking existence. This module owns the NEW
canonical receipts surface; the Sprint-3 inventory receipt path stays as a
deprecated shim (D-606-10).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.inventory.item_resolver import suggest_inventory_items
from app.modules.receipts.conversion import suggest_conversion

# commit_states a draft can still be dismissed/cancelled/deleted from.
_MUTABLE_STATES = ("draft", "pending_review")


async def create_draft(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    source: str,
    receipt_id: UUID | None = None,
    photo_object_key: str | None = None,
    original_filename: str | None = None,
    mime_type: str | None = None,
    file_size_bytes: int | None = None,
    supplier_name: str | None = None,
    invoice_date: date | None = None,
    created_by: UUID | None = None,
) -> UUID:
    """Insert a draft receipt and return its id. `source` is one of the intake enum
    values (mobile_photo/manual/...). The caller (services) supplies a validated
    photo_object_key for the upload path."""
    rid = receipt_id or uuid4()
    await db.execute(
        text("""
            INSERT INTO receipts
                (id, tenant_id, commit_state, source, photo_object_key,
                 original_filename, mime_type, file_size_bytes, supplier_name,
                 invoice_date, created_by)
            VALUES
                (:id, :tid, 'draft', :source, :key,
                 :fname, :mime, :size, :supplier,
                 :inv_date, :by)
        """),
        {
            "id": rid,
            "tid": tenant_id,
            "source": source,
            "key": photo_object_key,
            "fname": original_filename,
            "mime": mime_type,
            "size": file_size_bytes,
            "supplier": supplier_name,
            "inv_date": invoice_date,
            "by": created_by,
        },
    )
    return rid


async def get_receipt(db: AsyncSession, tenant_id: UUID, receipt_id: UUID) -> dict[str, Any] | None:
    """Full draft for the review screen (spec §5): header + lines + per-line match
    suggestions (unmatched lines only — a suggestion, never an auto-match, D-606-26).
    None if not this tenant's (→ 404)."""
    header = (
        (
            await db.execute(
                text("""
                SELECT id, source, commit_state, extraction_status, supplier_name,
                       total_cents, manual_entry_required, quota_blocked, created_at,
                       photo_object_key, mime_type, invoice_number, invoice_date,
                       subtotal_cents, tax_cents, extraction_confidence,
                       review_visibility_status, sender_email, filter_flags,
                       reviewed_affirmation, review_started_at, notes_log
                  FROM receipts
                 WHERE tenant_id = :tid AND id = :rid
            """),
                {"tid": tenant_id, "rid": receipt_id},
            )
        )
        .mappings()
        .fetchone()
    )
    if header is None:
        return None

    lines = (
        (
            await db.execute(
                text("""
                SELECT rl.id, rl.extracted_name, rl.inventory_item_id,
                       rl.received_quantity, rl.extracted_unit, rl.unit_cost_cents,
                       rl.confidence, rl.manually_corrected, rl.match_status,
                       rl.line_ordinal, ii.name AS item_name,
                       su.name AS item_storage_unit,
                       rl.purchase_quantity, rl.purchase_unit, rl.received_unit,
                       rl.conversion_factor, rl.conversion_source,
                       rl.conversion_confirmed_at,
                       rl.pack_count, rl.pack_size_qty, rl.pack_size_unit,
                       rl.actual_weight_qty, rl.actual_weight_unit
                  FROM receipt_lines rl
                  LEFT JOIN inventory_items ii ON ii.id = rl.inventory_item_id
                  LEFT JOIN units_of_measure su ON su.id = ii.storage_unit_id
                 WHERE rl.tenant_id = :tid AND rl.receipt_id = :rid
                 ORDER BY rl.line_ordinal NULLS LAST, rl.id
            """),
                {"tid": tenant_id, "rid": receipt_id},
            )
        )
        .mappings()
        .all()
    )

    # Remembered pack conversions for the linked items on this receipt — one
    # query, keyed by (item_id, purchase_unit). Prefill only; operator confirms.
    remembered: dict[tuple[str, str], Decimal] = {}
    item_ids = [ln["inventory_item_id"] for ln in lines if ln["inventory_item_id"]]
    if item_ids:
        rows = (
            await db.execute(
                text("""
                    SELECT inventory_item_id, purchase_unit, factor
                      FROM tenant_item_purchase_conversions
                     WHERE tenant_id = :tid AND inventory_item_id = ANY(:ids)
                """),
                {"tid": tenant_id, "ids": item_ids},
            )
        ).fetchall()
        remembered = {(str(r[0]), r[1]): Decimal(str(r[2])) for r in rows}

    result = dict(header)
    result["lines"] = []
    for line in lines:
        row = dict(line)
        row["suggestions"] = (
            await suggest_inventory_items(db, tenant_id, row["extracted_name"], limit=3)
            if row["match_status"] == "unmatched" and row["extracted_name"]
            else []
        )
        row.update(
            {"suggested_quantity": None, "suggested_factor": None, "suggestion_source": None}
        )
        # Conversion prefill: only for linked, not-yet-confirmed lines whose
        # invoice U/M isn't already the storage unit.
        if (
            row["inventory_item_id"]
            and row["item_storage_unit"]
            and row["conversion_confirmed_at"] is None
            and row["received_quantity"] is not None
        ):
            key = (str(row["inventory_item_id"]), (row["extracted_unit"] or "").strip())
            s = suggest_conversion(
                purchase_qty=Decimal(str(row["received_quantity"])),
                purchase_unit=row["extracted_unit"],
                storage_unit=row["item_storage_unit"],
                pack_count=row["pack_count"],
                pack_size_qty=row["pack_size_qty"],
                pack_size_unit=row["pack_size_unit"],
                actual_weight_qty=row["actual_weight_qty"],
                actual_weight_unit=row["actual_weight_unit"],
                remembered_factor=remembered.get(key),
            )
            if s is not None:
                row["suggested_quantity"] = float(s.quantity)
                row["suggested_factor"] = float(s.factor)
                row["suggestion_source"] = s.source
        result["lines"].append(row)
    return result


async def list_receipts(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    commit_state: str | None = None,
    extraction_status: str | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """List receipts, newest first, with optional filters. Suppressed (not-invoice)
    receipts are hidden from the default queue (review_visibility_status='visible')."""
    rows = (
        (
            await db.execute(
                text("""
                SELECT id, source, commit_state, extraction_status, supplier_name,
                       total_cents, manual_entry_required, quota_blocked, created_at
                  FROM receipts
                 WHERE tenant_id = :tid
                   AND review_visibility_status = 'visible'
                   AND (CAST(:cs AS text)  IS NULL OR commit_state      = :cs)
                   AND (CAST(:es AS text)  IS NULL OR extraction_status = :es)
                   AND (CAST(:src AS text) IS NULL OR source            = :src)
                 ORDER BY created_at DESC
            """),
                {
                    "tid": tenant_id,
                    "cs": commit_state,
                    "es": extraction_status,
                    "src": source,
                },
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def set_commit_state(
    db: AsyncSession,
    tenant_id: UUID,
    receipt_id: UUID,
    *,
    new_state: str,
    dismissed_reason: str | None = None,
) -> str:
    """Transition a draft to dismissed/cancelled. Returns 'ok' | 'not_found' |
    'conflict' (already terminal — can't dismiss a committed/cancelled receipt)."""
    current = (
        await db.execute(
            text("SELECT commit_state FROM receipts WHERE tenant_id = :tid AND id = :rid"),
            {"tid": tenant_id, "rid": receipt_id},
        )
    ).scalar()
    if current is None:
        return "not_found"
    if current not in _MUTABLE_STATES:
        return "conflict"
    await db.execute(
        text("""
            UPDATE receipts
               SET commit_state = :state,
                   dismissed_reason = COALESCE(:reason, dismissed_reason),
                   updated_at = now()
             WHERE tenant_id = :tid AND id = :rid
        """),
        {"state": new_state, "reason": dismissed_reason, "tid": tenant_id, "rid": receipt_id},
    )
    return "ok"


async def delete_draft(db: AsyncSession, tenant_id: UUID, receipt_id: UUID) -> str:
    """Hard-delete a DRAFT receipt. Returns 'ok' | 'not_found' | 'conflict' (only a
    draft can be deleted; committed/cancelled/dismissed are immutable history)."""
    current = (
        await db.execute(
            text("SELECT commit_state FROM receipts WHERE tenant_id = :tid AND id = :rid"),
            {"tid": tenant_id, "rid": receipt_id},
        )
    ).scalar()
    if current is None:
        return "not_found"
    if current != "draft":
        return "conflict"
    await db.execute(
        text("DELETE FROM receipts WHERE tenant_id = :tid AND id = :rid"),
        {"tid": tenant_id, "rid": receipt_id},
    )
    return "ok"
