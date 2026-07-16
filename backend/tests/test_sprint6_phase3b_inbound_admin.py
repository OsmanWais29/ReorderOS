"""Inbound-email observability surface — endpoints + ops verify command.

Endpoint tests ride the bound-session HTTP harness (get_db_session/get_principal
overrides, auto-rollback). The ops-command test seeds COMMITTED rows via
admin_conn because `verify()` opens its own engine — a bound-session seed would
be invisible to it (the Sprint-6 false-green lesson, applied in reverse).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from uuid6 import uuid7

from app.core.config import get_settings
from app.core.database import engine, get_db_session, make_bound_session
from app.core.security import Principal, get_principal
from app.main import create_app

pytestmark = pytest.mark.integration

_TOKEN = "tok-admin-test-4f9a2c"


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
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _as(app_instance: Any, tenant_id: str, user_id: str, role: str) -> None:
    app_instance.dependency_overrides[get_principal] = lambda: Principal(
        user_id=user_id,
        workos_id=f"w_{user_id[:8]}",
        email="x@test.com",
        tenant_id=tenant_id,
        role=role,  # type: ignore[arg-type]
    )


async def _seed(conn: AsyncConnection) -> dict[str, str]:
    """One tenant with: a token, a FILTERED email, and a SUCCESSFUL email whose
    attachment is linked to a draft with a complete extraction."""
    tid, uid = str(uuid7()), str(uuid7())
    iid_ok, iid_filtered, rid = str(uuid7()), str(uuid7()), str(uuid7())
    await conn.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'T', :slug)"),
        {"id": tid, "slug": f"t-{uuid.uuid4().hex[:8]}"},
    )
    await conn.execute(
        text("INSERT INTO tenant_inbound_email_tokens (tenant_id, token) VALUES (:t, :tok)"),
        {"t": tid, "tok": _TOKEN},
    )
    await conn.execute(
        text("""
            INSERT INTO inbound_email_inbox
                (id, tenant_id, postmark_message_id, source, mailbox_hash, from_email,
                 subject, attachment_count, processing_status, suppression_stage,
                 skip_reason, filter_flags)
            VALUES
                (:id, :tid, :mid, 'postmark', :tok, 'a@sup.example', 'April order',
                 2, 'filtered_out', 'pre_draft', 'no_qualifying_attachment',
                 '["attachment_0:RECEIPT_UNSUPPORTED_TYPE"]'::jsonb)
        """),
        {"id": iid_filtered, "tid": tid, "mid": f"pm-f-{uuid.uuid4()}", "tok": _TOKEN},
    )
    await conn.execute(
        text("""
            INSERT INTO inbound_email_inbox
                (id, tenant_id, postmark_message_id, source, mailbox_hash, from_email,
                 subject, attachment_count, processing_status)
            VALUES
                (:id, :tid, :mid, 'postmark', :tok, 'b@sup.example', 'Invoice 42',
                 1, 'complete')
        """),
        {"id": iid_ok, "tid": tid, "mid": f"pm-s-{uuid.uuid4()}", "tok": _TOKEN},
    )
    await conn.execute(
        text("""
            INSERT INTO receipts (id, tenant_id, commit_state, source, inbound_email_id,
                                  extraction_status, mime_type)
            VALUES (:id, :tid, 'draft', 'email', :iid, 'complete', 'application/pdf')
        """),
        {"id": rid, "tid": tid, "iid": iid_ok},
    )
    await conn.execute(
        text("""
            INSERT INTO inbound_email_attachments
                (inbound_email_id, tenant_id, attachment_index, original_filename,
                 mime_type, object_key, receipt_id)
            VALUES (:iid, :tid, 0, 'inv.pdf', 'application/pdf', 'receipts/x/0.pdf', :rid)
        """),
        {"iid": iid_ok, "tid": tid, "rid": rid},
    )
    return {"tid": tid, "uid": uid, "iid_ok": iid_ok, "iid_filtered": iid_filtered, "rid": rid}


async def test_manager_sees_own_tenant_metadata(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    _as(app_instance, s["tid"], s["uid"], "manager")
    r = await client.get("/api/v1/receipts/inbound-emails")
    assert r.status_code == 200, r.text
    emails = {e["id"]: e for e in r.json()["inbound_emails"]}
    assert set(emails) == {s["iid_ok"], s["iid_filtered"]}

    ok = emails[s["iid_ok"]]
    assert ok["display_status"] == "needs_review"
    assert ok["qualified_attachment_count"] == 1
    assert ok["receipts"][0]["receipt_id"] == s["rid"]
    assert ok["receipts"][0]["extraction_status"] == "complete"

    filtered = emails[s["iid_filtered"]]
    assert filtered["display_status"] == "filtered"
    assert filtered["skip_reason"] == "no_qualifying_attachment"
    assert "attachment_0:RECEIPT_UNSUPPORTED_TYPE" in filtered["filter_flags"]
    assert filtered["receipts"] == []


async def test_detail_includes_attachments_but_never_keys_or_tokens(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    _as(app_instance, s["tid"], s["uid"], "manager")
    r = await client.get(f"/api/v1/receipts/inbound-emails/{s['iid_ok']}")
    assert r.status_code == 200
    body = r.json()
    assert body["attachments"] == [
        {
            "attachment_index": 0,
            "original_filename": "inv.pdf",
            "mime_type": "application/pdf",
            "stored": True,
            "receipt_id": s["rid"],
        }
    ]
    # Leak gate: no routing token, no mailbox_hash field, no object keys, anywhere.
    dumped = json.dumps(body)
    assert _TOKEN not in dumped
    assert "mailbox_hash" not in dumped
    assert "object_key" not in dumped
    assert "receipts/x/0.pdf" not in dumped

    listing = await client.get("/api/v1/receipts/inbound-emails")
    dumped_list = json.dumps(listing.json())
    assert _TOKEN not in dumped_list
    assert "mailbox_hash" not in dumped_list


async def test_cross_tenant_blocked_and_staff_403(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    other_tid, other_uid = str(uuid7()), str(uuid7())
    await conn.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'O', :slug)"),
        {"id": other_tid, "slug": f"o-{uuid.uuid4().hex[:8]}"},
    )
    # Another tenant's manager sees nothing of tenant A…
    _as(app_instance, other_tid, other_uid, "manager")
    r = await client.get("/api/v1/receipts/inbound-emails")
    assert r.status_code == 200
    assert r.json()["inbound_emails"] == []
    # …and A's detail id is a 404, not a leak.
    r = await client.get(f"/api/v1/receipts/inbound-emails/{s['iid_ok']}")
    assert r.status_code == 404

    # Staff role is blocked outright (manager+ surface).
    _as(app_instance, s["tid"], s["uid"], "staff")
    for path in ("/api/v1/receipts/inbound-emails", "/api/v1/receipts/inbound-address"):
        assert (await client.get(path)).status_code == 403


async def test_inbound_address_provisions_once_and_reuses(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient, monkeypatch: Any
) -> None:
    tid, uid = str(uuid7()), str(uuid7())
    await conn.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'T', :slug)"),
        {"id": tid, "slug": f"t-{uuid.uuid4().hex[:8]}"},
    )
    _as(app_instance, tid, uid, "manager")

    # Unconfigured environment → configured:false, no address invented.
    monkeypatch.delenv("POSTMARK_INBOUND_ADDRESS", raising=False)
    monkeypatch.setenv("POSTMARK_INBOUND_ENABLED", "true")
    monkeypatch.setenv("POSTMARK_WEBHOOK_USER", "u")
    monkeypatch.setenv("POSTMARK_WEBHOOK_PASSWORD", "p")
    get_settings.cache_clear()
    r = await client.get("/api/v1/receipts/inbound-address")
    assert r.json() == {"configured": False, "address": None}

    monkeypatch.setenv("POSTMARK_INBOUND_ADDRESS", "abc123@inbound.postmarkapp.com")
    get_settings.cache_clear()
    r1 = await client.get("/api/v1/receipts/inbound-address")
    assert r1.status_code == 200 and r1.json()["configured"] is True
    addr = r1.json()["address"]
    assert addr.startswith("abc123+") and addr.endswith("@inbound.postmarkapp.com")
    token = addr.split("+", 1)[1].split("@", 1)[0]
    stored = (
        await conn.execute(
            text(
                "SELECT token FROM tenant_inbound_email_tokens "
                "WHERE tenant_id = :tid AND revoked_at IS NULL"
            ),
            {"tid": tid},
        )
    ).scalar_one()
    assert stored == token

    # Second call reuses — no second token row.
    r2 = await client.get("/api/v1/receipts/inbound-address")
    assert r2.json()["address"] == addr
    n = (
        await conn.execute(
            text("SELECT count(*) FROM tenant_inbound_email_tokens WHERE tenant_id = :tid"),
            {"tid": tid},
        )
    ).scalar_one()
    assert n == 1
    get_settings.cache_clear()


# ── ops command ────────────────────────────────────────────────────────────────


async def test_ops_verify_command_happy_and_missing(admin_conn: Any, monkeypatch: Any) -> None:
    """python -m app.ops.verify_postmark_inbound core: PASS on a healthy chain,
    FAIL (not crash) on an unknown message id. Committed seed — verify() opens
    its own engine and must SEE the rows."""
    from app.ops.verify_postmark_inbound import format_report, verify
    from tests.conftest import DB_URL_SYNC

    mid = f"pm-ops-{uuid.uuid4()}"
    tid, iid, rid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin_conn.execute(
        "INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'ops')", tid, f"op-{tid.hex[:8]}"
    )
    try:
        await admin_conn.execute(
            "INSERT INTO inbound_email_inbox (id, tenant_id, postmark_message_id, source, "
            "attachment_count, processing_status) VALUES ($1,$2,$3,'postmark',1,'complete')",
            iid,
            tid,
            mid,
        )
        await admin_conn.execute(
            "INSERT INTO receipts (id, tenant_id, commit_state, source, inbound_email_id, "
            "extraction_status, mime_type) "
            "VALUES ($1,$2,'draft','email',$3,'complete','application/pdf')",
            rid,
            tid,
            iid,
        )
        await admin_conn.execute(
            "INSERT INTO inbound_email_attachments (inbound_email_id, tenant_id, "
            "attachment_index, mime_type, object_key, receipt_id) "
            "VALUES ($1,$2,0,'application/pdf','k/0.pdf',$3)",
            iid,
            tid,
            rid,
        )
        await admin_conn.execute(
            "INSERT INTO receipt_extraction_jobs (tenant_id, receipt_id, status, "
            "raw_extraction) VALUES ($1,$2,'complete','{}')",
            tid,
            rid,
        )

        out, checks = await verify(mid, DB_URL_SYNC)
        report = format_report(out, checks)
        assert "OVERALL: PASS" in report
        assert "draft visible in review queue: yes" in report
        assert all(ok for _, ok in checks)

        out2, checks2 = await verify(f"pm-none-{uuid.uuid4()}", DB_URL_SYNC)
        report2 = format_report(out2, checks2)
        assert "OVERALL: FAIL" in report2
        assert ("exactly one inbox row", False) in checks2
    finally:
        await admin_conn.execute("DELETE FROM receipt_extraction_jobs WHERE tenant_id = $1", tid)
        await admin_conn.execute("DELETE FROM inbound_email_attachments WHERE tenant_id = $1", tid)
        await admin_conn.execute("DELETE FROM receipts WHERE tenant_id = $1", tid)
        await admin_conn.execute("DELETE FROM inbound_email_inbox WHERE tenant_id = $1", tid)
        await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tid)
