"""Receipt extraction worker (Sprint 6 S3).

Runs as `service_worker`. Claims jobs cross-tenant (receipt_extraction_jobs uses a
USING(true) policy), then operates tenant-scoped on receipts/receipt_lines after
SET'ing app.tenant_id (the grants are in 0025).

Concurrency model (§3.3 / D-606-23):
  - SHORT claim transaction: FOR UPDATE SKIP LOCKED → status='processing', stamp
    locked_at + a fresh lease_token; commit immediately so NO DB locks are held
    during the network I/O (Spaces GET, LLM) that follows.
  - FENCING: every terminal write is `WHERE id=:jid AND lease_token=:token`. If this
    worker stalled, its lease expired, and another worker reclaimed the job, the
    fenced write touches 0 rows and this worker discards its result (no double-apply).
  - Order: claim → hard-stop check → download + RE-VALIDATE magic bytes → quota
    charge → LLM → checkpoint raw_extraction → apply. A validation-failed or
    superseded job never consumes a quota slot; quota is charged once (attempts==0),
    so a transient-failure retry does not re-charge.

NOTE — the spec's per-tenant monthly *spend* kill switch does not exist in the
codebase; the per-tenant daily *quota* (tenant_extraction_rate_limits) is the v1
cost control. A spend check is a documented future hook (see _SPEND_HOOK below).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.logging import get_logger
from app.core.service_db import get_service_sessionmaker
from app.modules.inventory.depletion.units import is_canonical
from app.modules.receipts.extraction_llm import ExtractionClient, ExtractionUnavailable
from app.modules.receipts.validation import ReceiptValidationError, validate_and_clean

log = get_logger(__name__)

MAX_ATTEMPTS = 3
_LEASE_TTL_SQL = "INTERVAL '5 minutes'"
_LOW_CONFIDENCE = 0.4

# Future hook: a per-tenant spend kill switch would be checked here (before the quota
# charge) and set status='skipped' if exceeded. No such component exists yet.
_SPEND_HOOK = False


# _LEASE_TTL_SQL is a module constant (not user input) — the f-string is safe.
_CLAIM_SQL = text(f"""
    UPDATE receipt_extraction_jobs
       SET status = 'processing',
           locked_at = now(),
           lease_token = gen_random_uuid(),
           started_at = COALESCE(started_at, now())
     WHERE id = (
        SELECT id FROM receipt_extraction_jobs
         WHERE status = 'pending'
            OR status = 'failed'
            OR (status = 'quota_blocked' AND quota_blocked_until <= now())
            OR (status = 'processing' AND locked_at < now() - {_LEASE_TTL_SQL})
         ORDER BY created_at
         FOR UPDATE SKIP LOCKED
         LIMIT 1
     )
 RETURNING id, tenant_id, receipt_id, job_attempt, attempts, lease_token, raw_extraction
