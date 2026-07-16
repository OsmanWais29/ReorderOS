"""Postmark inbound email webhook (Sprint 6 Phase 3b).

  POST /api/v1/webhooks/inbound/postmark

Four-state intake lifecycle, split by owner (spec v6.14 #main):

  receiving → pending      — THIS webhook's job (bytes durable, no drafts yet)
  processing → complete    — inbound_email_worker's job (drafts exist)

Design decisions carried from the spec + prior sprint lessons:
  - Reservation-before-upload: the inbox row is INSERTed first ('receiving', with a
    lease) — the partial-unique on (tenant_id, postmark_message_id) IS the dedup
    reservation. Objects are then uploaded to inbox_id-keyed keys (D-606-24), so a
    concurrent retry can never delete the winner's objects.
  - Duplicate delivery branches on the existing row's state: complete/pending/
    processing/terminal → 200 no-op; stale 'receiving' (lease expired) → re-acquire
    the lease and finish the upload with THIS request's bytes. The webhook never
    touches a row the worker owns ('processing' reclaim belongs to the worker).
  - Unknown token spends nothing (#6): metadata-only row (tenant_id NULL,
    'filtered_out' + suppression_stage='pre_draft') + monitoring alert, 200 —
    no attachment validation, no upload of attacker-supplied bytes.
  - Terminal rejects use 'filtered_out'/'no_attachment', never 'failed' (#4):
    'failed' is retriable-only, so a deliberately-rejected message can't be
    resurrected by a Postmark retry.
  - Fence rule (PR #4 lesson): the finalize transaction's attachment-row inserts
    are unfenced siblings of the fenced receiving→pending flip — if the fence
    misses (0 rows), the WHOLE transaction rolls back.
  - D-606-15: never log bodies, attachment bytes, or tokens — only message ids,
    counts, status, and error classes.

HTML-body extraction (D-606-17) is deliberately NOT in this phase: emails whose
only content is an HTML body are terminally 'no_attachment' with has_html_body
recorded and a 'html_body_deferred' filter flag, so the later phase can find them.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.service_db import get_service_sessionmaker
from app.modules.receipts.validation import (
    ReceiptValidationError,
    extension_for,
    validate_and_clean,
)

log = get_logger(__name__)

# Per-attachment bounds — derived from REAL provider limits, not the upload
# path's 50 MB validator cap (which is unreachable on this channel):
#   - Postmark caps the whole inbound message at 35 MB raw MIME; base64 overhead
#     (~37%) makes ~25 MB the largest single attachment that can even arrive.
#   - Anthropic PDF extraction caps requests at 32 MB (the PDF goes in base64,
#     +37%) and 100 pages — anything past ~20 MB or 100 pages cannot extract.
# So: 20 MB / 100 pages accepts every extractable invoice this channel can
# physically deliver. Minimum size is TYPE-SPLIT (live-test finding: a real 4.3 KB
# digital invoice PDF was filtered): the 10 KB floor exists to drop signature/logo
# GRAPHICS, so it applies to images only; PDFs are rarely decorative, so they get
# a 1 KB sanity floor and rely on magic-byte + page validation instead.
MIN_IMAGE_BYTES = 10 * 1024
MIN_PDF_BYTES = 1024
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 100
MAX_SPAM_SCORE = 5.0

# Webhook upload lease — a crashed upload becomes finishable by a Postmark retry
# after this TTL (same TTL as the worker leases).
_LEASE_TTL_SQL = "INTERVAL '5 minutes'"


async def _require_postmark_enabled() -> None:
    """503 when the channel is off — mirrors the CLOVER_ENABLED pattern so
    deployments without Postmark run with zero Postmark configuration."""
    s = get_settings()
    if not (s.postmark_inbound_enabled and s.postmark_webhook_user and s.postmark_webhook_password):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "POSTMARK_INBOUND_DISABLED",
                "message": "Postmark inbound email is disabled.",
            },
        )


router = APIRouter(
    prefix="/webhooks/inbound",
    tags=["webhooks"],
    dependencies=[Depends(_require_postmark_enabled)],
)


def _check_basic_auth(request: Request) -> None:
    """Constant-time HTTP Basic Auth (both halves via hmac.compare_digest)."""
    settings = get_settings()
    header = request.headers.get("Authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        raise HTTPException(status_code=401, detail={"code": "POSTMARK_AUTH_REQUIRED"})
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
        user, _, password = decoded.partition(":")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=401, detail={"code": "POSTMARK_AUTH_INVALID"}) from exc
    expected_user = settings.postmark_webhook_user or ""
    expected_pass = settings.postmark_webhook_password or ""
    user_ok = hmac.compare_digest(user.encode(), expected_user.encode())
    pass_ok = hmac.compare_digest(password.encode(), expected_pass.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=401, detail={"code": "POSTMARK_AUTH_INVALID"})


def _spam_score(payload: dict[str, Any]) -> float:
    """Postmark surfaces the SpamAssassin score in the Headers list."""
    for h in payload.get("Headers") or []:
        if str(h.get("Name", "")).lower() == "x-spam-score":
            try:
                return float(str(h.get("Value", "0")).strip())
            except ValueError:
                return 0.0
    return 0.0


def _received_at(payload: dict[str, Any]) -> Any:
    raw = payload.get("Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None


def _qualify_attachments(
    attachments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Decode + validate every attachment; return (qualified, skip_reasons).

    Each qualified entry: {index, filename, mime_type, cleaned (bytes)}. The index
    is the ORIGINAL payload position — deterministic fan-out ordering and the
    UNIQUE(inbound_email_id, attachment_index) key both depend on it. Skips are
    per-attachment and content-free (reason codes only, D-606-15).
    """
    qualified: list[dict[str, Any]] = []
    skips: list[str] = []
    for idx, att in enumerate(attachments):
        name = att.get("Name")
        content = att.get("Content") or ""
        try:
            raw = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            skips.append(f"attachment_{idx}:INBOUND_ATTACHMENT_DECODE_ERROR")
            continue
        # Size floor by leading bytes only (validate_and_clean still owns the
        # authoritative type decision below).
        min_bytes = MIN_PDF_BYTES if raw.startswith(b"%PDF-") else MIN_IMAGE_BYTES
        if len(raw) < min_bytes:
            skips.append(f"attachment_{idx}:INBOUND_ATTACHMENT_TOO_SMALL")
            continue
        if len(raw) > MAX_ATTACHMENT_BYTES:
            skips.append(f"attachment_{idx}:INBOUND_ATTACHMENT_TOO_LARGE")
            continue
        try:
            mime_type, cleaned = validate_and_clean(raw, filename=name)
        except ReceiptValidationError as exc:
            skips.append(f"attachment_{idx}:{exc.code}")
            continue
        if mime_type == "application/pdf":
            pages = _pdf_page_count(cleaned)
            if pages == 0:
                skips.append(f"attachment_{idx}:RECEIPT_PDF_UNREADABLE")
                continue
            if pages is not None and pages > MAX_PDF_PAGES:
                skips.append(f"attachment_{idx}:RECEIPT_TOO_MANY_PAGES")
                continue
        qualified.append(
            {"index": idx, "filename": name, "mime_type": mime_type, "cleaned": cleaned}
        )
    return qualified, skips


