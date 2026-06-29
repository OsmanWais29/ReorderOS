"""Receipt service orchestration (Sprint 6 S2).

The API-mediated upload path (D-606-14): the server receives the photo bytes,
validates + EXIF-strips them (validation.validate_and_clean), writes the CLEANED
object to Spaces itself, then creates the draft. Orphan-safe: if the draft INSERT
fails after the PUT, the just-written object is deleted.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.modules.receipts import repo
from app.modules.receipts.validation import extension_for, validate_and_clean


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
