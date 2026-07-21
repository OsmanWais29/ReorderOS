"""Inbound email fan-out worker (Sprint 6 Phase 3b).

Owns the second half of the four-state intake lifecycle: claims 'pending' inbox
rows (webhook handoff — bytes durable, no drafts) plus stale 'processing' and
retriable 'failed' rows, creates one receipt draft per qualified attachment
(D-606-01), links each attachment row to its draft, enqueues one extraction job
per draft, and flips the row to 'complete' behind the lease fence.

Concurrency model — same claim/fence/reclaim as the extraction worker, with the
two hardening rules this codebase has already paid to learn:

  - THE FENCE IS AUTHORITATIVE (PR #4): the receiving transaction's draft
    INSERTs, attachment-link UPDATEs, and job INSERTs are unfenced siblings of
    the fenced complete-flip. If the fence misses (lease reclaimed/rotated), the
    ENTIRE transaction rolls back — a stale worker can never leave drafts behind.
  - ATTEMPTS ARE COUNTED AT CLAIM TIME, in the short committed claim transaction
    — not in the processing transaction, where an unhandled crash would roll the
    increment back and the row would reclaim forever without ever reaching
    'failed_terminal' (the known extraction-worker follow-up bug; not repeated here).

Fan-out idempotency (spec v6.11 #4): attachment rows already linked to a
receipt_id are skipped, so a crash after a partial fan-out retries only the
unlinked remainder — no duplicate drafts.

`create_drafts_and_enqueue` is the shared helper (#9 convergence): the Gmail sync
worker calls the same function — one fan-out implementation, two intake fronts.

D-606-15: logs carry ids, counts, status, and error CLASSES only.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.service_db import get_service_sessionmaker
from app.modules.receipts import repo

log = get_logger(__name__)

MAX_ATTEMPTS = 3
_LEASE_TTL_SQL = "INTERVAL '5 minutes'"

# attempts+1 happens HERE (claim tx, committed immediately) so a later crash
# cannot roll the counter back. _LEASE_TTL_SQL is a module constant — safe f-string.
_CLAIM_SQL = text(f"""
    UPDATE inbound_email_inbox
       SET processing_status = 'processing',
           locked_at = now(),
           lease_token = gen_random_uuid(),
           attempts = attempts + 1
     WHERE id = (
        SELECT id FROM inbound_email_inbox
         WHERE processing_status = 'pending'
            OR (processing_status = 'failed' AND attempts < {MAX_ATTEMPTS})
            OR (processing_status = 'processing' AND locked_at < now() - {_LEASE_TTL_SQL})
         ORDER BY created_at
         FOR UPDATE SKIP LOCKED
         LIMIT 1
     )
 RETURNING id, tenant_id, lease_token, attempts, filter_flags
