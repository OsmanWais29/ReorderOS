"""Materialized non-stock rows (0031 + extraction-worker apply change).

The extractor's non-item classifications (discount / credit / backorder /
fee_or_deposit) now materialize as line_type-tagged, match_status='skipped'
receipt_lines: visible in review, inert to every commit gate, cleaned up with
machine lines. Two harnesses:

  - extraction tests run the REAL ExtractionWorker under service_worker
    (admin_conn-seeded, committed rows — grants exercised);
  - commit/reset/API tests ride the bound-session harness.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core import storage
from app.core.database import engine, make_bound_session
from app.modules.inventory.services import commit_receipt
from app.modules.receipts import repo
from app.modules.receipts.extraction_llm import ExtractionResult
from app.modules.receipts.extraction_worker import ExtractionWorker
from app.modules.receipts.services import reset_extraction

pytestmark = pytest.mark.integration


# ── fakes / payloads ───────────────────────────────────────────────────────────


class FakeExtractionClient:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    async def extract_invoice(
        self, *, file_bytes: bytes, mime_type: str, repair_feedback: Any = None
    ) -> ExtractionResult:
        return ExtractionResult(
            payload=self.payload, model_version="fake-1", input_tokens=1, output_tokens=1
        )


_MIXED_INVOICE = {
    "document_type": "invoice",
    "supplier_name": "Lauzon",
    "lines": [
        {
            "name": "Milk 4x4L",
            "qty": 3,
            "unit": "CS",
            "confidence": 0.95,
            "line_total_cents": 8244,
            "line_type": "item",
        },
        {
            "name": "Volume discount",
            "confidence": 0.9,
            "line_total_cents": -500,
            "line_type": "discount",
        },
        {
            "name": "Returned cases credit",
            "confidence": 0.9,
            "line_total_cents": -1200,
            "line_type": "credit",
        },
        {"name": "Napkins (to follow)", "confidence": 0.8, "line_type": "backorder"},
        {
            "name": "Fuel surcharge",
            "confidence": 0.9,
            "line_total_cents": 350,
            "line_type": "fee_or_deposit",
        },
        {
            "name": "Butter",
            "qty": 2,
            "unit": "kg",
            "confidence": 0.92,
            "line_total_cents": 2000,
            "line_type": "item",
        },
    ],
}


def _valid_jpeg() -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, format="JPEG")
    return buf.getvalue()


async def _seed_job(admin_conn: Any) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tid, rid = uuid.uuid4(), uuid.uuid4()
    await admin_conn.execute(
        "INSERT INTO tenants (id, slug, name) VALUES ($1, $2, 'NS')", tid, f"ns-{tid.hex[:8]}"
    )
    await admin_conn.execute(
        """INSERT INTO receipts (id, tenant_id, commit_state, source, photo_object_key,
               mime_type, extraction_status)
           VALUES ($1, $2, 'draft', 'email', $3, 'image/jpeg', 'pending')""",
        rid,
        tid,
        f"receipts/{tid}/{rid}/x.jpg",
    )
    jid = await admin_conn.fetchval(
        "INSERT INTO receipt_extraction_jobs (tenant_id, receipt_id, status) "
        "VALUES ($1, $2, 'pending') RETURNING id",
        tid,
        rid,
    )
    return tid, rid, jid


async def _cleanup(admin_conn: Any, tid: uuid.UUID) -> None:
    for table in (
        "receipt_lines",
        "receipt_extraction_jobs",
        "receipts",
        "tenant_extraction_rate_limits",
        "tenants",
    ):
        col = "id" if table == "tenants" else "tenant_id"
        await admin_conn.execute(f"DELETE FROM {table} WHERE {col} = $1", tid)


@pytest.fixture(autouse=True)
async def _clean_queue(admin_conn: Any) -> AsyncIterator[None]:
    await admin_conn.execute("DELETE FROM receipt_lines WHERE extraction_job_id IS NOT NULL")
    await admin_conn.execute("DELETE FROM receipt_extraction_jobs")
    yield
    await admin_conn.execute("DELETE FROM receipt_lines WHERE extraction_job_id IS NOT NULL")
    await admin_conn.execute("DELETE FROM receipt_extraction_jobs")


# ── extraction materializes non-stock rows ─────────────────────────────────────


async def test_extraction_materializes_nonstock_rows(admin_conn: Any, monkeypatch: Any) -> None:
    tid, rid, jid = await _seed_job(admin_conn)
    monkeypatch.setattr(storage, "get_bytes", lambda key: _valid_jpeg())
    try:
        assert await ExtractionWorker(FakeExtractionClient(_MIXED_INVOICE)).process_once()
        rows = await admin_conn.fetch(
            "SELECT extracted_name, line_type, match_status, received_quantity, "
            "extracted_unit, inventory_item_id, line_total_cents, extraction_job_id, "
            "line_ordinal, adjustment_disposition "
            "FROM receipt_lines WHERE receipt_id = $1 ORDER BY line_ordinal",
            rid,
        )
        assert [r["line_type"] for r in rows] == [
            "item",
            "discount",
            "credit",
            "backorder",
            "fee_or_deposit",
            "item",
        ]
        nonstock = [r for r in rows if r["line_type"] != "item"]
        for r in nonstock:
            assert r["match_status"] == "skipped"
            assert r["received_quantity"] is None
            assert r["extracted_unit"] is None
            assert r["inventory_item_id"] is None
            assert r["extraction_job_id"] == jid  # machine row — reset cleans it
        # Signed as printed: discount/credit negative, fee positive, backorder none.
        assert [r["line_total_cents"] for r in nonstock] == [-500, -1200, None, 350]
        # Linkable rows are born NEEDING a decision (0033 commit blocker);
        # backorder/fee rows carry no disposition at all.
        assert [r["adjustment_disposition"] for r in nonstock] == [
            "pending",
            "pending",
            None,
            None,
        ]
        assert all(r["adjustment_disposition"] is None for r in rows if r["line_type"] == "item")
        # Visible non-stock rows do NOT force manual entry any more.
        manual = await admin_conn.fetchval(
            "SELECT manual_entry_required FROM receipts WHERE id = $1", rid
        )
        assert manual is False
    finally:
        await _cleanup(admin_conn, tid)


async def test_unknown_type_still_forces_manual(admin_conn: Any, monkeypatch: Any) -> None:
    payload = {
        "document_type": "invoice",
        "lines": [
            {"name": "Milk", "qty": 1, "unit": "L", "confidence": 0.9, "line_type": "item"},
            {"name": "???", "confidence": 0.5, "line_type": "mystery_row"},
        ],
    }
    tid, rid, _jid = await _seed_job(admin_conn)
    monkeypatch.setattr(storage, "get_bytes", lambda key: _valid_jpeg())
    try:
        assert await ExtractionWorker(FakeExtractionClient(payload)).process_once()
        n = await admin_conn.fetchval(
            "SELECT count(*) FROM receipt_lines WHERE receipt_id = $1", rid
        )
        assert n == 1  # the unknown row was genuinely dropped
        manual = await admin_conn.fetchval(
            "SELECT manual_entry_required FROM receipts WHERE id = $1", rid
        )
        assert manual is True  # dropped content = known-incomplete extraction
    finally:
        await _cleanup(admin_conn, tid)


# ── ops verifier invariant (committed data — verify() opens its own engine) ────


async def test_ops_verifier_nonstock_invariant(admin_conn: Any, monkeypatch: Any) -> None:
    from app.ops.verify_postmark_inbound import format_report, verify
    from tests.conftest import DB_URL_SYNC

    tid, rid, jid = await _seed_job(admin_conn)
    mid = f"pm-ns-{uuid.uuid4()}"
    iid = uuid.uuid4()
    await admin_conn.execute(
        "INSERT INTO inbound_email_inbox (id, tenant_id, postmark_message_id, source, "
        "attachment_count, processing_status) VALUES ($1,$2,$3,'postmark',1,'complete')",
        iid,
        tid,
        mid,
    )
    await admin_conn.execute("UPDATE receipts SET inbound_email_id = $2 WHERE id = $1", rid, iid)
    await admin_conn.execute(
        "INSERT INTO inbound_email_attachments (inbound_email_id, tenant_id, "
        "attachment_index, mime_type, object_key, receipt_id) "
        "VALUES ($1,$2,0,'application/pdf','k/0.pdf',$3)",
        iid,
        tid,
        rid,
    )
    await admin_conn.execute(
        "UPDATE receipt_extraction_jobs SET status='complete', raw_extraction='{}' WHERE id = $1",
        jid,
    )
    monkeypatch.setattr(storage, "get_bytes", lambda key: _valid_jpeg())
    try:
        # A properly-skipped non-stock row → PASS with the new wording.
        await admin_conn.execute(
            "INSERT INTO receipt_lines (tenant_id, receipt_id, line_type, match_status, "
            "extracted_name, line_total_cents) VALUES ($1,$2,'discount','skipped','d',-100)",
            tid,
            rid,
        )
        _out, checks = await verify(mid, DB_URL_SYNC)
        assert ("non-stock rows skipped (none receivable)", True) in checks

        # A NON-skipped non-stock row is the failure the invariant exists to catch.
        await admin_conn.execute(
            "UPDATE receipt_lines SET match_status='unmatched' "
            "WHERE receipt_id=$1 AND line_type='discount'",
            rid,
        )
        out2, checks2 = await verify(mid, DB_URL_SYNC)
        assert ("non-stock rows skipped (none receivable)", False) in checks2
        assert "OVERALL: FAIL" in format_report(out2, checks2)
    finally:
        # FK order: attachments → lines/jobs/receipts (unreferences inbox) → inbox → tenant.
        await admin_conn.execute("DELETE FROM inbound_email_attachments WHERE tenant_id=$1", tid)
        for table in ("receipt_lines", "receipt_extraction_jobs", "receipts"):
            await admin_conn.execute(f"DELETE FROM {table} WHERE tenant_id = $1", tid)
        await admin_conn.execute("DELETE FROM inbound_email_inbox WHERE tenant_id=$1", tid)
        await admin_conn.execute("DELETE FROM tenants WHERE id=$1", tid)


# ── bound-session harness: API shape, commit inertness, reset cleanup ─────────


@pytest.fixture
async def db() -> AsyncIterator[Any]:
    connection: AsyncConnection
    async with engine.connect() as connection:
        await connection.begin()
        session = make_bound_session(connection)
        try:
            yield session
        finally:
            await connection.rollback()


async def _seed_receipt(db: Any) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'NS2')"),
        {"id": tid, "s": f"ns2-{tid.hex[:8]}"},
    )
    uom = (
        await db.execute(
            text(
                "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
                "VALUES (:t, 'g', 'g', 'weight') RETURNING id"
            ),
            {"t": tid},
        )
    ).scalar_one()
    item = (
        await db.execute(
            text(
                "INSERT INTO inventory_items (tenant_id, name, inventory_mode, storage_unit_id, "
                "recipe_unit_id) VALUES (:t, 'Flour', 'recipe_deducted', :u, :u) RETURNING id"
            ),
            {"t": tid, "u": uom},
        )
    ).scalar_one()
    rid = (
        await db.execute(
            text(
                "INSERT INTO receipts (tenant_id, commit_state, source) "
                "VALUES (:t, 'draft', 'email') RETURNING id"
            ),
            {"t": tid},
        )
    ).scalar_one()
    return tid, item, rid


async def _add_nonstock(db: Any, tid: uuid.UUID, rid: uuid.UUID, *, machine: bool) -> uuid.UUID:
    jid = None
    if machine:
        jid = (
            await db.execute(
                text(
                    "INSERT INTO receipt_extraction_jobs (tenant_id, receipt_id, status) "
                    "VALUES (:t, :r, 'complete') RETURNING id"
                ),
                {"t": tid, "r": rid},
            )
        ).scalar_one()
    return (
        await db.execute(
            text("""
                INSERT INTO receipt_lines
                    (tenant_id, receipt_id, line_type, match_status, extracted_name,
                     line_total_cents, extraction_job_id, adjustment_disposition,
                     disposition_reason)
                -- DECIDED seed: this file tests non-stock visibility/inertness,
                -- not the adjustment-decision gate (which has its own tests).
                VALUES (:t, :r, 'credit', 'skipped', 'Returned cases', -1200, :j,
                        'excluded', 'test_seed')
                RETURNING id
            """),
            {"t": tid, "r": rid, "j": jid},
        )
    ).scalar_one()


async def test_get_receipt_exposes_line_type(db: Any) -> None:
    tid, _item, rid = await _seed_receipt(db)
    await _add_nonstock(db, tid, rid, machine=False)
    detail = await repo.get_receipt(db, tid, rid)
    assert detail is not None
    line = detail["lines"][0]
    assert line["line_type"] == "credit"
    assert line["match_status"] == "skipped"
    assert line["line_total_cents"] == -1200


async def test_nonstock_rows_never_block_commit_or_move_stock(db: Any) -> None:
    tid, item, rid = await _seed_receipt(db)
    await _add_nonstock(db, tid, rid, machine=False)
    await db.execute(
        text("""
            INSERT INTO receipt_lines
                (tenant_id, receipt_id, inventory_item_id, received_quantity,
                 unit_cost_cents, match_status, line_type)
            VALUES (:t, :r, :i, :q, 250, 'matched', 'item')
        """),
        {"t": tid, "r": rid, "i": item, "q": Decimal("10")},
    )
    result = await commit_receipt(
        db, tenant_id=tid, receipt_id=rid, confirm=True, reviewed_affirmation=True
    )
    assert result["status"] == "committed"
    movements = (
        await db.execute(
            text("SELECT count(*) FROM inventory_movements WHERE tenant_id = :t"), {"t": tid}
        )
    ).scalar_one()
    assert movements == 1  # ONLY the item line moved stock — the credit is inert


async def test_reset_extraction_removes_machine_nonstock_rows(db: Any) -> None:
    tid, _item, rid = await _seed_receipt(db)
    await _add_nonstock(db, tid, rid, machine=True)
    n_before = (
        await db.execute(
            text("SELECT count(*) FROM receipt_lines WHERE receipt_id = :r"), {"r": rid}
        )
    ).scalar_one()
    assert n_before == 1
    await reset_extraction(db, tenant_id=tid, receipt_id=rid, discard_edits=True)
    n_after = (
        await db.execute(
            text("SELECT count(*) FROM receipt_lines WHERE receipt_id = :r"), {"r": rid}
        )
    ).scalar_one()
    assert n_after == 0
