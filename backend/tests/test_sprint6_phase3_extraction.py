"""Sprint 6 S3 — extraction worker behavior, under the REAL service_worker role.

Runs the actual ExtractionWorker (which connects via get_service_sessionmaker =
service_worker), so it exercises the 0025 grants — a superuser unit test would mask
a missing grant (Sprint 5 keystone lesson). Seeds are committed via admin_conn and
torn down by tenant CASCADE; storage + LLM are injected (no network).

Covers: happy path (lines + min-confidence aggregate), document_type='not_invoice'
→ receipt-level suppression, the review_started_at hard-stop → superseded, terminal
validation failure → failed_terminal + receipt failed/manual, quota cap →
quota_blocked, low-confidence → manual_entry_required, and the raw_extraction
checkpoint reuse (a retry does not call the LLM again).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core import storage
from app.modules.receipts import extraction_worker as ew_mod
from app.modules.receipts.extraction_llm import ExtractionResult, ExtractionUnavailable
from app.modules.receipts.extraction_worker import ExtractionWorker

pytestmark = pytest.mark.integration


class FakeExtractionClient:
    def __init__(
        self, payload: dict[str, Any] | None = None, *, raise_exc: Exception | None = None
    ):
        self.payload = payload or {}
        self.raise_exc = raise_exc
        self.calls = 0

    async def extract_invoice(
        self, *, file_bytes: bytes, mime_type: str, repair_feedback: Any = None
    ) -> ExtractionResult:
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return ExtractionResult(
            payload=self.payload, model_version="fake-1", input_tokens=10, output_tokens=20
        )


_INVOICE = {
    "document_type": "invoice",
    "supplier_name": "Sysco",
    "invoice_number": "INV-1",
    "lines": [
        {"name": "Flour", "qty": 5, "unit": "kg", "unit_price_cents": 250, "confidence": 0.9},
        {"name": "Sugar", "qty": 2, "unit": "kg", "unit_price_cents": 180, "confidence": 0.6},
    ],
}


@pytest.fixture(autouse=True)
async def _clean_jobs(admin_conn: Any) -> AsyncIterator[None]:
    """Clean the jobs queue so process_once() deterministically claims our seeded job."""
    await admin_conn.execute("DELETE FROM receipt_lines WHERE extraction_job_id IS NOT NULL")
    await admin_conn.execute("DELETE FROM receipt_extraction_jobs")
    yield
    await admin_conn.execute("DELETE FROM receipt_lines WHERE extraction_job_id IS NOT NULL")
    await admin_conn.execute("DELETE FROM receipt_extraction_jobs")


async def _seed(
    admin_conn: Any,
    *,
    review_started: bool = False,
    daily_cap: int | None = None,
    jobs_today: int = 0,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tid = uuid.uuid4()
    rid = uuid.uuid4()
    await admin_conn.execute(
        "INSERT INTO tenants (id, slug, name) VALUES ($1, $2, 'S3 Extract')",
        tid,
        f"s3-{tid.hex[:8]}",
    )
    await admin_conn.execute(
        """INSERT INTO receipts (id, tenant_id, commit_state, source, photo_object_key,
               mime_type, extraction_status, review_started_at)
           VALUES ($1, $2, 'draft', 'mobile_photo', $3, 'image/jpeg', 'pending', $4)""",
        rid,
        tid,
        f"receipts/{tid}/{rid}/x.jpg",
        datetime.now(UTC) if review_started else None,
    )
    if daily_cap is not None:
        await admin_conn.execute(
            "INSERT INTO tenant_extraction_rate_limits (tenant_id, daily_cap, jobs_today, window_started_at) "
            "VALUES ($1, $2, $3, (now() AT TIME ZONE 'utc')::date)",
            tid,
            daily_cap,
            jobs_today,
        )
    jid = await admin_conn.fetchval(
        "INSERT INTO receipt_extraction_jobs (tenant_id, receipt_id, status) "
        "VALUES ($1, $2, 'pending') RETURNING id",
        tid,
        rid,
    )
    return tid, rid, jid


def _valid_jpeg() -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, format="JPEG")
    return buf.getvalue()


async def _run(
    admin_conn: Any, monkeypatch: Any, fake: FakeExtractionClient, raw: bytes | None = None
) -> bool:
    monkeypatch.setattr(storage, "get_bytes", lambda key: raw if raw is not None else _valid_jpeg())
    return await ExtractionWorker(fake).process_once()


async def _cleanup(admin_conn: Any, tid: uuid.UUID) -> None:
    await admin_conn.execute("DELETE FROM receipt_lines WHERE tenant_id = $1", tid)
    await admin_conn.execute("DELETE FROM receipt_extraction_jobs WHERE tenant_id = $1", tid)
    await admin_conn.execute("DELETE FROM tenant_extraction_rate_limits WHERE tenant_id = $1", tid)
    await admin_conn.execute("DELETE FROM receipts WHERE tenant_id = $1", tid)
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tid)


async def test_happy_path_writes_lines_and_min_confidence(
    admin_conn: Any, monkeypatch: Any
) -> None:
    tid, rid, jid = await _seed(admin_conn)
    try:
        assert await _run(admin_conn, monkeypatch, FakeExtractionClient(_INVOICE)) is True
        job_status = await admin_conn.fetchval(
            "SELECT status FROM receipt_extraction_jobs WHERE id=$1", jid
        )
        assert job_status == "complete"
        rec = await admin_conn.fetchrow(
            "SELECT extraction_status, extraction_confidence, manual_entry_required, supplier_name "
            "FROM receipts WHERE id=$1",
            rid,
        )
        assert rec["extraction_status"] == "complete"
        assert float(rec["extraction_confidence"]) == pytest.approx(0.6)  # min(0.9, 0.6)
        assert rec["manual_entry_required"] is False  # 0.6 >= 0.4
        assert rec["supplier_name"] == "Sysco"
        lines = await admin_conn.fetch(
            "SELECT extracted_name, extracted_unit, received_quantity, unit_cost_cents, match_status, "
            "extraction_job_id, line_ordinal FROM receipt_lines WHERE receipt_id=$1 ORDER BY line_ordinal",
            rid,
        )
        assert [r["extracted_name"] for r in lines] == ["Flour", "Sugar"]
        assert [r["extracted_unit"] for r in lines] == ["kg", "kg"]
        assert all(
            r["match_status"] == "unmatched" and r["extraction_job_id"] == jid for r in lines
        )
    finally:
        await _cleanup(admin_conn, tid)


async def test_not_invoice_suppresses_receipt(admin_conn: Any, monkeypatch: Any) -> None:
    tid, rid, jid = await _seed(admin_conn)
    try:
        await _run(
            admin_conn,
            monkeypatch,
            FakeExtractionClient({"document_type": "not_invoice", "lines": []}),
        )
        rec = await admin_conn.fetchrow(
            "SELECT review_visibility_status, suppression_reason, manual_entry_required, extraction_status "
            "FROM receipts WHERE id=$1",
            rid,
        )
        assert rec["review_visibility_status"] == "suppressed"
        assert rec["suppression_reason"] == "not_invoice"
        assert rec["manual_entry_required"] is False  # not_invoice is NOT operator work
        n = await admin_conn.fetchval("SELECT count(*) FROM receipt_lines WHERE receipt_id=$1", rid)
        assert n == 0
        assert (
            await admin_conn.fetchval("SELECT status FROM receipt_extraction_jobs WHERE id=$1", jid)
            == "complete"
        )
    finally:
        await _cleanup(admin_conn, tid)


async def test_review_started_supersedes(admin_conn: Any, monkeypatch: Any) -> None:
    tid, rid, jid = await _seed(admin_conn, review_started=True)
    fake = FakeExtractionClient(_INVOICE)
    try:
        await _run(admin_conn, monkeypatch, fake)
        assert (
            await admin_conn.fetchval("SELECT status FROM receipt_extraction_jobs WHERE id=$1", jid)
            == "superseded"
        )
        assert (
            await admin_conn.fetchval("SELECT extraction_status FROM receipts WHERE id=$1", rid)
            == "superseded"
        )
        n = await admin_conn.fetchval("SELECT count(*) FROM receipt_lines WHERE receipt_id=$1", rid)
        assert n == 0
        assert fake.calls == 0  # hard-stop is BEFORE the LLM call — no spend
    finally:
        await _cleanup(admin_conn, tid)


async def test_validation_failure_is_terminal(admin_conn: Any, monkeypatch: Any) -> None:
    tid, rid, jid = await _seed(admin_conn)
    fake = FakeExtractionClient(_INVOICE)
    try:
        # storage returns a non-image (validation fails on re-read)
        await _run(admin_conn, monkeypatch, fake, raw=b"not-an-image-at-all")
        assert (
            await admin_conn.fetchval("SELECT status FROM receipt_extraction_jobs WHERE id=$1", jid)
            == "failed_terminal"
        )
        rec = await admin_conn.fetchrow(
            "SELECT extraction_status, manual_entry_required FROM receipts WHERE id=$1", rid
        )
        assert rec["extraction_status"] == "failed"
        assert rec["manual_entry_required"] is True
        assert fake.calls == 0  # validation precedes the LLM
        # no quota consumed by a validation failure
        charged = await admin_conn.fetchval(
            "SELECT count(*) FROM tenant_extraction_rate_limits WHERE tenant_id=$1", tid
        )
        assert charged == 0
    finally:
        await _cleanup(admin_conn, tid)


async def test_quota_cap_blocks(admin_conn: Any, monkeypatch: Any) -> None:
    tid, rid, jid = await _seed(admin_conn, daily_cap=2, jobs_today=2)  # already at cap
    fake = FakeExtractionClient(_INVOICE)
    try:
        await _run(admin_conn, monkeypatch, fake)
        assert (
            await admin_conn.fetchval("SELECT status FROM receipt_extraction_jobs WHERE id=$1", jid)
            == "quota_blocked"
        )
        rec = await admin_conn.fetchrow(
            "SELECT quota_blocked, quota_blocked_until FROM receipts WHERE id=$1", rid
        )
        assert rec["quota_blocked"] is True
        assert rec["quota_blocked_until"] is not None
        assert fake.calls == 0  # capped before the LLM
    finally:
        await _cleanup(admin_conn, tid)


async def test_low_confidence_flags_manual(admin_conn: Any, monkeypatch: Any) -> None:
    tid, rid, _jid = await _seed(admin_conn)
    low = {
        "document_type": "invoice",
        "lines": [{"name": "Mystery", "qty": 1, "unit": "kg", "confidence": 0.2}],
    }
    try:
        await _run(admin_conn, monkeypatch, FakeExtractionClient(low))
        rec = await admin_conn.fetchrow(
            "SELECT extraction_confidence, manual_entry_required FROM receipts WHERE id=$1", rid
        )
        assert float(rec["extraction_confidence"]) == pytest.approx(0.2)
        assert rec["manual_entry_required"] is True  # 0.2 < 0.4
    finally:
        await _cleanup(admin_conn, tid)


async def test_checkpoint_reuse_skips_second_llm_call(admin_conn: Any, monkeypatch: Any) -> None:
    """A job whose raw_extraction is already checkpointed must NOT call the LLM again
    (crash-after-LLM-before-lines must not pay twice)."""
    tid, rid, jid = await _seed(admin_conn)
    import json

    await admin_conn.execute(
        "UPDATE receipt_extraction_jobs SET raw_extraction = $1::jsonb WHERE id=$2",
        json.dumps(_INVOICE),
        jid,
    )
    fake = FakeExtractionClient(raise_exc=ExtractionUnavailable("should not be called"))
    try:
        await _run(admin_conn, monkeypatch, fake)
        assert fake.calls == 0  # reused the checkpoint
        assert (
            await admin_conn.fetchval("SELECT status FROM receipt_extraction_jobs WHERE id=$1", jid)
            == "complete"
        )
        n = await admin_conn.fetchval("SELECT count(*) FROM receipt_lines WHERE receipt_id=$1", rid)
        assert n == 2
    finally:
        await _cleanup(admin_conn, tid)


def test_spend_hook_is_documented_disabled() -> None:
    """The spec's spend kill switch doesn't exist; the hook is an explicit no-op."""
    assert ew_mod._SPEND_HOOK is False


# ── extraction LLM module (no network) ────────────────────────────────────────


def test_tool_schema_constrains_units_and_requires_document_type() -> None:
    from app.modules.inventory.depletion.units import CANONICAL_UNITS
    from app.modules.receipts.extraction_llm import tool_schema

    schema = tool_schema()
    props = schema["input_schema"]["properties"]
    assert "document_type" in schema["input_schema"]["required"]
    assert props["document_type"]["enum"] == ["invoice", "not_invoice"]
    unit_enum = props["lines"]["items"]["properties"]["unit"]["enum"]
    assert set(unit_enum) == set(CANONICAL_UNITS)


def test_content_block_image_vs_pdf() -> None:
    from app.modules.receipts.extraction_llm import _content_block

    img = _content_block(b"\xff\xd8\xff", "image/jpeg")
    assert img["type"] == "image" and img["source"]["media_type"] == "image/jpeg"
    pdf = _content_block(b"%PDF-", "application/pdf")
    assert pdf["type"] == "document" and pdf["source"]["media_type"] == "application/pdf"


# ── invalid-qty lines (smoke-test regression 2026-07-14) ──────────────────────
# The live smoke test hit a receipt where the LLM returned a qty=0 line
# (a deposit/zero row) at high confidence: the INSERT violated
# receipt_lines_qty_positive and rolled back EVERY line. One bad line must
# never discard the good ones, and the skip must leak no content (D-606-15).


async def test_invalid_qty_line_skipped_valid_lines_applied(
    admin_conn: Any, monkeypatch: Any
) -> None:
    from structlog.testing import capture_logs

    payload = {
        "document_type": "invoice",
        "lines": [
            {
                "name": "ZERO-QTY-SENTINEL-DEPOSIT",
                "qty": 0,
                "unit": "ea",
                "unit_price_cents": 1140,
                "confidence": 0.99,
            },
            {"name": "Flour", "qty": 5, "unit": "kg", "unit_price_cents": 250, "confidence": 0.9},
        ],
    }
    tid, rid, jid = await _seed(admin_conn)
    try:
        with capture_logs() as logs:
            assert await _run(admin_conn, monkeypatch, FakeExtractionClient(payload)) is True

        # The job completes and the VALID line is applied at its original ordinal.
        assert (
            await admin_conn.fetchval("SELECT status FROM receipt_extraction_jobs WHERE id=$1", jid)
            == "complete"
        )
        lines = await admin_conn.fetch(
            "SELECT extracted_name, line_ordinal FROM receipt_lines WHERE receipt_id=$1", rid
        )
        assert [(r["extracted_name"], r["line_ordinal"]) for r in lines] == [("Flour", 1)]

        # Extraction is known-incomplete → manual flag; confidence from applied lines only.
        rec = await admin_conn.fetchrow(
            "SELECT extraction_status, extraction_confidence, manual_entry_required "
            "FROM receipts WHERE id=$1",
            rid,
        )
        assert rec["extraction_status"] == "complete"
        assert float(rec["extraction_confidence"]) == pytest.approx(0.9)
        assert rec["manual_entry_required"] is True

        # Content-free telemetry: the skip is COUNTED, the line text never logged.
        skip_events = [e for e in logs if e.get("lines_skipped_invalid_qty")]
        assert len(skip_events) == 1
        assert skip_events[0]["lines_skipped_invalid_qty"] == 1
        assert skip_events[0]["lines_applied"] == 1
        assert "ZERO-QTY-SENTINEL-DEPOSIT" not in str(logs)
    finally:
        await _cleanup(admin_conn, tid)


async def test_all_lines_invalid_qty_completes_with_manual_flag(
    admin_conn: Any, monkeypatch: Any
) -> None:
    payload = {
        "document_type": "invoice",
        "lines": [{"name": "Deposit", "qty": 0, "unit": "ea", "confidence": 0.9}],
    }
    tid, rid, jid = await _seed(admin_conn)
    try:
        assert await _run(admin_conn, monkeypatch, FakeExtractionClient(payload)) is True
        assert (
            await admin_conn.fetchval("SELECT status FROM receipt_extraction_jobs WHERE id=$1", jid)
            == "complete"
        )
        n = await admin_conn.fetchval("SELECT count(*) FROM receipt_lines WHERE receipt_id=$1", rid)
        assert n == 0
        rec = await admin_conn.fetchrow(
            "SELECT extraction_confidence, manual_entry_required FROM receipts WHERE id=$1", rid
        )
        assert rec["extraction_confidence"] is None  # no applied lines
        assert rec["manual_entry_required"] is True
    finally:
        await _cleanup(admin_conn, tid)


def test_tool_schema_forbids_nonpositive_qty() -> None:
    from app.modules.receipts.extraction_llm import tool_schema

    qty = tool_schema()["input_schema"]["properties"]["lines"]["items"]["properties"]["qty"]
    assert qty["exclusiveMinimum"] == 0