def _pdf_page_count(data: bytes) -> int | None:
    """Best-effort page count against the Anthropic 100-page extraction cap.

    Deliberately LENIENT: None (unparseable here / encrypted) passes the
    attachment through — pypdf strictness must never reject a real supplier
    invoice the extraction provider could read. Only POSITIVE knowledge rejects:
    a confirmed zero-page file or a confirmed over-cap count."""
    from io import BytesIO

    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                return None
        return len(reader.pages)
    except Exception:
        return None


def _object_key(tenant_id: UUID, inbox_id: UUID, index: int, mime_type: str) -> str:
    """D-606-24 inbound key shape — inbox_id-scoped, deterministic per index so a
    retried upload overwrites its own object instead of orphaning a new one."""
    return f"receipts/{tenant_id}/inbound/postmark/{inbox_id}/{index}.{extension_for(mime_type)}"


async def _resolve_tenant(s: AsyncSession, token: str) -> UUID | None:
    if not token:
        return None
    row = (
        await s.execute(
            text(
                "SELECT tenant_id FROM tenant_inbound_email_tokens "
                "WHERE token = :tok AND revoked_at IS NULL"
            ),
            {"tok": token},
        )
    ).fetchone()
    return row[0] if row else None


async def _record_unknown_token(s: AsyncSession, payload: dict[str, Any], message_id: str) -> None:
    """Metadata-only row + alert (#6). No byte work happens for unknown tokens.
    The partial unique on (postmark_message_id) WHERE tenant_id IS NULL dedups
    replays — the alert fires only when the row is genuinely new.

    Retention is the diagnostic minimum: message id (dedup), mailbox_hash (the
    attempted token — what ops needs to fix a typo'd forward), from_email (who
    mis-forwarded), counts. NO subject, NO body, NO attachment bytes/filenames —
    an unknown-token row never reaches a review queue, so content serves nothing
    (D-606-15 posture)."""
    inserted = (
        await s.execute(
            text("""
                INSERT INTO inbound_email_inbox
                    (tenant_id, postmark_message_id, source, mailbox_hash, from_email,
                     subject, received_at, attachment_count, has_html_body,
                     processing_status, suppression_stage, skip_reason)
                VALUES
                    (NULL, :mid, 'postmark', :hash, :sender,
                     NULL, :rcvd, :att_count, :has_html,
                     'filtered_out', 'pre_draft', 'unknown_token')
                ON CONFLICT (postmark_message_id)
                    WHERE tenant_id IS NULL AND postmark_message_id IS NOT NULL
                    DO NOTHING
                RETURNING id
            """),
            {
                "mid": message_id,
                "hash": payload.get("MailboxHash"),
                "sender": payload.get("From"),
                "rcvd": _received_at(payload),
                "att_count": len(payload.get("Attachments") or []),
                "has_html": bool(payload.get("HtmlBody")),
            },
        )
    ).fetchone()
    if inserted:
        await s.execute(
            text("""
                INSERT INTO monitoring_alerts (tenant_id, monitor_name, severity, trigger_payload)
                VALUES (NULL, 'postmark_unknown_token', 'warn',
                        jsonb_build_object('postmark_message_id', CAST(:mid AS text)))
            """),
            {"mid": message_id},
        )
    await s.commit()
    log.info("postmark_inbound.unknown_token", postmark_message_id=message_id)