""")  # noqa: S608


async def create_drafts_and_enqueue(
    s: AsyncSession,
    *,
    tenant_id: UUID,
    inbound_email_id: UUID,
    filter_flags: Any,
) -> list[UUID]:
    """Fan out one inbox row into receipt drafts + extraction jobs (shared by the
    Postmark and Gmail intake workers — #9). Idempotent: attachment rows already
    linked to a receipt are skipped. Caller owns the transaction AND the fence —
    nothing here commits."""
    atts = (
        (
            await s.execute(
                text("""
                    SELECT id, attachment_index, original_filename, mime_type, object_key
                      FROM inbound_email_attachments
                     WHERE inbound_email_id = :iid AND receipt_id IS NULL
                     ORDER BY attachment_index
                       FOR UPDATE
                """),
                {"iid": inbound_email_id},
            )
        )
        .mappings()
        .fetchall()
    )
    flags = _flags_list(filter_flags)
    created: list[UUID] = []
    for att in atts:
        # Everything set at INSERT time: service_worker's receipts UPDATE grant is
        # column-scoped (0025) and does not cover the intake columns — an UPDATE
        # here fails under the real role (caught by the phase3b e2e).
        rid = await repo.create_draft(
            s,
            tenant_id=tenant_id,
            source="email",
            photo_object_key=att["object_key"],
            original_filename=att["original_filename"],
            mime_type=att["mime_type"],
            inbound_email_id=inbound_email_id,
            filter_flags=flags,
            extraction_status="pending",
        )
        await s.execute(
            text("""
                INSERT INTO receipt_extraction_jobs (tenant_id, receipt_id, job_attempt, status)
                VALUES (:tid, :rid, 1, 'pending')
            """),
            {"tid": tenant_id, "rid": rid},
        )
        await s.execute(
            text("UPDATE inbound_email_attachments SET receipt_id = :rid WHERE id = :aid"),
            {"rid": rid, "aid": att["id"]},
        )
        created.append(rid)
    return created


def _flags_list(filter_flags: Any) -> list[str]:
    """The claim returns filter_flags as driver-dependent jsonb (str or list)."""
    import json

    if isinstance(filter_flags, str):
        parsed = json.loads(filter_flags)
        return parsed if isinstance(parsed, list) else []
    return filter_flags if isinstance(filter_flags, list) else []


class InboundEmailWorker:
    async def process_once(self) -> bool:
        """Claim and process one inbox row. Returns False when the queue is empty."""
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

    async def _process(self, claim: dict[str, Any]) -> None:
        iid, token = claim["id"], claim["lease_token"]
        tenant_id, attempts = claim["tenant_id"], claim["attempts"]
        sm = get_service_sessionmaker()
        try:
            async with sm() as s:
                # receipts/receipt_extraction_jobs RLS is tenant-predicated for
                # every role — scope the session like the extraction worker does.
                await s.execute(
                    text("SELECT set_config('app.tenant_id', :tid, true)"),
                    {"tid": str(tenant_id)},
                )
                created = await create_drafts_and_enqueue(
                    s,
                    tenant_id=tenant_id,
                    inbound_email_id=iid,
                    filter_flags=claim.get("filter_flags"),
                )
                fenced = (
                    await s.execute(
                        text("""
                            UPDATE inbound_email_inbox
                               SET processing_status = 'complete',
                                   locked_at = NULL,
                                   lease_token = NULL,
                                   last_error = NULL
                             WHERE id = :iid AND lease_token = :tok
                            RETURNING id
                        """),
                        {"iid": iid, "tok": token},
                    )
                ).fetchone()
                if fenced is None:
                    # Lost the lease: every sibling write above must die with the tx.
                    await s.rollback()
                    log.info(
                        "inbound_email_worker.lost_lease",
                        inbound_email_id=str(iid),
                        tenant_id=str(tenant_id),
                    )
                    return
                await s.commit()
            log.info(
                "inbound_email_worker.fanned_out",
                inbound_email_id=str(iid),
                tenant_id=str(tenant_id),
                drafts_created=len(created),
            )
        except Exception as exc:
            # Class only — asyncpg/SQLAlchemy exception strings embed row content.
            await self._transient_fail(iid, token, attempts, type(exc).__name__)

    async def _transient_fail(
        self, iid: UUID, token: UUID, attempts: int, error_class: str
    ) -> None:
        """Fenced failure write in a FRESH session (the processing tx is dead).
        attempts was already counted at claim time, so exhaustion is decided here
        even when the crash rolled the processing transaction back."""
        status = "failed_terminal" if attempts >= MAX_ATTEMPTS else "failed"
        sm = get_service_sessionmaker()
        async with sm() as s:
            fenced = (
                await s.execute(
                    text("""
                        UPDATE inbound_email_inbox
                           SET processing_status = :st,
                               last_error = :err,
                               locked_at = NULL,
                               lease_token = NULL
                         WHERE id = :iid AND lease_token = :tok
                        RETURNING id
                    """),
                    {"st": status, "err": error_class, "iid": iid, "tok": token},
                )
            ).fetchone()
            await s.commit()
        log.error(
            "inbound_email_worker.failed",
            inbound_email_id=str(iid),
            status=status if fenced else "lost_lease",
            attempts=attempts,
            error_class=error_class,
        )
