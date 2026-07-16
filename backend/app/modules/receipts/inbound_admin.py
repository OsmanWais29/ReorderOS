"""Inbound email observability surface (Sprint 6 Phase 3b follow-up).

Manager-facing read endpoints so an operator can verify an emailed invoice from
the UI — no DB scripts, no ops narration:

  GET /api/v1/receipts/inbound-address        — tenant forwarding address
  GET /api/v1/receipts/inbound-emails         — recent inbound emails (safe metadata)
  GET /api/v1/receipts/inbound-emails/{id}    — one email + attachments + linked drafts

Safety contract (D-606-15): responses carry metadata ONLY — never email bodies,
never attachment bytes, never object keys, and never the routing token column
(`mailbox_hash`) on email rows. The forwarding-address endpoint is the single
deliberate place the tenant's own token appears, because the address IS the
product feature, delivered over authenticated manager+ API.

NOTE main.py registers this router BEFORE the receipts router: /receipts/{id}
takes a UUID path param, and FastAPI would 422 "inbound-emails" against it
instead of falling through.
"""

from __future__ import annotations

import secrets
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_rls_session
from app.core.security import Principal, require_role

router = APIRouter(prefix="/receipts", tags=["receipts-inbound"])

# ── display status: one server-derived badge per email, so every client agrees ──
# received | processing | filtered | error | draft_created | needs_review | committed


def _display_status(processing_status: str, receipts: list[dict[str, Any]]) -> str:
    if processing_status == "receiving":
        return "received"
    if processing_status in ("pending", "processing"):
        return "processing"
    if processing_status in ("filtered_out", "no_attachment"):
        return "filtered"
    if processing_status in ("failed", "failed_terminal"):
        return "error"
    # complete — the drafts exist; refine by their state.
    if any(r["commit_state"] == "committed" for r in receipts):
        return "committed"
    if any(r["extraction_status"] in ("complete", "failed", "failed_terminal") for r in receipts):
        return "needs_review"
    return "draft_created"


_LIST_SQL = text("""
    SELECT i.id, i.received_at, i.created_at, i.from_email, i.subject, i.source,
           i.processing_status, i.skip_reason, i.filter_flags, i.attachment_count,
           i.has_html_body, i.last_error,
           (SELECT count(*) FROM inbound_email_attachments a
             WHERE a.inbound_email_id = i.id) AS qualified_attachment_count,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
                        'receipt_id', r.id,
                        'commit_state', r.commit_state,
                        'extraction_status', r.extraction_status,
                        'manual_entry_required', r.manual_entry_required,
                        'review_visibility_status', r.review_visibility_status))
               FROM receipts r
              WHERE r.inbound_email_id = i.id AND r.tenant_id = i.tenant_id),
             '[]'::jsonb) AS receipts
      FROM inbound_email_inbox i
     WHERE i.tenant_id = :tid
     ORDER BY i.created_at DESC
     LIMIT :limit
""")


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    import json

    receipts = row["receipts"]
    if isinstance(receipts, str):
        receipts = json.loads(receipts)
    flags = row["filter_flags"]
    if isinstance(flags, str):
        flags = json.loads(flags)
    return {
        "id": row["id"],
        "received_at": row["received_at"],
        "created_at": row["created_at"],
        "from_email": row["from_email"],
        "subject": row["subject"],
        "source": row["source"],
        "processing_status": row["processing_status"],
        "display_status": _display_status(row["processing_status"], receipts),
        "skip_reason": row["skip_reason"],
        "filter_flags": flags,
        "attachment_count": row["attachment_count"],
        "qualified_attachment_count": row["qualified_attachment_count"],
        "has_html_body": row["has_html_body"],
        "error_class": row["last_error"],
        "receipts": receipts,
    }


@router.get("/inbound-emails")
async def list_inbound_emails(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("manager"),
) -> dict[str, Any]:
    rows = (
        (await db.execute(_LIST_SQL, {"tid": principal.tenant_id, "limit": limit}))
        .mappings()
        .fetchall()
    )
    return {"inbound_emails": [_serialize(dict(r)) for r in rows]}


@router.get("/inbound-emails/{inbound_email_id}")
async def get_inbound_email(
    inbound_email_id: UUID,
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("manager"),
) -> dict[str, Any]:
    rows = (
        (await db.execute(_LIST_SQL, {"tid": principal.tenant_id, "limit": 200}))
        .mappings()
        .fetchall()
    )
    match = [dict(r) for r in rows if r["id"] == inbound_email_id]
    if not match:
        raise HTTPException(status_code=404, detail={"code": "INBOUND_EMAIL_NOT_FOUND"})
    out = _serialize(match[0])
    atts = (
        (
            await db.execute(
                text("""
                    SELECT attachment_index, original_filename, mime_type,
                           object_key IS NOT NULL AS stored, receipt_id
                      FROM inbound_email_attachments
                     WHERE inbound_email_id = :iid AND tenant_id = :tid
                     ORDER BY attachment_index
                """),
                {"iid": inbound_email_id, "tid": principal.tenant_id},
            )
        )
        .mappings()
        .fetchall()
    )
    out["attachments"] = [dict(a) for a in atts]
    return out


@router.get("/inbound-address")
async def get_inbound_address(
    db: AsyncSession = Depends(get_rls_session),
    principal: Principal = require_role("manager"),
) -> dict[str, Any]:
    """The tenant's forwarding address. Lazily provisions a routing token on
    first call (idempotent — reuses the active token afterwards). This endpoint
    is deliberately the only place the tenant's own token is returned."""
    settings = get_settings()
    base = settings.postmark_inbound_address
    if not (settings.postmark_inbound_enabled and base and "@" in base):
        return {"configured": False, "address": None}
    token = (
        await db.execute(
            text(
                "SELECT token FROM tenant_inbound_email_tokens "
                "WHERE tenant_id = :tid AND revoked_at IS NULL "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"tid": principal.tenant_id},
        )
    ).scalar_one_or_none()
    if token is None:
        token = secrets.token_hex(12)
        await db.execute(
            text("INSERT INTO tenant_inbound_email_tokens (tenant_id, token) VALUES (:tid, :tok)"),
            {"tid": principal.tenant_id, "tok": token},
        )
        await db.commit()  # durability rule: no mutating endpoint without commit
    local, domain = base.split("@", 1)
    return {"configured": True, "address": f"{local}+{token}@{domain}"}
