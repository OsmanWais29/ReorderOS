"""Sprint 6 — receipt review endpoints (spec §5, D-606-25/26).

HTTP-level coverage of the review surface that closes the extraction → commit gap:
  * PUT  /receipts/{id}/lines/{line_id} — the D-606-26 match_status lifecycle
    (link → matched, create → created, clear → unmatched+uncorrected, skip/unskip)
    and the D-606-25 freshness rule (EVERY mutation sets review_started_at and
    clears reviewed_affirmation);
  * POST /receipts/{id}/lines — operator lines append after machine lines;
  * POST /receipts/{id}/reset-extraction — 409 without discard_edits, and the
    destructive path: all lines gone, review flags cleared, stale jobs superseded,
    a fresh job enqueued, notes PRESERVED;
  * POST /receipts/{id}/notes — notes_log append (any commit_state);
  * GET  /receipts/{id} — match suggestions on unmatched lines only;
  * the D-606-22 interplay: a manual correction alone satisfies the commit gate.
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

from app.core.database import engine, get_db_session, make_bound_session
from app.core.security import Principal, get_principal
from app.main import create_app
from app.modules.receipts.extraction_worker import ExtractionWorker

pytestmark = pytest.mark.integration


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


def _as(app_instance: Any, tenant_id: str, user_id: str, role: str = "staff") -> None:
    app_instance.dependency_overrides[get_principal] = lambda: Principal(
        user_id=user_id,
        workos_id=f"w_{user_id[:8]}",
        email="x@test.com",
        tenant_id=tenant_id,
        role=role,  # type: ignore[arg-type]
    )


async def _seed(conn: AsyncConnection) -> dict[str, Any]:
    """Tenant + user + one canonical unit ('g') + one active item ('Tomato') + a
    mobile_photo draft with one extraction job and two machine lines (ordinals 0,1,
    both unmatched — the state the review screen starts from)."""
    tid, uid = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'T', :slug)"),
        {"id": tid, "slug": f"t-{tid.hex[:8]}"},
    )
    await conn.execute(
        text("INSERT INTO users (id, workos_id, email) VALUES (:id, :w, :e)"),
        {"id": uid, "w": f"w_{uid.hex[:8]}", "e": f"{uid.hex[:8]}@test.com"},
    )
    unit_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO units_of_measure (id, tenant_id, name, abbreviation, unit_type) "
            "VALUES (:id, :tid, 'g', 'g', 'weight')"
        ),
        {"id": unit_id, "tid": tid},
    )
    item_id = uuid.uuid4()
    await conn.execute(
        text("""
            INSERT INTO inventory_items
                (id, tenant_id, name, inventory_mode, storage_unit_id, recipe_unit_id)
            VALUES (:id, :tid, 'Tomato', 'recipe_deducted', :uid, :uid)
        """),
        {"id": item_id, "tid": tid, "uid": unit_id},
    )
    receipt_id = uuid.uuid4()
    await conn.execute(
        text("""
            INSERT INTO receipts (id, tenant_id, commit_state, source, extraction_status)
            VALUES (:id, :tid, 'draft', 'mobile_photo', 'complete')
        """),
        {"id": receipt_id, "tid": tid},
    )
    job_id = (
        await conn.execute(
            text("""
                INSERT INTO receipt_extraction_jobs (tenant_id, receipt_id, job_attempt, status)
                VALUES (:tid, :rid, 1, 'complete') RETURNING id
            """),
            {"tid": tid, "rid": receipt_id},
        )
    ).scalar_one()
    line_ids = []
    for ordinal, (name, qty) in enumerate([("Tomatoes 5lb", 5), ("Basil bunch", 2)]):
        lid = uuid.uuid4()
        await conn.execute(
            text("""
                INSERT INTO receipt_lines
                    (id, tenant_id, receipt_id, extracted_name, received_quantity,
                     extracted_unit, confidence, match_status, extraction_job_id,
                     job_attempt, line_ordinal)
                VALUES (:id, :tid, :rid, :name, :qty, 'g', 0.9, 'unmatched', :job, 1, :ord)
            """),
            {
                "id": lid,
                "tid": tid,
                "rid": receipt_id,
                "name": name,
                "qty": qty,
                "ord": ordinal,
                "job": job_id,
            },
        )
        line_ids.append(lid)
    return {
        "tenant_id": tid,
        "user_id": uid,
        "unit_id": unit_id,
        "item_id": item_id,
        "receipt_id": receipt_id,
        "job_id": job_id,
        "line_ids": line_ids,
    }


async def _receipt_row(conn: AsyncConnection, s: dict[str, Any]) -> Any:
    return (
        (
            await conn.execute(
                text("""
                    SELECT review_started_at, reviewed_affirmation, extraction_status,
                           quota_blocked, quota_blocked_until, notes_log
                      FROM receipts WHERE id = :rid AND tenant_id = :tid
                """),
                {"rid": s["receipt_id"], "tid": s["tenant_id"]},
            )
        )
        .mappings()
        .one()
    )


def _line_url(s: dict[str, Any], idx: int = 0) -> str:
    return f"/api/v1/receipts/{s['receipt_id']}/lines/{s['line_ids'][idx]}"


async def _force_committed(conn: AsyncConnection, s: dict[str, Any]) -> None:
    """Put the receipt into 'committed' by satisfying trg_receipt_commit_integrity
    (>=1 matched line with an item; intake source needs confirmed_at + a corrected
    line) — the trigger rightly rejects a bare commit_state flip."""
    await conn.execute(
        text("""
            UPDATE receipt_lines
               SET inventory_item_id = :iid, match_status = 'matched', manually_corrected = true
             WHERE id = :lid
        """),
        {"iid": s["item_id"], "lid": s["line_ids"][0]},
    )
    await conn.execute(
        text("""
            UPDATE receipts
               SET commit_state = 'committed', committed_at = now(), confirmed_at = now()
             WHERE id = :r
        """),
        {"r": s["receipt_id"]},
    )


# ── PUT /lines/{id} — the D-606-25/26 lifecycle ──────────────────────────────


async def test_edit_qty_marks_corrected_and_touches_review(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    # Pre-set the affirmation so the clear is observable (D-606-25 freshness).
    await conn.execute(
        text("UPDATE receipts SET reviewed_affirmation = true WHERE id = :r"),
        {"r": s["receipt_id"]},
    )
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))

    r = await client.put(_line_url(s), json={"received_quantity": 7.5, "unit_cost_cents": 129})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["received_quantity"] == 7.5
    assert body["unit_cost_cents"] == 129
    assert body["manually_corrected"] is True
    assert body["match_status"] == "unmatched"  # a field edit is not an item match

    receipt = await _receipt_row(conn, s)
    assert receipt["review_started_at"] is not None  # first active edit
    assert receipt["reviewed_affirmation"] is False  # cleared by the mutation


async def test_link_existing_item_becomes_matched(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))

    r = await client.put(_line_url(s), json={"inventory_item_id": str(s["item_id"])})
    assert r.status_code == 200, r.text
    assert r.json()["match_status"] == "matched"
    assert r.json()["manually_corrected"] is True
    assert r.json()["inventory_item_id"] == str(s["item_id"])


async def test_create_new_item_becomes_created(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))

    r = await client.put(_line_url(s), json={"new_item_name": "Basil", "new_item_unit": "g"})
    assert r.status_code == 200, r.text
    assert r.json()["match_status"] == "created"
    assert r.json()["manually_corrected"] is True
    # The shared resolver really created the item, tenant-scoped.
    created = (
        await conn.execute(
            text("SELECT name FROM inventory_items WHERE tenant_id = :t AND id = :i"),
            {"t": s["tenant_id"], "i": r.json()["inventory_item_id"]},
        )
    ).scalar_one()
    assert created == "Basil"


async def test_created_item_uses_edited_name_and_response_echoes_it(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    """Live smoke 2026-07-15: operator edited the picker name before creating,
    but the row kept showing the invoice text and nothing surfaced what got
    linked. The item must be created with the OPERATOR'S name (never the
    extracted line text) and every line payload must echo item_name."""
    s = await _seed(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))

    r = await client.put(
        _line_url(s),
        json={"new_item_name": "Café Grains Espresso Foncé", "new_item_unit": "kg"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["item_name"] == "Café Grains Espresso Foncé"  # echoed for the UI
    created = (
        await conn.execute(
            text("SELECT name FROM inventory_items WHERE tenant_id = :t AND id = :i"),
            {"t": s["tenant_id"], "i": body["inventory_item_id"]},
        )
    ).scalar_one()
    assert created == "Café Grains Espresso Foncé"  # edited name, not invoice text

    # GET detail carries item_name too (the row's linked-item display).
    detail = await client.get(f"/api/v1/receipts/{s['receipt_id']}")
    assert detail.status_code == 200
    [line] = [ln for ln in detail.json()["lines"] if ln["id"] == str(s["line_ids"][0])]
    assert line["item_name"] == "Café Grains Espresso Foncé"


async def test_clear_item_reverts_to_machine_state(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))
    linked = await client.put(_line_url(s), json={"inventory_item_id": str(s["item_id"])})
    assert linked.status_code == 200

    r = await client.put(_line_url(s), json={"inventory_item_id": None})
    assert r.status_code == 200, r.text
    body = r.json()
    # D-606-26: never a matched row with a NULL item; reverted line is uncorrected.
    assert body["inventory_item_id"] is None
    assert body["match_status"] == "unmatched"
    assert body["manually_corrected"] is False


async def test_skip_and_unskip(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))
    await client.put(_line_url(s), json={"inventory_item_id": str(s["item_id"])})

    r_skip = await client.put(_line_url(s), json={"skipped": True})
    assert r_skip.status_code == 200
    assert r_skip.json()["match_status"] == "skipped"
    assert r_skip.json()["inventory_item_id"] == str(s["item_id"])  # item survives a skip

    r_unskip = await client.put(_line_url(s), json={"skipped": False})
    assert r_unskip.status_code == 200
    assert r_unskip.json()["match_status"] == "matched"  # restored from the linked item


async def test_invalid_combinations_are_422(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))

    # skip combined with an edit
    r1 = await client.put(_line_url(s), json={"skipped": True, "received_quantity": 3})
    # item link and item create together
    r2 = await client.put(
        _line_url(s),
        json={
            "inventory_item_id": str(s["item_id"]),
            "new_item_name": "X",
            "new_item_unit": "g",
        },
    )
    # non-canonical unit
    r3 = await client.put(_line_url(s), json={"extracted_unit": "oz"})
    # empty body
    r4 = await client.put(_line_url(s), json={})
    assert [r.status_code for r in (r1, r2, r3, r4)] == [422, 422, 422, 422]


async def test_unknown_item_is_422(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))
    r = await client.put(_line_url(s), json={"inventory_item_id": str(uuid.uuid4())})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "RECEIPT_UNKNOWN_ITEM"


async def test_committed_receipt_is_not_editable(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    await _force_committed(conn, s)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))

    r_put = await client.put(_line_url(s, idx=1), json={"received_quantity": 1})
    r_post = await client.post(
        f"/api/v1/receipts/{s['receipt_id']}/lines",
        json={"extracted_name": "Late", "received_quantity": 1, "extracted_unit": "g"},
    )
    assert r_put.status_code == 409
    assert r_put.json()["detail"]["code"] == "RECEIPT_NOT_EDITABLE"
    assert r_post.status_code == 409


async def test_cross_tenant_line_is_404(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    other = await _seed(conn)  # second tenant
    _as(app_instance, str(other["tenant_id"]), str(other["user_id"]))
    # Another tenant's receipt is simply not found — no existence leak.
    r = await client.put(_line_url(s), json={"received_quantity": 1})
    assert r.status_code == 404


# ── POST /lines ───────────────────────────────────────────────────────────────


async def test_add_operator_line_appends_after_machine_lines(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))

    r = await client.post(
        f"/api/v1/receipts/{s['receipt_id']}/lines",
        json={"extracted_name": "Olive oil", "received_quantity": 2, "extracted_unit": "g"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["match_status"] == "unmatched"
    assert body["manually_corrected"] is False
    assert body["line_ordinal"] == 2  # machine lines used 0 and 1

    # Linking at creation is 'matched' + corrected immediately.
    r2 = await client.post(
        f"/api/v1/receipts/{s['receipt_id']}/lines",
        json={
            "extracted_name": "Tomato crate",
            "received_quantity": 4,
            "extracted_unit": "g",
            "inventory_item_id": str(s["item_id"]),
        },
    )
    assert r2.status_code == 201
    assert r2.json()["match_status"] == "matched"
    assert r2.json()["manually_corrected"] is True
    assert r2.json()["line_ordinal"] == 3

    receipt = await _receipt_row(conn, s)
    assert receipt["review_started_at"] is not None  # add counts as an active edit


# ── reset-extraction ──────────────────────────────────────────────────────────


async def test_reset_extraction_full_flow(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))

    # A note that must SURVIVE the reset (audit trail).
    r_note = await client.post(
        f"/api/v1/receipts/{s['receipt_id']}/notes", json={"text": "supplier said 3 boxes"}
    )
    assert r_note.status_code == 201

    # An edit starts review → a plain re-extract is now refused (#7)...
    edit = await client.put(_line_url(s), json={"received_quantity": 9})
    assert edit.status_code == 200
    r_extract = await client.post(f"/api/v1/receipts/{s['receipt_id']}/extract")
    assert r_extract.status_code == 409
    assert r_extract.json()["detail"]["code"] == "RECEIPT_REVIEW_IN_PROGRESS"

    # A stale queued job that must be superseded by the reset.
    await conn.execute(
        text("""
            INSERT INTO receipt_extraction_jobs (tenant_id, receipt_id, job_attempt, status)
            VALUES (:tid, :rid, 2, 'pending')
        """),
        {"tid": s["tenant_id"], "rid": s["receipt_id"]},
    )
    # Simulate a quota-blocked receipt so the clear is observable.
    await conn.execute(
        text(
            "UPDATE receipts SET quota_blocked = true, "
            "quota_blocked_until = now() + interval '1 hour' WHERE id = :r"
        ),
        {"r": s["receipt_id"]},
    )

    # ...without the flag the reset refuses (work is never discarded implicitly)...
    r_no = await client.post(f"/api/v1/receipts/{s['receipt_id']}/reset-extraction", json={})
    assert r_no.status_code == 409
    assert r_no.json()["detail"]["code"] == "RECEIPT_RESET_NEEDS_CONFIRM"

    # ...with the flag it starts over from the machine.
    r_yes = await client.post(
        f"/api/v1/receipts/{s['receipt_id']}/reset-extraction", json={"discard_edits": True}
    )
    assert r_yes.status_code == 202, r_yes.text
    assert r_yes.json()["status"] == "pending"

    n_lines = (
        await conn.execute(
            text("SELECT count(*) FROM receipt_lines WHERE receipt_id = :r"),
            {"r": s["receipt_id"]},
        )
    ).scalar_one()
    assert n_lines == 0  # ALL lines gone — machine and operator

    receipt = await _receipt_row(conn, s)
    assert receipt["review_started_at"] is None
    assert receipt["reviewed_affirmation"] is False
    assert receipt["extraction_status"] == "pending"
    assert receipt["quota_blocked"] is False
    assert receipt["quota_blocked_until"] is None
    assert len(receipt["notes_log"]) == 1  # notes preserved

    stale = (
        await conn.execute(
            text(
                "SELECT count(*) FROM receipt_extraction_jobs "
                "WHERE receipt_id = :r AND job_attempt = 2 AND status = 'superseded'"
            ),
            {"r": s["receipt_id"]},
        )
    ).scalar_one()
    assert stale == 1  # the queued job cannot resurrect the pre-reset state
    fresh = (
        await conn.execute(
            text(
                "SELECT count(*) FROM receipt_extraction_jobs "
                "WHERE receipt_id = :r AND status = 'pending'"
            ),
            {"r": s["receipt_id"]},
        )
    ).scalar_one()
    assert fresh == 1

    # And the extract path is unblocked again (review_started_at cleared).
    r_extract2 = await client.post(f"/api/v1/receipts/{s['receipt_id']}/extract")
    assert r_extract2.status_code == 202


class _ForbiddenLLM:
    """Fake client that fails the test if the stale worker ever reaches the LLM."""

    async def extract_invoice(self, **_kw: Any) -> Any:
        raise AssertionError("stale worker must not call the LLM in this scenario")


async def test_reset_mid_flight_supersedes_processing_and_fences_stale_worker(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    """The in-flight race: a worker CLAIMED a job (holds lease_token), is mid-LLM,
    and the operator resets. The reset must (a) supersede every non-terminal job —
    processing and retriable failed included, since the claim SQL re-claims both —
    and (b) rotate the lease so the stale worker's fenced writes match ZERO rows:
    no raw_extraction checkpoint, no lines, no receipt-header stomp."""
    s = await _seed(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))

    stale_payload = {
        "document_type": "invoice",
        "supplier_name": "Stale Supplier Inc",
        "lines": [{"name": "Stale line", "qty": 1, "unit": "g", "confidence": 0.99}],
    }
    old_token = uuid.uuid4()
    # Job A: mid-flight (claimed: processing + locked_at + held token), with its
    # raw_extraction ALREADY checkpointed — the worst case, because the post-LLM
    # code path proceeds straight to line application.
    job_a = (
        await conn.execute(
            text("""
                INSERT INTO receipt_extraction_jobs
                    (tenant_id, receipt_id, job_attempt, status, locked_at, lease_token,
                     raw_extraction, attempts)
                VALUES (:tid, :rid, 2, 'processing', now(), :tok, CAST(:raw AS jsonb), 1)
                RETURNING id
            """),
            {
                "tid": s["tenant_id"],
                "rid": s["receipt_id"],
                "tok": old_token,
                "raw": json.dumps(stale_payload),
            },
        )
    ).scalar_one()
    # Job B: a retriable failure — the claim SQL re-claims 'failed', so reset must
    # supersede it too or it resurrects later.
    job_b = (
        await conn.execute(
            text("""
                INSERT INTO receipt_extraction_jobs
                    (tenant_id, receipt_id, job_attempt, status, attempts, last_error)
                VALUES (:tid, :rid, 3, 'failed', 1, 'transient')
                RETURNING id
            """),
            {"tid": s["tenant_id"], "rid": s["receipt_id"]},
        )
    ).scalar_one()

    r = await client.post(
        f"/api/v1/receipts/{s['receipt_id']}/reset-extraction", json={"discard_edits": True}
    )
    assert r.status_code == 202, r.text

    # (a) BOTH non-terminal jobs superseded, lease rotated (token+locked_at cleared).
    jobs = (
        (
            await conn.execute(
                text(
                    "SELECT id, status, lease_token, locked_at FROM receipt_extraction_jobs "
                    "WHERE id IN (:a, :b)"
                ),
                {"a": job_a, "b": job_b},
            )
        )
        .mappings()
        .all()
    )
    assert {row["status"] for row in jobs} == {"superseded"}
    assert all(row["lease_token"] is None and row["locked_at"] is None for row in jobs)

    # (b) The stale worker comes back from its LLM call and runs the real post-claim
    # code with the token it still holds.
    worker = ExtractionWorker(_ForbiddenLLM())
    ws = make_bound_session(conn)

    # Its checkpoint write is fenced out (nothing new lands in raw_extraction)...
    assert await worker._checkpoint(ws, job_a, old_token, {"late": True}) is False

    # ...and its line application reports a lost lease; per the _process contract
    # the transaction is rolled back — NOTHING it wrote survives.
    job_row = {"id": job_a, "job_attempt": 2, "tenant_id": s["tenant_id"], "attempts": 1}
    applied = await worker._apply(
        ws, job_a, old_token, job_row, s["receipt_id"], s["tenant_id"], stale_payload
    )
    assert applied is False
    await ws.rollback()  # what _process does on fence miss

    n_lines = (
        await conn.execute(
            text("SELECT count(*) FROM receipt_lines WHERE receipt_id = :r"),
            {"r": s["receipt_id"]},
        )
    ).scalar_one()
    assert n_lines == 0  # the reset receipt stays empty — no stale lines
    header = (
        (
            await conn.execute(
                text("SELECT extraction_status, supplier_name FROM receipts WHERE id = :r"),
                {"r": s["receipt_id"]},
            )
        )
        .mappings()
        .one()
    )
    assert header["extraction_status"] == "pending"  # still the post-reset state
    assert header["supplier_name"] is None  # stale header data did not land
    status_a = (
        await conn.execute(
            text("SELECT status FROM receipt_extraction_jobs WHERE id = :a"), {"a": job_a}
        )
    ).scalar_one()
    assert status_a == "superseded"  # the stale 'complete' flip was fenced out


# ── notes ─────────────────────────────────────────────────────────────────────


async def test_notes_append_any_state(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))

    # Author + timestamp are SERVER-stamped: client-supplied user_id/created_at/id
    # are ignored (NoteCreate accepts only `text`), and the endpoint can only append.
    spoof = {
        "text": "first",
        "user_id": str(uuid.uuid4()),
        "created_at": "1999-01-01T00:00:00Z",
        "notes_log": [],
    }
    r1 = await client.post(f"/api/v1/receipts/{s['receipt_id']}/notes", json=spoof)
    assert r1.status_code == 201
    assert r1.json()["text"] == "first"
    assert r1.json()["user_id"] == str(s["user_id"])  # the principal, not the spoof
    assert r1.json()["created_at"].startswith("20")  # server now(), not 1999

    # Notes stay writable after commit (audit trail for adjustments).
    await _force_committed(conn, s)
    r2 = await client.post(f"/api/v1/receipts/{s['receipt_id']}/notes", json={"text": "second"})
    assert r2.status_code == 201

    log = (
        await conn.execute(
            text("SELECT notes_log FROM receipts WHERE id = :r"), {"r": s["receipt_id"]}
        )
    ).scalar_one()
    assert [n["text"] for n in log] == ["first", "second"]


# ── GET detail: suggestions ───────────────────────────────────────────────────


async def test_detail_suggests_for_unmatched_lines_only(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))

    # Line 0 ("Tomatoes 5lb") is unmatched → 'Tomato' should be suggested by
    # substring containment; link line 1 → its suggestions must be empty.
    await client.put(_line_url(s, idx=1), json={"inventory_item_id": str(s["item_id"])})

    r = await client.get(f"/api/v1/receipts/{s['receipt_id']}")
    assert r.status_code == 200, r.text
    lines = {ln["line_ordinal"]: ln for ln in r.json()["lines"]}
    assert [sg["name"] for sg in lines[0]["suggestions"]] == ["Tomato"]
    assert lines[1]["suggestions"] == []
    # Review fields the FE needs are present.
    assert r.json()["reviewed_affirmation"] is False
    assert r.json()["review_started_at"] is not None
    assert r.json()["filter_flags"] == []


# ── D-606-22 interplay: a manual correction alone satisfies the commit gate ──


async def test_manual_correction_satisfies_commit_gate(
    app_instance: Any, conn: AsyncConnection, client: AsyncClient
) -> None:
    s = await _seed(conn)
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]))

    # Link line 0 (manually_corrected=true) and skip line 1 so every line is decided.
    r_link = await client.put(_line_url(s, idx=0), json={"inventory_item_id": str(s["item_id"])})
    assert r_link.status_code == 200
    r_skip = await client.put(_line_url(s, idx=1), json={"skipped": True})
    assert r_skip.status_code == 200

    # Commit with confirm but NO affirmation: the corrected line is the gate pass.
    _as(app_instance, str(s["tenant_id"]), str(s["user_id"]), role="manager")
    r_commit = await client.post(
        f"/api/v1/receipts/{s['receipt_id']}/commit",
        json={"confirm": True, "reviewed_affirmation": False},
    )
    assert r_commit.status_code == 200, r_commit.text
    assert r_commit.json()["status"] == "committed"
    assert len(r_commit.json()["movement_ids"]) == 1  # skipped line wrote nothing

    delta = (
        await conn.execute(
            text(
                "SELECT delta FROM inventory_movements "
                "WHERE tenant_id = :t AND inventory_item_id = :i AND movement_type = 'receive'"
            ),
            {"t": s["tenant_id"], "i": s["item_id"]},
        )
    ).scalar_one()
    assert float(delta) == 5.0  # qty as seeded; 'g' → 'g' conversion is the identity
