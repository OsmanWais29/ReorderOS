"""Sprint 6 Phase 3b — Postmark inbound webhook + inbound email fan-out worker.

Runs the REAL service_worker role end to end (webhook and worker both connect via
get_service_sessionmaker), so the 0030 INSERT grant and the RLS surfaces are
exercised — a superuser-only test would mask a missing grant (Sprint 5 keystone
lesson). Storage is injected; no network.

Covers the Phase 3b exit-gate slice shipped in this PR: Basic Auth, MailboxHash
routing + tenant isolation, unknown-token no-byte-work + alert + replay dedup,
reservation dedup on MessageID, per-attachment qualification (unsupported types
create no object and no draft), spam gate, four-state lifecycle ownership
(webhook→pending, worker→complete), deterministic multi-attachment fan-out with
retry idempotency, transient-vs-terminal failure semantics with claim-time attempt
counting → failed_terminal, the authoritative-fence rollback (PR #4 rule), and the
D-606-15 no-content-in-logs gate.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

import pytest
import structlog.testing
from httpx import AsyncClient

from app.core import storage
from app.core.config import get_settings
from app.modules.receipts import inbound_email_worker as worker_mod
from app.modules.receipts.inbound_email_worker import InboundEmailWorker

pytestmark = pytest.mark.integration

_USER, _PASS = "pm-test-user", "pm-test-password"
_AUTH = {"Authorization": "Basic " + base64.b64encode(f"{_USER}:{_PASS}".encode()).decode()}
_URL = "/api/v1/webhooks/inbound/postmark"

# ≥10 KB (Layer 3 lower bound), single PDF signature, no foreign signatures.
# (Not pypdf-parseable — exercises the LENIENT pass-through of the page check.)
_PDF_BYTES = b"%PDF-1.4\n" + b"A" * 11_000
_DOCX_BYTES = b"PK\x03\x04" + b"B" * 11_000  # zip container → unsupported
_CSV_BYTES = b"item,qty,price\nmilk,2,4.99\n" * 500  # text → unsupported


def _real_pdf(pages: int = 1, pad_to: int = 11_000) -> bytes:
    """A genuine parseable PDF with `pages` blank pages, space-padded past the
    10 KB floor (or to an arbitrary size — padding carries no foreign container
    signatures, so the polyglot scan stays clean)."""
    from io import BytesIO

    from pypdf import PdfWriter

    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=72, height=72)
    buf = BytesIO()
    w.write(buf)
    data = buf.getvalue()
    if len(data) < pad_to:
        data += b" " * (pad_to - len(data))
    return data


@pytest.fixture(autouse=True)
def _postmark_on(monkeypatch: Any) -> Any:
    monkeypatch.setenv("POSTMARK_INBOUND_ENABLED", "true")
    monkeypatch.setenv("POSTMARK_WEBHOOK_USER", _USER)
    monkeypatch.setenv("POSTMARK_WEBHOOK_PASSWORD", _PASS)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeSpaces:
    """put_bytes recorder; optionally fails to simulate a storage outage."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail = False

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        if self.fail:
            raise RuntimeError("simulated storage outage")
        self.objects[key] = data


@pytest.fixture
def spaces(monkeypatch: Any) -> FakeSpaces:
    fake = FakeSpaces()
    monkeypatch.setattr(storage, "put_bytes", fake.put)
    return fake


async def _seed_tenant(admin_conn: Any, token: str) -> uuid.UUID:
    tid = uuid.uuid4()
    await admin_conn.execute(
        "INSERT INTO tenants (id, slug, name) VALUES ($1, $2, 'P3b')",
        tid,
        f"p3b-{tid.hex[:8]}",
    )
    await admin_conn.execute(
        "INSERT INTO tenant_inbound_email_tokens (tenant_id, token) VALUES ($1, $2)",
        tid,
        token,
    )
    return tid


async def _cleanup(admin_conn: Any, *tids: uuid.UUID) -> None:
    for tid in tids:
        await admin_conn.execute("DELETE FROM receipt_extraction_jobs WHERE tenant_id = $1", tid)
        await admin_conn.execute("DELETE FROM inbound_email_attachments WHERE tenant_id = $1", tid)
        await admin_conn.execute("DELETE FROM receipts WHERE tenant_id = $1", tid)
        await admin_conn.execute("DELETE FROM inbound_email_inbox WHERE tenant_id = $1", tid)
        await admin_conn.execute(
            "DELETE FROM tenant_inbound_email_tokens WHERE tenant_id = $1", tid
        )
        await admin_conn.execute("DELETE FROM tenant_invoice_senders WHERE tenant_id = $1", tid)
        await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tid)
    await admin_conn.execute(
        "DELETE FROM monitoring_alerts WHERE monitor_name = 'postmark_unknown_token'"
    )
    await admin_conn.execute("DELETE FROM inbound_email_inbox WHERE tenant_id IS NULL")


