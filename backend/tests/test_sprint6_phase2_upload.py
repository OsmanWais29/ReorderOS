"""Sprint 6 S2 — receipts module + API-mediated upload.

Three layers:
  * validation units (no DB) — magic-byte allowlist, polyglot + HEIC rejection,
    size bounds, and the EXIF strip (the gate: a GPS/metadata tag present in the
    uploaded bytes is GONE from what would reach Spaces);
  * service round-trip (bound-session DB + monkeypatched storage) — bytes through
    the server create a draft with source='mobile_photo'; the bytes handed to the
    storage PUT carry no EXIF; a validation failure writes nothing;
  * HTTP RBAC (real role principals) — staff can upload, list requires manager.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from io import BytesIO
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from uuid6 import uuid7

from app.core import storage
from app.core.database import engine, get_db_session, make_bound_session
from app.core.security import Principal, get_principal
from app.main import create_app
from app.modules.receipts import services
from app.modules.receipts.validation import (
    MIME_JPEG,
    MIME_PDF,
    MIME_PNG,
    ReceiptValidationError,
    validate_and_clean,
)

pytestmark = pytest.mark.integration

_EXIF_DESCRIPTION = 0x010E  # ImageDescription


def _jpeg_with_exif() -> bytes:
    img = Image.new("RGB", (16, 16), (10, 20, 30))
    exif = img.getexif()
    exif[_EXIF_DESCRIPTION] = "ReorderOS GPS 45.4215,-75.6972"
    buf = BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def _plain_png() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (16, 16), (200, 100, 50)).save(buf, format="PNG")
    return buf.getvalue()


def _pdf() -> bytes:
    return b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _heic() -> bytes:
    return b"\x00\x00\x00\x18ftypheic" + b"\x00" * 16


# ── validation units ──────────────────────────────────────────────────────────


def test_accepts_pdf_png_jpeg() -> None:
    assert validate_and_clean(_pdf())[0] == MIME_PDF
    assert validate_and_clean(_plain_png())[0] == MIME_PNG
    assert validate_and_clean(_jpeg_with_exif())[0] == MIME_JPEG


def test_rejects_empty_and_oversize() -> None:
    with pytest.raises(ReceiptValidationError) as e1:
        validate_and_clean(b"")
    assert e1.value.code == "RECEIPT_EMPTY"
    with pytest.raises(ReceiptValidationError) as e2:
        validate_and_clean(b"%PDF-" + b"\x00" * (50 * 1024 * 1024 + 1))
    assert e2.value.code == "RECEIPT_TOO_LARGE"


def test_rejects_unsupported_type() -> None:
    with pytest.raises(ReceiptValidationError) as e:
        validate_and_clean(b"GIF89a" + b"\x00" * 32)
    assert e.value.code == "RECEIPT_UNSUPPORTED_TYPE"


@pytest.mark.parametrize(
    ("label", "data"),
    [
        # DOCX/XLSX are ZIP containers: PK\x03\x04 at offset 0.
        ("docx", b"PK\x03\x04" + b"\x14\x00\x06\x00" + b"word/document.xml" + b"\x00" * 32),
        ("xlsx", b"PK\x03\x04" + b"\x14\x00\x06\x00" + b"xl/workbook.xml" + b"\x00" * 32),
        # Legacy DOC/XLS: OLE2 compound-file signature.
        ("doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64),
        ("xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64 + b"Workbook"),
        # CSV: plain text, no container signature at all.
        ("csv", b"item,qty,price\nFlour,5,2.50\nSugar,2,1.80\n"),
    ],
)
def test_rejects_office_and_csv_formats(label: str, data: bytes) -> None:
    """Sprint 6 scope: JPEG/PNG/PDF only. Word/Excel/CSV must reject cleanly
    (RECEIPT_UNSUPPORTED_TYPE), never reach Spaces, never create a job."""
    with pytest.raises(ReceiptValidationError) as e:
        validate_and_clean(data, filename=f"invoice.{label}")
    assert e.value.code == "RECEIPT_UNSUPPORTED_TYPE"


def test_rejects_heic() -> None:
    with pytest.raises(ReceiptValidationError) as e:
        validate_and_clean(_heic())
    assert e.value.code == "RECEIPT_HEIC_UNSUPPORTED"


def test_rejects_polyglot_pdf_with_png() -> None:
    poly = _pdf() + b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    with pytest.raises(ReceiptValidationError) as e:
        validate_and_clean(poly)
    assert e.value.code == "RECEIPT_POLYGLOT_REJECTED"


def test_rejects_polyglot_jpeg_with_zip() -> None:
    poly = _jpeg_with_exif() + b"PK\x03\x04" + b"\x00" * 16
    with pytest.raises(ReceiptValidationError) as e:
        validate_and_clean(poly)
    assert e.value.code == "RECEIPT_POLYGLOT_REJECTED"


def test_exif_is_stripped() -> None:
    original = _jpeg_with_exif()
    # sanity: the uploaded bytes really carry the metadata
    assert Image.open(BytesIO(original)).getexif().get(_EXIF_DESCRIPTION) is not None
    _mime, cleaned = validate_and_clean(original)
    # the cleaned bytes that would reach Spaces carry NO metadata
    assert Image.open(BytesIO(cleaned)).getexif().get(_EXIF_DESCRIPTION) is None
    assert dict(Image.open(BytesIO(cleaned)).getexif()) == {}


# ── service round-trip (bound session + monkeypatched storage) ────────────────


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


async def _seed_tenant_user(db: Any) -> tuple[uuid.UUID, uuid.UUID]:
    tid = uuid.uuid4()
    uid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'T', :slug)"),
        {"id": tid, "slug": f"t-{tid.hex[:8]}"},
    )
    await db.execute(
        text("INSERT INTO users (id, workos_id, email) VALUES (:id, :w, :e)"),
        {"id": uid, "w": f"w_{uid.hex[:8]}", "e": f"{uid.hex[:8]}@test.com"},
    )
    return tid, uid


async def test_upload_creates_draft_with_clean_bytes(db: Any, monkeypatch: Any) -> None:
    tid, uid = await _seed_tenant_user(db)
    captured: dict[str, Any] = {}

    def fake_put(key: str, data: bytes, *, content_type: str) -> None:
        captured["key"] = key
        captured["data"] = data
        captured["content_type"] = content_type

    monkeypatch.setattr(storage, "put_bytes", fake_put)

    result = await services.create_receipt_from_upload(
        db, tenant_id=tid, raw_bytes=_jpeg_with_exif(), filename="invoice.jpg", created_by=uid
    )

    # round-trip: a draft exists, source mobile_photo, key persisted
    row = (
        (
            await db.execute(
                text(
                    "SELECT source, photo_object_key, mime_type, commit_state "
                    "FROM receipts WHERE id = :id AND tenant_id = :t"
                ),
                {"id": result["receipt_id"], "t": tid},
            )
        )
        .mappings()
        .one()
    )
    assert row["source"] == "mobile_photo"
    assert row["commit_state"] == "draft"
    assert row["photo_object_key"] == captured["key"] == result["photo_object_key"]
    assert str(tid) in captured["key"]  # tenant-scoped key
    # the bytes that reached storage are EXIF-free
    assert Image.open(BytesIO(captured["data"])).getexif().get(_EXIF_DESCRIPTION) is None


async def test_upload_validation_failure_writes_nothing(db: Any, monkeypatch: Any) -> None:
    tid, uid = await _seed_tenant_user(db)
    calls: list[Any] = []
    monkeypatch.setattr(storage, "put_bytes", lambda *a, **k: calls.append(a))

    with pytest.raises(ReceiptValidationError):
        await services.create_receipt_from_upload(
            db, tenant_id=tid, raw_bytes=_heic(), filename="ios.heic", created_by=uid
        )
    assert calls == []  # validation precedes any storage write
    n = (
        await db.execute(text("SELECT count(*) FROM receipts WHERE tenant_id = :t"), {"t": tid})
    ).scalar_one()
    assert n == 0


# ── HTTP RBAC (real role principals) ──────────────────────────────────────────


@pytest.fixture(scope="module")
def app_instance() -> Any:
    return create_app()


@pytest.fixture
async def conn(app_instance: Any) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as connection:
        await connection.begin()
        bound = make_bound_session(connection)
        app_instance.dependency_overrides[get_db_session] = lambda: bound
        try:
            yield connection
        finally:
            app_instance.dependency_overrides.clear()
            await connection.rollback()


@pytest.fixture
async def client(app_instance: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app_instance), base_url="http://test") as c:
        yield c


def _as(app_instance: Any, tenant_id: str, user_id: str, role: str) -> None:
    app_instance.dependency_overrides[get_principal] = lambda: Principal(
        user_id=user_id,
        workos_id=f"w_{user_id[:8]}",
        email="x@test.com",
        tenant_id=tenant_id,
        role=role,  # type: ignore[arg-type]
    )


async def test_http_rbac_staff_uploads_manager_lists(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient, monkeypatch: Any
) -> None:
    monkeypatch.setattr(storage, "put_bytes", lambda *a, **k: None)
    tid = str(uuid7())
    uid = str(uuid7())
    await conn.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'T', :slug)"),
        {"id": tid, "slug": f"t-{uuid.uuid4().hex[:8]}"},
    )
    await conn.execute(
        text("INSERT INTO users (id, workos_id, email) VALUES (:id, :w, :e)"),
        {"id": uid, "w": f"w_{uid[:8]}", "e": f"{uid[:8]}@test.com"},
    )

    # staff CAN upload
    _as(app_instance, tid, uid, "staff")
    r = await client.post(
        "/api/v1/receipts/uploads",
        files={"file": ("invoice.jpg", _jpeg_with_exif(), "image/jpeg")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["mime_type"] == MIME_JPEG

    # staff CANNOT list (manager+)
    r_staff_list = await client.get("/api/v1/receipts")
    assert r_staff_list.status_code == 403

    # manager CAN list and sees the uploaded draft
    _as(app_instance, tid, uid, "manager")
    r_list = await client.get("/api/v1/receipts")
    assert r_list.status_code == 200
    assert any(item["source"] == "mobile_photo" for item in r_list.json())


async def test_http_rejects_heic_with_422(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient, monkeypatch: Any
) -> None:
    monkeypatch.setattr(storage, "put_bytes", lambda *a, **k: None)
    tid = str(uuid7())
    uid = str(uuid7())
    await conn.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'T', :slug)"),
        {"id": tid, "slug": f"t-{uuid.uuid4().hex[:8]}"},
    )
    await conn.execute(
        text("INSERT INTO users (id, workos_id, email) VALUES (:id, :w, :e)"),
        {"id": uid, "w": f"w_{uid[:8]}", "e": f"{uid[:8]}@test.com"},
    )
    _as(app_instance, tid, uid, "staff")
    r = await client.post(
        "/api/v1/receipts/uploads",
        files={"file": ("ios.heic", _heic(), "application/octet-stream")},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "RECEIPT_HEIC_UNSUPPORTED"


async def _seed_receipt_via_conn(
    conn: AsyncConnection, tid: str, *, review_started: bool = False
) -> str:
    rid = str(uuid7())
    await conn.execute(
        text(
            "INSERT INTO receipts (id, tenant_id, commit_state, source) "
            "VALUES (:id, :t, 'draft', 'mobile_photo')"
        ),
        {"id": rid, "t": tid},
    )
    if review_started:
        await conn.execute(
            text("UPDATE receipts SET review_started_at = now() WHERE id = :id"), {"id": rid}
        )
    return rid


async def test_extract_enqueues_job(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    tid, uid = str(uuid7()), str(uuid7())
    await conn.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'T', :slug)"),
        {"id": tid, "slug": f"t-{uuid.uuid4().hex[:8]}"},
    )
    rid = await _seed_receipt_via_conn(conn, tid)
    _as(app_instance, tid, uid, "staff")

    r = await client.post(f"/api/v1/receipts/{rid}/extract")
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "pending"
    job = await conn.execute(
        text("SELECT status FROM receipt_extraction_jobs WHERE receipt_id = :r"), {"r": rid}
    )
    assert job.scalar_one() == "pending"
    es = await conn.execute(
        text("SELECT extraction_status FROM receipts WHERE id = :r"), {"r": rid}
    )
    assert es.scalar_one() == "pending"


async def test_extract_409_when_review_started(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    tid, uid = str(uuid7()), str(uuid7())
    await conn.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'T', :slug)"),
        {"id": tid, "slug": f"t-{uuid.uuid4().hex[:8]}"},
    )
    rid = await _seed_receipt_via_conn(conn, tid, review_started=True)
    _as(app_instance, tid, uid, "staff")

    r = await client.post(f"/api/v1/receipts/{rid}/extract")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "RECEIPT_REVIEW_IN_PROGRESS"
    # no job created
    n = await conn.execute(
        text("SELECT count(*) FROM receipt_extraction_jobs WHERE receipt_id = :r"), {"r": rid}
    )
    assert n.scalar_one() == 0