""")  # noqa: S608  — _LEASE_TTL_SQL is a module constant, not user input


class ExtractionWorker:
    def __init__(self, llm: ExtractionClient) -> None:
        self._llm = llm

    async def process_once(self) -> bool:
        """Claim and fully process one job. Returns False if no job was available."""
        claim = await self._claim()
        if claim is None:
            return False
        await self._process(claim)
        return True

    async def _claim(self) -> dict[str, Any] | None:
        sm = get_service_sessionmaker()
        async with sm() as s:
            row = (await s.execute(_CLAIM_SQL)).mappings().fetchone()
            await s.commit()
            return dict(row) if row else None

    async def _process(self, job: dict[str, Any]) -> None:
        jid, token = job["id"], job["lease_token"]
        tenant_id, receipt_id = job["tenant_id"], job["receipt_id"]
        sm = get_service_sessionmaker()
        async with sm() as s:
            await s.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            receipt = (
                (
                    await s.execute(
                        text(
                            "SELECT photo_object_key, mime_type, review_started_at "
                            "FROM receipts WHERE id = :rid AND tenant_id = :tid"
                        ),
                        {"rid": receipt_id, "tid": tenant_id},
                    )
                )
                .mappings()
                .fetchone()
            )
            if receipt is None:
                await self._terminal(s, jid, token, "failed_terminal", "receipt missing")
                await s.commit()
                return

            # ── hard-stop: a human has begun review → discard, supersede ──────────
            if receipt["review_started_at"] is not None:
                await self._supersede(s, jid, token, receipt_id, tenant_id)
                await s.commit()
                return

            # ── reuse a checkpointed extraction (crash-after-LLM-before-lines) ────
            payload = job["raw_extraction"]
            if payload is None:
                # download + RE-VALIDATE (terminal on tamper/corruption) ───────────
                try:
                    raw = storage.get_bytes(receipt["photo_object_key"])
                    _mime, _clean = validate_and_clean(raw, filename=None)
                except ReceiptValidationError as exc:
                    await self._terminal(s, jid, token, "failed_terminal", exc.code)
                    await self._mark_receipt_failed(s, receipt_id, tenant_id)
                    await s.commit()
                    return
                except storage.SpacesNotConfigured:
                    await self._transient_fail(
                        s, jid, token, job, receipt_id, tenant_id, "storage unavailable"
                    )
                    await s.commit()
                    return

                # ── quota charge (once per job: only the first attempt) ───────────
                if job["attempts"] == 0:
                    jobs_today = await self._charge_quota(s, tenant_id)
                    if jobs_today is None:
                        await self._quota_block(s, jid, token, receipt_id, tenant_id)
                        await s.commit()
                        return

                # ── LLM (transient failure → retry / failed_terminal) ─────────────
                try:
                    result = await self._llm.extract_invoice(
                        file_bytes=raw, mime_type=receipt["mime_type"]
                    )
                except ExtractionUnavailable as exc:
                    await self._transient_fail(s, jid, token, job, receipt_id, tenant_id, str(exc))
                    await s.commit()
                    return

                payload = result.payload
                log.info(
                    "receipt.extraction",
                    tenant_id=str(tenant_id),
                    receipt_id=str(receipt_id),
                    model=result.model_version,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                )
                # CHECKPOINT before any line write (fenced): a crash here means the
                # retry finds the payload and skips a second paid LLM call.
                if not await self._checkpoint(s, jid, token, payload):
                    await s.commit()  # lost the lease — discard
                    return

            # ── re-check hard-stop after the (possibly slow) LLM call ─────────────
            fresh = (
                await s.execute(
                    text("SELECT review_started_at FROM receipts WHERE id=:rid AND tenant_id=:tid"),
                    {"rid": receipt_id, "tid": tenant_id},
                )
            ).scalar()
            if fresh is not None:
                await self._supersede(s, jid, token, receipt_id, tenant_id)
                await s.commit()
                return

            await self._apply(s, jid, token, job, receipt_id, tenant_id, payload)
            await s.commit()

    # ── stages ────────────────────────────────────────────────────────────────

    async def _apply(
        self,
        s: AsyncSession,
        jid: UUID,
        token: UUID,
        job: dict[str, Any],
        receipt_id: UUID,
        tenant_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        if str(payload.get("document_type")) == "not_invoice":
            await s.execute(
                text(
                    "UPDATE receipts SET review_visibility_status='suppressed', "
                    "suppression_reason='not_invoice', extraction_status='complete', "
                    "quota_blocked=false, updated_at=now() WHERE id=:rid AND tenant_id=:tid"
                ),
                {"rid": receipt_id, "tid": tenant_id},
            )
            await self._terminal(s, jid, token, "complete", None)
            return

        raw_lines = payload.get("lines")
        lines = raw_lines if isinstance(raw_lines, list) else []
        confidences: list[float] = []
        for ordinal, raw_line in enumerate(lines):
            qty = _to_decimal(raw_line.get("qty"))
            unit = str(raw_line.get("unit", ""))
            conf = _to_float(raw_line.get("confidence"))
            confidences.append(conf)
            await s.execute(
                text("""
                    INSERT INTO receipt_lines
                        (tenant_id, receipt_id, inventory_item_id, received_quantity,
                         unit_cost_cents, extracted_name, extracted_unit, confidence,
                         manually_corrected, match_status, extraction_job_id, job_attempt,
                         line_ordinal)
                    VALUES
                        (:tid, :rid, NULL, :qty,
                         :cost, :name, :unit, :conf,
                         false, 'unmatched', :jid, :att,
                         :ord)
                """),
                {
                    "tid": tenant_id,
                    "rid": receipt_id,
                    "qty": qty,
                    "cost": _to_int(raw_line.get("unit_price_cents")),
                    "name": str(raw_line.get("name", "")).strip() or None,
                    "unit": unit if is_canonical(unit) else (unit or None),
                    "conf": conf,
                    "jid": job["id"],
                    "att": job["job_attempt"],
                    "ord": ordinal,
                },
            )

        # D-606-08: aggregate = min(line confidences); NULL when zero lines, which
        # always pairs with manual_entry_required. Low aggregate also flags manual.
        agg = min(confidences) if confidences else None
        manual = agg is None or agg < _LOW_CONFIDENCE
        await s.execute(
            text("""
                UPDATE receipts SET
                    extraction_status   = 'complete',
                    extraction_confidence = :agg,
                    manual_entry_required = :manual,
                    quota_blocked       = false,
                    supplier_name       = COALESCE(:supplier, supplier_name),
                    invoice_number      = COALESCE(:invnum, invoice_number),
                    invoice_date        = COALESCE(:invdate, invoice_date),
                    subtotal_cents      = COALESCE(:sub, subtotal_cents),
                    tax_cents           = COALESCE(:tax, tax_cents),
                    total_cents         = COALESCE(:total, total_cents),
                    updated_at          = now()
                WHERE id = :rid AND tenant_id = :tid
            """),
            {
                "agg": agg,
                "manual": manual,
                "supplier": _str_or_none(payload.get("supplier_name")),
                "invnum": _str_or_none(payload.get("invoice_number")),
                "invdate": _to_date(payload.get("invoice_date")),
                "sub": _to_int(payload.get("subtotal_cents")),
                "tax": _to_int(payload.get("tax_cents")),
                "total": _to_int(payload.get("total_cents")),
                "rid": receipt_id,
                "tid": tenant_id,
            },
        )
        await self._terminal(s, jid, token, "complete", None)

    async def _charge_quota(self, s: AsyncSession, tenant_id: UUID) -> int | None:
        """Atomic UTC daily-cap upsert (D-606-23). Returns jobs_today, or None if the
        cap is reached (zero rows)."""
        row = (
            await s.execute(
                text("""
                    INSERT INTO tenant_extraction_rate_limits
                        (tenant_id, jobs_today, window_started_at)
                    VALUES (:tid, 1, (now() AT TIME ZONE 'utc')::date)
                    ON CONFLICT (tenant_id) DO UPDATE SET
                        jobs_today = CASE
                            WHEN tenant_extraction_rate_limits.window_started_at
                                 < (now() AT TIME ZONE 'utc')::date THEN 1
                            ELSE tenant_extraction_rate_limits.jobs_today + 1 END,
                        window_started_at = (now() AT TIME ZONE 'utc')::date
                    WHERE tenant_extraction_rate_limits.window_started_at
                              < (now() AT TIME ZONE 'utc')::date
                       OR tenant_extraction_rate_limits.jobs_today
                              < tenant_extraction_rate_limits.daily_cap
                    RETURNING jobs_today
                """),
                {"tid": tenant_id},
            )
        ).scalar()
        return int(row) if row is not None else None

    async def _quota_block(
        self, s: AsyncSession, jid: UUID, token: UUID, receipt_id: UUID, tenant_id: UUID
    ) -> None:
        await s.execute(
            text("""
                UPDATE receipt_extraction_jobs
                   SET status='quota_blocked',
                       quota_blocked_until = ((now() AT TIME ZONE 'utc')::date + 1)::timestamp
                                             AT TIME ZONE 'utc'
                 WHERE id=:jid AND lease_token=:tok
            """),
            {"jid": jid, "tok": token},
        )
        await s.execute(
            text(
                "UPDATE receipts SET quota_blocked=true, "
                "quota_blocked_until="
                "((now() AT TIME ZONE 'utc')::date + 1)::timestamp AT TIME ZONE 'utc', "
                "extraction_status='pending', updated_at=now() WHERE id=:rid AND tenant_id=:tid"
            ),
            {"rid": receipt_id, "tid": tenant_id},
        )

    async def _transient_fail(
        self,
        s: AsyncSession,
        jid: UUID,
        token: UUID,
        job: dict[str, Any],
        receipt_id: UUID,
        tenant_id: UUID,
        error: str,
    ) -> None:
        """Retriable failure: bump attempts; after MAX_ATTEMPTS it becomes terminal."""
        new_attempts = job["attempts"] + 1
        if new_attempts >= MAX_ATTEMPTS:
            await self._terminal(s, jid, token, "failed_terminal", error, attempts=new_attempts)
            await self._mark_receipt_failed(s, receipt_id, tenant_id)
        else:
            await s.execute(
                text(
                    "UPDATE receipt_extraction_jobs SET status='failed', attempts=:n, "
                    "last_error=:err, lease_token=NULL WHERE id=:jid AND lease_token=:tok"
                ),
                {"n": new_attempts, "err": error[:500], "jid": jid, "tok": token},
            )

    async def _supersede(
        self, s: AsyncSession, jid: UUID, token: UUID, receipt_id: UUID, tenant_id: UUID
    ) -> None:
        await self._terminal(s, jid, token, "superseded", None)
        await s.execute(
            text(
                "UPDATE receipts SET extraction_status='superseded', quota_blocked=false, "
                "updated_at=now() WHERE id=:rid AND tenant_id=:tid"
            ),
            {"rid": receipt_id, "tid": tenant_id},
        )

    async def _mark_receipt_failed(
        self, s: AsyncSession, receipt_id: UUID, tenant_id: UUID
    ) -> None:
        await s.execute(
            text(
                "UPDATE receipts SET extraction_status='failed', manual_entry_required=true, "
                "quota_blocked=false, updated_at=now() WHERE id=:rid AND tenant_id=:tid"
            ),
            {"rid": receipt_id, "tid": tenant_id},
        )

    async def _checkpoint(
        self, s: AsyncSession, jid: UUID, token: UUID, payload: dict[str, Any]
    ) -> bool:
        """Persist raw_extraction BEFORE line application (fenced). False = lost lease."""
        result = await s.execute(
            text(
                "UPDATE receipt_extraction_jobs SET raw_extraction = CAST(:p AS jsonb) "
                "WHERE id=:jid AND lease_token=:tok RETURNING id"
            ),
            {"p": json.dumps(payload), "jid": jid, "tok": token},
        )
        return result.fetchone() is not None

    async def _terminal(
        self,
        s: AsyncSession,
        jid: UUID,
        token: UUID,
        status: str,
        error: str | None,
        *,
        attempts: int | None = None,
    ) -> None:
        """Fenced terminal status write (WHERE lease_token matches)."""
        await s.execute(
            text(
                "UPDATE receipt_extraction_jobs SET status=:st, completed_at=now(), "
                "last_error=COALESCE(:err, last_error), "
                "attempts=COALESCE(:att, attempts) "
                "WHERE id=:jid AND lease_token=:tok"
            ),
            {
                "st": status,
                "err": error[:500] if error else None,
                "att": attempts,
                "jid": jid,
                "tok": token,
            },
        )


# ── lenient coercions (LLM output is untrusted data) ──────────────────────────


def _to_decimal(v: Any) -> Decimal:
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else Decimal(0)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _to_float(v: Any) -> float:
    try:
        f = float(v)
        return f if 0.0 <= f <= 1.0 else max(0.0, min(1.0, f))
    except (TypeError, ValueError):
        return 0.0


def _to_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _str_or_none(v: Any) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s or None


def _to_date(v: Any) -> date | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v)).date()
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(str(v)[:10])
        except (TypeError, ValueError):
            return None