def _payload(
    message_id: str,
    token: str,
    attachments: list[tuple[str, bytes, str]] | None = None,
    *,
    html_body: str | None = None,
    spam_score: float | None = None,
    sender: str = "orders@lauzon.example",
) -> dict[str, Any]:
    p: dict[str, Any] = {
        "MessageID": message_id,
        "MailboxHash": token,
        "From": sender,
        "Subject": "Invoice",
        "Date": "Tue, 15 Jul 2026 12:00:00 -0400",
        "Attachments": [
            {
                "Name": name,
                "Content": base64.b64encode(data).decode(),
                "ContentType": ctype,
                "ContentLength": len(data),
            }
            for name, data, ctype in (attachments or [])
        ],
    }
    if html_body is not None:
        p["HtmlBody"] = html_body
    if spam_score is not None:
        p["Headers"] = [{"Name": "X-Spam-Score", "Value": str(spam_score)}]
    return p


def _with_recipients(
    p: dict[str, Any],
    *,
    to: list[str | None] | None = None,
    cc: list[str | None] | None = None,
    top_level: str | None = None,
) -> dict[str, Any]:
    """Attach ToFull/CcFull recipient entries (MailboxHash per entry; None =
    unrelated recipient with no hash) and optionally override the top-level
    MailboxHash — Postmark's top-level field reflects only one recipient."""
    out = dict(p)
    if top_level is not None:
        out["MailboxHash"] = top_level
    if to is not None:
        out["ToFull"] = [
            {"Email": f"r{i}@x.example", "MailboxHash": h or ""} for i, h in enumerate(to)
        ]
    if cc is not None:
        out["CcFull"] = [
            {"Email": f"c{i}@x.example", "MailboxHash": h or ""} for i, h in enumerate(cc)
        ]
    return out


async def _inbox_row(admin_conn: Any, message_id: str) -> Any:
    return await admin_conn.fetchrow(
        "SELECT * FROM inbound_email_inbox WHERE postmark_message_id = $1", message_id
    )


# ── auth / enablement ─────────────────────────────────────────────────────────


async def test_auth_missing_and_wrong_rejected(client: AsyncClient) -> None:
    r = await client.post(_URL, json={"MessageID": "x"})
    assert r.status_code == 401
    bad = {"Authorization": "Basic " + base64.b64encode(b"pm-test-user:wrong").decode()}
    r = await client.post(_URL, json={"MessageID": "x"}, headers=bad)
    assert r.status_code == 401


async def test_disabled_channel_503(client: AsyncClient, monkeypatch: Any) -> None:
    monkeypatch.setenv("POSTMARK_INBOUND_ENABLED", "false")
    get_settings.cache_clear()
    r = await client.post(_URL, json={"MessageID": "x"}, headers=_AUTH)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "POSTMARK_INBOUND_DISABLED"


async def test_disabled_worker_runner_exits_cleanly(monkeypatch: Any) -> None:
    """Clover inbox_worker pattern: channel off → the runner logs .disabled and
    returns (exit 0) without touching the queue."""
    monkeypatch.setenv("POSTMARK_INBOUND_ENABLED", "false")
    get_settings.cache_clear()
    from app.workers import inbound_email_worker as runner

    # configure_logging() would re-configure structlog and defeat capture_logs.
    monkeypatch.setattr(runner, "configure_logging", lambda: None)
    with structlog.testing.capture_logs() as logs:
        await runner._main()  # returns immediately — no loop, no DB
    assert any(e.get("event") == "inbound_email_worker.disabled" for e in logs)


# ── happy path + lifecycle ownership ──────────────────────────────────────────