async def _sender_flags(s: AsyncSession, tenant_id: UUID, from_email: str | None) -> list[str]:
    """Layer 2 — allowlist is a routing convenience, not a security boundary
    (D-606-02): unknown senders get a review-queue flag, never a reject."""
    count = (
        await s.execute(
            text("SELECT count(*) FROM tenant_invoice_senders WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
    ).scalar_one()
    if count == 0:
        return []
    email = (from_email or "").strip().lower()
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    matched = (
        await s.execute(
            text("""
                SELECT count(*) FROM tenant_invoice_senders
                 WHERE tenant_id = :tid
                   AND ((match_type = 'email' AND lower(match_value) = :email)
                        OR (match_type = 'domain' AND lower(match_value) = :domain))
            """),
            {"tid": tenant_id, "email": email, "domain": domain},
        )
    ).scalar_one()
    return [] if matched else ["unknown_sender"]


async def _reserve(
    s: AsyncSession, tenant_id: UUID, message_id: str, payload: dict[str, Any]
) -> tuple[UUID, UUID] | None:
    """Insert the 'receiving' reservation. Returns (inbox_id, lease_token) when this
    request won the reservation, None when a row already exists (caller branches)."""
    row = (
        await s.execute(
            text("""
                INSERT INTO inbound_email_inbox
                    (tenant_id, postmark_message_id, source, mailbox_hash, from_email,
                     subject, received_at, attachment_count, has_html_body,
                     processing_status, locked_at, lease_token)
                VALUES
                    (:tid, :mid, 'postmark', :hash, :sender,
                     :subject, :rcvd, :att_count, :has_html,
                     'receiving', now(), gen_random_uuid())
                ON CONFLICT (tenant_id, postmark_message_id)
                    WHERE tenant_id IS NOT NULL AND postmark_message_id IS NOT NULL
                    DO NOTHING
                RETURNING id, lease_token
            """),
            {
                "tid": tenant_id,
                "mid": message_id,
                "hash": payload.get("MailboxHash"),
                "sender": payload.get("From"),
                "subject": payload.get("Subject"),
                "rcvd": _received_at(payload),
                "att_count": len(payload.get("Attachments") or []),
                "has_html": bool(payload.get("HtmlBody")),
            },
        )
    ).fetchone()
    return (row[0], row[1]) if row else None


async def _branch_existing(
    s: AsyncSession, tenant_id: UUID, message_id: str
) -> tuple[UUID, UUID] | str:
    """Duplicate delivery: decide from the existing row's state. Returns a fresh
    (inbox_id, lease_token) when this request may finish a stale 'receiving' upload,
    else the existing status string for a 200 no-op."""
    row = (
        (
            await s.execute(
                text(
                    "SELECT id, processing_status FROM inbound_email_inbox "
                    "WHERE tenant_id = :tid AND postmark_message_id = :mid"
                ),
                {"tid": tenant_id, "mid": message_id},
            )
        )
        .mappings()
        .fetchone()
    )
    if row is None:  # lost a race with a concurrent delete; treat as no-op
        return "gone"
    if row["processing_status"] != "receiving":
        # pending/processing/complete/failed/terminal: the worker (or a prior
        # request) owns it — the webhook never re-runs byte work on these (#main).
        return str(row["processing_status"])

    reacquired = (
        await s.execute(
            text(f"""
                UPDATE inbound_email_inbox
                   SET locked_at = now(), lease_token = gen_random_uuid()
                 WHERE id = :iid AND processing_status = 'receiving'
                   AND locked_at < now() - {_LEASE_TTL_SQL}
                RETURNING id, lease_token
            """),  # noqa: S608
            {"iid": row["id"]},
        )
    ).fetchone()
    if reacquired is None:
        return "receiving"  # fresh lease — another delivery is mid-upload
    return (reacquired[0], reacquired[1])


async def _finalize(
    s: AsyncSession,
    *,
    inbox_id: UUID,
    lease_token: UUID,
    tenant_id: UUID,
    uploaded: list[dict[str, Any]],
    attachment_total: int,
    has_html_body: bool,
    flags: list[str],
    skips: list[str],
    force_terminal: tuple[str, str] | None = None,
) -> str | None:
    """One transaction: attachment rows + the FENCED receiving→pending flip.

    Returns the final status, or None when the fence missed — in which case the
    whole transaction (attachment rows included — the PR #4 unfenced-sibling rule)
    has been rolled back."""
    for item in uploaded:
        await s.execute(
            text("""
                INSERT INTO inbound_email_attachments
                    (inbound_email_id, tenant_id, attachment_index,
                     original_filename, mime_type, object_key)
                VALUES (:iid, :tid, :idx, :fname, :mime, :key)
                ON CONFLICT (inbound_email_id, attachment_index)
                DO UPDATE SET object_key = EXCLUDED.object_key,
                              mime_type = EXCLUDED.mime_type,
                              original_filename = EXCLUDED.original_filename
            """),
            {
                "iid": inbox_id,
                "tid": tenant_id,
                "idx": item["index"],
                "fname": item["filename"],
                "mime": item["mime_type"],
                "key": item["object_key"],
            },
        )

    if force_terminal is not None:
        status, skip_reason = force_terminal
    elif uploaded:
        status = "pending"
        skip_reason = None
        if has_html_body and any(i["mime_type"] == "application/pdf" for i in uploaded):
            # Attachments-win narrowed to PDF (#22): a PDF is the authoritative invoice.
            flags = [*flags, "html_body_ignored"]
    elif attachment_total > 0:
        status = "filtered_out"
        skip_reason = "no_qualifying_attachment"
    else:
        status = "no_attachment"
        skip_reason = "no_attachment"
    if force_terminal is None and not uploaded and has_html_body:
        # D-606-17 HTML-body extraction is a later phase; mark rows it should revisit.
        flags = [*flags, "html_body_deferred"]

    fenced = (
        await s.execute(
            text("""
                UPDATE inbound_email_inbox
                   SET processing_status = :st,
                       suppression_stage = CASE WHEN :st = 'filtered_out'
                                                THEN 'pre_draft' ELSE suppression_stage END,
                       skip_reason = :skip,
                       filter_flags = CAST(:flags AS jsonb),
                       locked_at = NULL,
                       lease_token = NULL
                 WHERE id = :iid AND lease_token = :tok AND processing_status = 'receiving'
                RETURNING id
            """),
            {
                "st": status,
                # Per-attachment skip details live in filter_flags; skip_reason is
                # the row-level reason and only terminal rows have one.
                "skip": skip_reason,
                "flags": json.dumps(flags + skips),
                "iid": inbox_id,
                "tok": lease_token,
            },
        )
    ).fetchone()
    if fenced is None:
        await s.rollback()
        return None
    await s.commit()
    return status


@router.post("/postmark")
async def handle_postmark_inbound(request: Request) -> JSONResponse:
    """Receive one Postmark inbound message. Always 200 on handled outcomes
    (Postmark retries non-2xx — 500 is reserved for genuinely transient failures
    like a storage outage, where a retry is exactly what we want)."""
    _check_basic_auth(request)

    try:
        payload = json.loads(await request.body())
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"code": "POSTMARK_MALFORMED"}) from exc

    message_id = str(payload.get("MessageID") or "").strip()
    if not message_id:
        # No dedup key — nothing safe to store. Content-free log, 200 (no retry).
        log.warning("postmark_inbound.missing_message_id")
        return JSONResponse({"status": "ignored"})

    attachments = payload.get("Attachments") or []
    sm = get_service_sessionmaker()

    async with sm() as s:
        tenant_id = await _resolve_tenant(s, str(payload.get("MailboxHash") or ""))
        if tenant_id is None:
            await _record_unknown_token(s, payload, message_id)
            return JSONResponse({"status": "filtered_out", "reason": "unknown_token"})

        spam = _spam_score(payload)
        reservation = await _reserve(s, tenant_id, message_id, payload)
        if reservation is None:
            branch = await _branch_existing(s, tenant_id, message_id)
            await s.commit()
            if isinstance(branch, str):
                log.info(
                    "postmark_inbound.duplicate",
                    postmark_message_id=message_id,
                    tenant_id=str(tenant_id),
                    existing_status=branch,
                )
                return JSONResponse({"status": branch, "duplicate": True})
            reservation = branch  # stale 'receiving' — finish with THIS request's bytes
        else:
            await s.commit()  # reservation durable before any byte work

    inbox_id, lease_token = reservation

    # Layer 3 spam gate — terminal, no byte work (fenced like every other flip).
    if spam > MAX_SPAM_SCORE:
        async with sm() as s:
            status = await _finalize(
                s,
                inbox_id=inbox_id,
                lease_token=lease_token,
                tenant_id=tenant_id,
                uploaded=[],
                attachment_total=len(attachments),
                has_html_body=bool(payload.get("HtmlBody")),
                flags=["spam_score_exceeded"],
                skips=[],
                force_terminal=("filtered_out", "spam_score_exceeded"),
            )
        log.info(
            "postmark_inbound.spam_filtered",
            postmark_message_id=message_id,
            tenant_id=str(tenant_id),
        )
        return JSONResponse({"status": status or "superseded"})

    # ── Byte work: validate + upload OUTSIDE any DB transaction ──────────────
    qualified, skips = _qualify_attachments(attachments)
    uploaded: list[dict[str, Any]] = []
    try:
        for item in qualified:
            key = _object_key(tenant_id, inbox_id, item["index"], item["mime_type"])
            storage.put_bytes(key, item["cleaned"], content_type=item["mime_type"])
            uploaded.append({**item, "object_key": key})
    except Exception as exc:
        # Transient storage failure: leave the row 'receiving' (lease expires, a
        # Postmark retry finishes the upload). 500 asks Postmark to retry.
        log.error(
            "postmark_inbound.storage_error",
            postmark_message_id=message_id,
            tenant_id=str(tenant_id),
            error_class=type(exc).__name__,
        )
        raise HTTPException(
            status_code=500, detail={"code": "POSTMARK_STORAGE_UNAVAILABLE"}
        ) from exc

    async with sm() as s:
        flags = await _sender_flags(s, tenant_id, payload.get("From"))
        status = await _finalize(
            s,
            inbox_id=inbox_id,
            lease_token=lease_token,
            tenant_id=tenant_id,
            uploaded=uploaded,
            attachment_total=len(attachments),
            has_html_body=bool(payload.get("HtmlBody")),
            flags=flags,
            skips=skips,
        )

    log.info(
        "postmark_inbound.received",
        postmark_message_id=message_id,
        tenant_id=str(tenant_id),
        attachment_count=len(attachments),
        qualified_count=len(uploaded),
        status=status or "superseded",
    )
    return JSONResponse({"status": status or "superseded", "attachments_qualified": len(uploaded)})