async def test_one_pdf_creates_one_draft_via_worker(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    try:
        r = await client.post(
            _URL,
            json=_payload(mid, token, [("inv.pdf", _PDF_BYTES, "application/pdf")]),
            headers=_AUTH,
        )
        assert r.status_code == 200
        assert r.json() == {"status": "pending", "attachments_qualified": 1}

        row = await _inbox_row(admin_conn, mid)
        # Webhook owns receiving→pending and STOPS there (no drafts yet).
        assert row["processing_status"] == "pending"
        assert row["tenant_id"] == tid
        key = f"receipts/{tid}/inbound/postmark/{row['id']}/0.pdf"
        assert list(spaces.objects) == [key]
        att = await admin_conn.fetchrow(
            "SELECT * FROM inbound_email_attachments WHERE inbound_email_id = $1", row["id"]
        )
        assert att["object_key"] == key and att["receipt_id"] is None

        # Worker owns processing→complete: one draft, linked + enqueued.
        assert await InboundEmailWorker().process_once() is True
        row = await _inbox_row(admin_conn, mid)
        assert row["processing_status"] == "complete"
        rec = await admin_conn.fetchrow("SELECT * FROM receipts WHERE tenant_id = $1", tid)
        assert rec["source"] == "email"
        assert rec["photo_object_key"] == key
        assert rec["inbound_email_id"] == row["id"]
        assert rec["extraction_status"] == "pending"
        att = await admin_conn.fetchrow(
            "SELECT receipt_id FROM inbound_email_attachments WHERE inbound_email_id = $1",
            row["id"],
        )
        assert att["receipt_id"] == rec["id"]
        jobs = await admin_conn.fetchval(
            "SELECT count(*) FROM receipt_extraction_jobs WHERE receipt_id = $1", rec["id"]
        )
        assert jobs == 1
    finally:
        await _cleanup(admin_conn, tid)


async def test_multi_attachment_fan_out_deterministic_and_idempotent(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    try:
        atts = [
            ("a.pdf", _PDF_BYTES, "application/pdf"),
            ("junk.docx", _DOCX_BYTES, "application/vnd.openxmlformats"),
            ("b.pdf", _PDF_BYTES + b"B", "application/pdf"),
        ]
        r = await client.post(_URL, json=_payload(mid, token, atts), headers=_AUTH)
        assert r.json() == {"status": "pending", "attachments_qualified": 2}
        row = await _inbox_row(admin_conn, mid)
        # Qualified rows keep their ORIGINAL payload indexes (0 and 2) — the docx hole stays.
        idxs = [
            r["attachment_index"]
            for r in await admin_conn.fetch(
                "SELECT attachment_index FROM inbound_email_attachments "
                "WHERE inbound_email_id = $1 ORDER BY attachment_index",
                row["id"],
            )
        ]
        assert idxs == [0, 2]
        assert json.loads(row["filter_flags"]) == ["attachment_1:RECEIPT_UNSUPPORTED_TYPE"]

        assert await InboundEmailWorker().process_once() is True
        n = await admin_conn.fetchval("SELECT count(*) FROM receipts WHERE tenant_id = $1", tid)
        assert n == 2
        # Idempotency: nothing left to claim; a second pass creates nothing.
        assert await InboundEmailWorker().process_once() is False
        n = await admin_conn.fetchval("SELECT count(*) FROM receipts WHERE tenant_id = $1", tid)
        assert n == 2
    finally:
        await _cleanup(admin_conn, tid)


# ── multi-page / size policy (real supplier invoices) ────────────────────────


async def test_multipage_pdf_is_exactly_one_receipt(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    """One 5-page PDF = one attachment row = ONE draft (never one per page):
    the whole PDF travels as a single Anthropic document block downstream."""
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    try:
        r = await client.post(
            _URL,
            json=_payload(mid, token, [("inv5p.pdf", _real_pdf(pages=5), "application/pdf")]),
            headers=_AUTH,
        )
        assert r.json() == {"status": "pending", "attachments_qualified": 1}
        assert await InboundEmailWorker().process_once() is True
        n_receipts = await admin_conn.fetchval(
            "SELECT count(*) FROM receipts WHERE tenant_id = $1", tid
        )
        n_jobs = await admin_conn.fetchval(
            "SELECT count(*) FROM receipt_extraction_jobs WHERE tenant_id = $1", tid
        )
        assert (n_receipts, n_jobs) == (1, 1)
    finally:
        await _cleanup(admin_conn, tid)


async def test_large_pdf_under_cap_accepted(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    """12 MB — over the old 10 MB Layer-3 ceiling, under the 20 MB provider-derived
    cap — must qualify and upload intact."""
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    big = _real_pdf(pages=3, pad_to=12 * 1024 * 1024)
    try:
        r = await client.post(
            _URL, json=_payload(mid, token, [("big.pdf", big, "application/pdf")]), headers=_AUTH
        )
        assert r.json() == {"status": "pending", "attachments_qualified": 1}
        assert len(next(iter(spaces.objects.values()))) == len(big)
    finally:
        await _cleanup(admin_conn, tid)


async def test_oversized_pdf_terminal_and_never_uploaded(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    oversized = _real_pdf(pages=1, pad_to=21 * 1024 * 1024)  # > 20 MB cap
    try:
        r = await client.post(
            _URL,
            json=_payload(mid, token, [("huge.pdf", oversized, "application/pdf")]),
            headers=_AUTH,
        )
        assert r.json() == {"status": "filtered_out", "attachments_qualified": 0}
        assert spaces.objects == {}  # never reached Spaces
        row = await _inbox_row(admin_conn, mid)
        assert "attachment_0:INBOUND_ATTACHMENT_TOO_LARGE" in json.loads(row["filter_flags"])
        # Terminal: worker has nothing to claim; not resurrectable by retry.
        assert await InboundEmailWorker().process_once() is False
    finally:
        await _cleanup(admin_conn, tid)


async def test_small_real_pdf_qualifies_and_creates_draft(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    """Live-test finding: a real 4.3 KB digital invoice was filtered by the old
    10 KB blanket floor. PDFs get a 1 KB sanity floor only."""
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    small_pdf = _real_pdf(pages=1, pad_to=4_400)  # ≈4.3 KB, under the old floor
    try:
        r = await client.post(
            _URL,
            json=_payload(mid, token, [("inv.pdf", small_pdf, "application/pdf")]),
            headers=_AUTH,
        )
        assert r.json() == {"status": "pending", "attachments_qualified": 1}
        assert await InboundEmailWorker().process_once() is True
        n = await admin_conn.fetchval("SELECT count(*) FROM receipts WHERE tenant_id = $1", tid)
        assert n == 1
    finally:
        await _cleanup(admin_conn, tid)


async def test_tiny_image_still_filtered(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    """The 10 KB floor stays for JPEG/PNG — tiny images are logos/signatures."""
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (16, 16), (200, 10, 10)).save(buf, format="JPEG")
    tiny_jpeg = buf.getvalue()
    assert len(tiny_jpeg) < 10 * 1024
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    try:
        r = await client.post(
            _URL,
            json=_payload(mid, token, [("logo.jpg", tiny_jpeg, "image/jpeg")]),
            headers=_AUTH,
        )
        assert r.json() == {"status": "filtered_out", "attachments_qualified": 0}
        assert spaces.objects == {}
        row = await _inbox_row(admin_conn, mid)
        assert "attachment_0:INBOUND_ATTACHMENT_TOO_SMALL" in json.loads(row["filter_flags"])
    finally:
        await _cleanup(admin_conn, tid)


async def test_empty_pdf_rejected(client: AsyncClient, admin_conn: Any, spaces: FakeSpaces) -> None:
    """Zero/near-zero bytes fail the 1 KB PDF sanity floor."""
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    try:
        r = await client.post(
            _URL,
            json=_payload(mid, token, [("empty.pdf", b"%PDF-", "application/pdf")]),
            headers=_AUTH,
        )
        assert r.json() == {"status": "filtered_out", "attachments_qualified": 0}
        assert spaces.objects == {}
        row = await _inbox_row(admin_conn, mid)
        assert "attachment_0:INBOUND_ATTACHMENT_TOO_SMALL" in json.loads(row["filter_flags"])
    finally:
        await _cleanup(admin_conn, tid)


async def test_zero_page_pdf_unreadable_terminal(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    """A structurally valid PDF that pypdf POSITIVELY reads as zero pages is
    terminally RECEIPT_PDF_UNREADABLE (unparseable PDFs pass through instead)."""
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    zero_page = _real_pdf(pages=0, pad_to=2_048)
    try:
        r = await client.post(
            _URL,
            json=_payload(mid, token, [("hollow.pdf", zero_page, "application/pdf")]),
            headers=_AUTH,
        )
        assert r.json() == {"status": "filtered_out", "attachments_qualified": 0}
        assert spaces.objects == {}
        row = await _inbox_row(admin_conn, mid)
        assert "attachment_0:RECEIPT_PDF_UNREADABLE" in json.loads(row["filter_flags"])
    finally:
        await _cleanup(admin_conn, tid)


async def test_pdf_over_page_cap_rejected_cleanly(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    """101 pages exceeds the Anthropic extraction cap — clean terminal reason
    instead of three doomed extraction attempts downstream."""
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    try:
        r = await client.post(
            _URL,
            json=_payload(mid, token, [("novel.pdf", _real_pdf(pages=101), "application/pdf")]),
            headers=_AUTH,
        )
        assert r.json() == {"status": "filtered_out", "attachments_qualified": 0}
        assert spaces.objects == {}
        row = await _inbox_row(admin_conn, mid)
        assert "attachment_0:RECEIPT_TOO_MANY_PAGES" in json.loads(row["filter_flags"])
    finally:
        await _cleanup(admin_conn, tid)


async def test_two_pdfs_two_separate_drafts_multipage_never_splits(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    """Two attachments → two drafts (D-606-01); pages never multiply drafts."""
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    try:
        atts = [
            ("a.pdf", _real_pdf(pages=4), "application/pdf"),
            ("b.pdf", _real_pdf(pages=2), "application/pdf"),
        ]
        r = await client.post(_URL, json=_payload(mid, token, atts), headers=_AUTH)
        assert r.json() == {"status": "pending", "attachments_qualified": 2}
        assert await InboundEmailWorker().process_once() is True
        n = await admin_conn.fetchval("SELECT count(*) FROM receipts WHERE tenant_id = $1", tid)
        assert n == 2  # one per ATTACHMENT — 6 total pages never became 6 drafts
    finally:
        await _cleanup(admin_conn, tid)


def test_multipage_dedup_instruction_pinned() -> None:
    """Repeated headers/footers must not become duplicate lines — behavioral
    guidance lives in the extraction system prompt; this pins its presence
    (live proof belongs to the staging certification)."""
    from app.modules.receipts.extraction_llm import _SYSTEM

    assert "multi-page document is ONE invoice" in _SYSTEM
    assert "duplicate lines" in _SYSTEM


# ── dedup / duplicate delivery ────────────────────────────────────────────────


async def test_duplicate_message_id_never_duplicates(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    payload = _payload(mid, token, [("inv.pdf", _PDF_BYTES, "application/pdf")])
    try:
        assert (await client.post(_URL, json=payload, headers=_AUTH)).json()["status"] == "pending"
        puts_after_first = len(spaces.objects)

        # Duplicate while pending → no-op, no re-upload.
        r = await client.post(_URL, json=payload, headers=_AUTH)
        assert r.json() == {"status": "pending", "duplicate": True}
        assert len(spaces.objects) == puts_after_first
        assert (
            await admin_conn.fetchval(
                "SELECT count(*) FROM inbound_email_inbox WHERE postmark_message_id = $1", mid
            )
            == 1
        )

        await InboundEmailWorker().process_once()
        # Duplicate after completion → no-op; receipts unchanged.
        r = await client.post(_URL, json=payload, headers=_AUTH)
        assert r.json() == {"status": "complete", "duplicate": True}
        n = await admin_conn.fetchval("SELECT count(*) FROM receipts WHERE tenant_id = $1", tid)
        assert n == 1
    finally:
        await _cleanup(admin_conn, tid)


# ── rejection paths: no object, no draft, terminal, not retried ───────────────


async def test_unsupported_types_create_no_object_and_no_draft(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    payload = _payload(
        mid,
        token,
        [
            ("doc.docx", _DOCX_BYTES, "application/vnd.openxmlformats"),
            ("sheet.xlsx", _DOCX_BYTES + b"x", "application/vnd.ms-excel"),
            ("data.csv", _CSV_BYTES, "text/csv"),
        ],
    )
    try:
        r = await client.post(_URL, json=payload, headers=_AUTH)
        assert r.json() == {"status": "filtered_out", "attachments_qualified": 0}
        assert spaces.objects == {}  # nothing reached Spaces
        row = await _inbox_row(admin_conn, mid)
        assert row["processing_status"] == "filtered_out"
        assert row["skip_reason"] == "no_qualifying_attachment"
        assert row["suppression_stage"] == "pre_draft"
        n_att = await admin_conn.fetchval(
            "SELECT count(*) FROM inbound_email_attachments WHERE inbound_email_id = $1",
            row["id"],
        )
        assert n_att == 0

        # Terminal is terminal: the worker has nothing to claim, a replay is a no-op.
        assert await InboundEmailWorker().process_once() is False
        r = await client.post(_URL, json=payload, headers=_AUTH)
        assert r.json() == {"status": "filtered_out", "duplicate": True}
        assert spaces.objects == {}
        n = await admin_conn.fetchval("SELECT count(*) FROM receipts WHERE tenant_id = $1", tid)
        assert n == 0
    finally:
        await _cleanup(admin_conn, tid)


async def test_no_attachment_with_html_body_terminal_and_flagged(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    try:
        r = await client.post(
            _URL, json=_payload(mid, token, [], html_body="<table>…</table>"), headers=_AUTH
        )
        assert r.json()["status"] == "no_attachment"
        row = await _inbox_row(admin_conn, mid)
        assert row["has_html_body"] is True
        # D-606-17 marker so the HTML-body phase can find deferred rows.
        assert "html_body_deferred" in json.loads(row["filter_flags"])
    finally:
        await _cleanup(admin_conn, tid)


async def test_spam_score_filtered_before_byte_work(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    try:
        payload = _payload(mid, token, [("inv.pdf", _PDF_BYTES, "application/pdf")], spam_score=7.5)
        r = await client.post(_URL, json=payload, headers=_AUTH)
        assert r.json()["status"] == "filtered_out"
        assert spaces.objects == {}  # spam gate precedes ALL byte work
        row = await _inbox_row(admin_conn, mid)
        assert row["skip_reason"] == "spam_score_exceeded"
    finally:
        await _cleanup(admin_conn, tid)


# ── unknown token: no byte work, alert, replay dedup ──────────────────────────


async def test_unknown_token_metadata_only_alert_once(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    mid = f"pm-{uuid.uuid4()}"
    payload = _payload(mid, "no-such-token", [("inv.pdf", _PDF_BYTES, "application/pdf")])
    try:
        r = await client.post(_URL, json=payload, headers=_AUTH)
        assert r.json() == {"status": "filtered_out", "reason": "unknown_token"}
        assert spaces.objects == {}  # auth precedes byte work (#6)
        row = await _inbox_row(admin_conn, mid)
        assert row["tenant_id"] is None
        assert row["skip_reason"] == "unknown_token"
        # Retention = diagnostic minimum: token + sender kept, NO subject, NO
        # attachment rows (bytes/filenames), counts only.
        assert row["mailbox_hash"] == "no-such-token"
        assert row["from_email"] == "orders@lauzon.example"
        assert row["subject"] is None
        assert row["attachment_count"] == 1
        n_att = await admin_conn.fetchval(
            "SELECT count(*) FROM inbound_email_attachments WHERE inbound_email_id = $1",
            row["id"],
        )
        assert n_att == 0
        alerts = await admin_conn.fetchval(
            "SELECT count(*) FROM monitoring_alerts WHERE monitor_name = 'postmark_unknown_token'"
        )
        assert alerts == 1

        # Replay: partial-unique dedups the row AND the alert.
        await client.post(_URL, json=payload, headers=_AUTH)
        rows = await admin_conn.fetchval(
            "SELECT count(*) FROM inbound_email_inbox WHERE postmark_message_id = $1", mid
        )
        alerts = await admin_conn.fetchval(
            "SELECT count(*) FROM monitoring_alerts WHERE monitor_name = 'postmark_unknown_token'"
        )
        assert (rows, alerts) == (1, 1)
    finally:
        await _cleanup(admin_conn)


# ── tenant routing isolation ──────────────────────────────────────────────────


async def test_tenant_routing_isolated_and_dedup_tenant_scoped(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    tok_a, tok_b = f"tok-a-{uuid.uuid4().hex[:8]}", f"tok-b-{uuid.uuid4().hex[:8]}"
    tid_a = await _seed_tenant(admin_conn, tok_a)
    tid_b = await _seed_tenant(admin_conn, tok_b)
    mid = f"pm-{uuid.uuid4()}"  # SAME message id to both tenants
    try:
        for tok in (tok_a, tok_b):
            r = await client.post(
                _URL,
                json=_payload(mid, tok, [("inv.pdf", _PDF_BYTES, "application/pdf")]),
                headers=_AUTH,
            )
            assert r.json()["status"] == "pending"
        # Dedup is tenant-scoped (#11): one row EACH, not one global.
        rows = await admin_conn.fetch(
            "SELECT tenant_id FROM inbound_email_inbox WHERE postmark_message_id = $1", mid
        )
        assert sorted(str(r["tenant_id"]) for r in rows) == sorted([str(tid_a), str(tid_b)])

        while await InboundEmailWorker().process_once():
            pass
        for tid in (tid_a, tid_b):
            n = await admin_conn.fetchval("SELECT count(*) FROM receipts WHERE tenant_id = $1", tid)
            assert n == 1  # each tenant got exactly its own draft, no cross-leak
    finally:
        await _cleanup(admin_conn, tid_a, tid_b)


# ── failure semantics ─────────────────────────────────────────────────────────


async def test_transient_storage_failure_is_retriable(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    payload = _payload(mid, token, [("inv.pdf", _PDF_BYTES, "application/pdf")])
    try:
        spaces.fail = True
        r = await client.post(_URL, json=payload, headers=_AUTH)
        assert r.status_code == 500  # asks Postmark to retry
        row = await _inbox_row(admin_conn, mid)
        assert row["processing_status"] == "receiving"  # NOT terminal, NOT failed

        # Fresh-lease retry is a no-op (another delivery may be mid-upload)…
        spaces.fail = False
        r = await client.post(_URL, json=payload, headers=_AUTH)
        assert r.json() == {"status": "receiving", "duplicate": True}

        # …but once the lease is stale, the retry FINISHES the upload.
        await admin_conn.execute(
            "UPDATE inbound_email_inbox SET locked_at = now() - interval '6 minutes' WHERE id = $1",
            row["id"],
        )
        r = await client.post(_URL, json=payload, headers=_AUTH)
        assert r.json() == {"status": "pending", "attachments_qualified": 1}
        assert len(spaces.objects) == 1
    finally:
        await _cleanup(admin_conn, tid)


async def test_worker_retry_exhaustion_reaches_failed_terminal(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces, monkeypatch: Any
) -> None:
    """Transient worker crashes: attempts are counted at CLAIM time, so even a
    mid-transaction crash (which rolls the tx back) converges on failed_terminal."""
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    try:
        await client.post(
            _URL,
            json=_payload(mid, token, [("inv.pdf", _PDF_BYTES, "application/pdf")]),
            headers=_AUTH,
        )

        async def boom(*a: Any, **k: Any) -> Any:
            raise RuntimeError("simulated draft crash")

        monkeypatch.setattr(worker_mod.repo, "create_draft", boom)
        w = InboundEmailWorker()
        for expected_attempts, expected_status in (
            (1, "failed"),
            (2, "failed"),
            (3, "failed_terminal"),
        ):
            assert await w.process_once() is True
            row = await _inbox_row(admin_conn, mid)
            assert (row["attempts"], row["processing_status"]) == (
                expected_attempts,
                expected_status,
            )
            assert row["last_error"] == "RuntimeError"  # class only, never content

        # Exhausted: not claimable, no drafts ever half-created.
        assert await w.process_once() is False
        n = await admin_conn.fetchval("SELECT count(*) FROM receipts WHERE tenant_id = $1", tid)
        assert n == 0
    finally:
        await _cleanup(admin_conn, tid)


async def test_worker_lost_fence_rolls_back_every_sibling_write(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    """PR #4 rule: drafts/links/jobs are unfenced siblings of the fenced complete
    flip — when the lease is rotated mid-flight, the whole transaction dies."""
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    try:
        await client.post(
            _URL,
            json=_payload(mid, token, [("inv.pdf", _PDF_BYTES, "application/pdf")]),
            headers=_AUTH,
        )
        w = InboundEmailWorker()
        claim = await w._claim()
        assert claim is not None and claim["tenant_id"] == tid
        # Simulate a reset/supersede while the worker holds its (now stale) claim:
        # rotate the lease to NULL — NULL never matches a held token.
        await admin_conn.execute(
            "UPDATE inbound_email_inbox SET lease_token = NULL WHERE id = $1", claim["id"]
        )
        await w._process(claim)

        assert (
            await admin_conn.fetchval("SELECT count(*) FROM receipts WHERE tenant_id = $1", tid)
            == 0
        )
        assert (
            await admin_conn.fetchval(
                "SELECT count(*) FROM receipt_extraction_jobs WHERE tenant_id = $1", tid
            )
            == 0
        )
        linked = await admin_conn.fetchval(
            "SELECT count(*) FROM inbound_email_attachments "
            "WHERE tenant_id = $1 AND receipt_id IS NOT NULL",
            tid,
        )
        assert linked == 0
    finally:
        await _cleanup(admin_conn, tid)


# ── D-606-15: no content in logs ──────────────────────────────────────────────


async def test_no_body_or_attachment_content_in_logs(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    body_marker = "SENSITIVE-HTML-BODY-9f3a"
    attach_marker = b"SENSITIVE-ATTACHMENT-77c1"
    payload = _payload(
        mid,
        token,
        [("inv.pdf", b"%PDF-1.4\n" + attach_marker + b"A" * 11_000, "application/pdf")],
        html_body=f"<p>{body_marker}</p>",
    )
    try:
        with structlog.testing.capture_logs() as logs:
            await client.post(_URL, json=payload, headers=_AUTH)
            await InboundEmailWorker().process_once()
        dumped = json.dumps(logs, default=str)
        assert body_marker not in dumped
        assert attach_marker.decode() not in dumped
        assert _PASS not in dumped
        assert token not in dumped  # routing token is a credential — never logged
        # And the receipt was still created (the cycle actually ran).
        n = await admin_conn.fetchval("SELECT count(*) FROM receipts WHERE tenant_id = $1", tid)
        assert n == 1
    finally:
        await _cleanup(admin_conn, tid)


# ── multi-tenant routing: recipient collections, ambiguity, revocation ────────


async def test_same_token_across_recipient_fields_routes_once(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    try:
        payload = _with_recipients(
            _payload(mid, token, [("inv.pdf", _PDF_BYTES, "application/pdf")]),
            to=[token, None],  # token repeated + an unrelated hash-less recipient
            cc=[token],
        )
        r = await client.post(_URL, json=payload, headers=_AUTH)
        assert r.json()["status"] == "pending"
        rows = await admin_conn.fetch(
            "SELECT tenant_id FROM inbound_email_inbox WHERE postmark_message_id = $1", mid
        )
        assert len(rows) == 1 and rows[0]["tenant_id"] == tid  # ONE resolution
    finally:
        await _cleanup(admin_conn, tid)


async def test_token_only_in_cc_still_routes(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    """Top-level MailboxHash empty (the matched recipient was in Cc)."""
    token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, token)
    mid = f"pm-{uuid.uuid4()}"
    try:
        payload = _with_recipients(
            _payload(mid, "", [("inv.pdf", _PDF_BYTES, "application/pdf")]),
            top_level="",
            cc=[token],
        )
        r = await client.post(_URL, json=payload, headers=_AUTH)
        assert r.json()["status"] == "pending"
        row = await _inbox_row(admin_conn, mid)
        assert row["tenant_id"] == tid
    finally:
        await _cleanup(admin_conn, tid)


async def test_two_distinct_tenant_tokens_fail_closed_ambiguous(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    tok_a, tok_b = f"tok-a-{uuid.uuid4().hex[:8]}", f"tok-b-{uuid.uuid4().hex[:8]}"
    tid_a = await _seed_tenant(admin_conn, tok_a)
    tid_b = await _seed_tenant(admin_conn, tok_b)
    mid = f"pm-{uuid.uuid4()}"
    try:
        payload = _with_recipients(
            _payload(mid, tok_a, [("inv.pdf", _PDF_BYTES, "application/pdf")]),
            to=[tok_a],
            cc=[tok_b],
        )
        r = await client.post(_URL, json=payload, headers=_AUTH)
        assert r.json() == {"status": "filtered_out", "reason": "AMBIGUOUS_TENANT_RECIPIENT"}
        assert spaces.objects == {}  # zero bytes stored
        row = await _inbox_row(admin_conn, mid)
        assert row["tenant_id"] is None  # NO fallback tenant
        assert row["skip_reason"] == "ambiguous_tenant_recipient"
        for tid in (tid_a, tid_b):
            n = await admin_conn.fetchval("SELECT count(*) FROM receipts WHERE tenant_id = $1", tid)
            assert n == 0  # zero drafts either side
        assert await InboundEmailWorker().process_once() is False  # nothing claimable
    finally:
        await _cleanup(admin_conn, tid_a, tid_b)


async def test_revoked_token_is_unknown_and_new_token_routes(
    client: AsyncClient, admin_conn: Any, spaces: FakeSpaces
) -> None:
    """Rotation semantics at the webhook: revoked token → metadata-only row,
    no drafts; the replacement token routes normally."""
    old_token = f"tok-{uuid.uuid4().hex[:12]}"
    tid = await _seed_tenant(admin_conn, old_token)
    new_token = f"tok-{uuid.uuid4().hex[:12]}"
    await admin_conn.execute(
        "UPDATE tenant_inbound_email_tokens SET revoked_at = now() WHERE tenant_id = $1", tid
    )
    await admin_conn.execute(
        "INSERT INTO tenant_inbound_email_tokens (tenant_id, token) VALUES ($1, $2)",
        tid,
        new_token,
    )
    mid_old, mid_new = f"pm-{uuid.uuid4()}", f"pm-{uuid.uuid4()}"
    try:
        r = await client.post(
            _URL,
            json=_payload(mid_old, old_token, [("inv.pdf", _PDF_BYTES, "application/pdf")]),
            headers=_AUTH,
        )
        assert r.json() == {"status": "filtered_out", "reason": "unknown_token"}
        assert spaces.objects == {}
        n = await admin_conn.fetchval("SELECT count(*) FROM receipts WHERE tenant_id = $1", tid)
        assert n == 0

        r = await client.post(
            _URL,
            json=_payload(mid_new, new_token, [("inv.pdf", _PDF_BYTES, "application/pdf")]),
            headers=_AUTH,
        )
        assert r.json()["status"] == "pending"
        row = await _inbox_row(admin_conn, mid_new)
        assert row["tenant_id"] == tid
    finally:
        await _cleanup(admin_conn, tid)
