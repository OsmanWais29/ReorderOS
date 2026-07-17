"""Receipt intake live-test verifier — SELECT-only, PASS/FAIL invariants.

Usage (inside any component with DATABASE_URL, or locally):
  python -m app.ops.verify_postmark_inbound --message-id <postmark_message_id>
  python -m app.ops.verify_postmark_inbound --receipt-id <receipt_id>

--message-id walks the full email chain (inbox → attachments → drafts);
--receipt-id starts at the receipt (upload/manual intake — no inbox row exists,
so those sections and invariants are N/A). Both print the extraction jobs, a
safe line sample (ids, types, signed amounts, names/qty/unit — never body text
or bytes), adjustment links, movements, cost snapshots, and a PASS/FAIL
invariant table. Replaces the doctl-console paste scripts used during the
first live cert.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

_SAFE_LINE_KEYS = (
    "id",
    "adjusts_line_id",
    "adjustment_disposition",
    "disposition_reason",
    "line_total_cents",
    "extracted_name",
    "item_name",
    "quantity",
    "received_qty",
    "purchase_qty",
    "extracted_unit",
    "unit",
    "line_type",
    "match_status",
)


def _subset(row: dict[str, Any], keys: tuple[str, ...] | list[str]) -> dict[str, str]:
    return {k: str(row[k]) for k in keys if k in row}


async def _verify_receipt_chain(
    c: AsyncConnection,
    out: list[str],
    checks: list[tuple[str, bool]],
    rec: dict[str, Any],
) -> tuple[list[str], list[tuple[str, bool]]]:
    """Shared receipt → jobs → lines → movements → snapshots verification."""
    out.append(
        str(
            _subset(
                rec,
                [
                    "id",
                    "source",
                    "commit_state",
                    "extraction_status",
                    "manual_entry_required",
                    "review_visibility_status",
                    "file_size_bytes",
                    "confirmed_at",
                ],
            )
        )
    )
    visible = rec.get("review_visibility_status", "visible") != "suppressed"
    out.append(f"draft visible in review queue: {'yes' if visible else 'no (suppressed)'}")

    out.append("=== receipt_extraction_jobs ===")
    jobs = [
        dict(r)
        for r in (
            await c.execute(
                text(
                    "SELECT id, status, attempts, job_attempt, last_error, "
                    "raw_extraction IS NOT NULL AS raw_extraction_present "
                    "FROM receipt_extraction_jobs WHERE receipt_id = :rid"
                ),
                {"rid": rec["id"]},
            )
        )
        .mappings()
        .fetchall()
    ]
    for j in jobs:
        out.append(str(j))
    if jobs or rec["extraction_status"] not in ("none",):
        checks.append(("exactly one extraction job", len(jobs) == 1))
        ok_provider = (
            len(jobs) == 1 and jobs[0]["status"] == "complete" and jobs[0]["raw_extraction_present"]
        )
        checks.append(("extraction complete + raw_extraction present", ok_provider))
    else:
        out.append("(manual receipt — no extraction expected)")

    out.append("=== receipt_lines (safe sample) ===")
    lines = [
        dict(r)
        for r in (
            await c.execute(
                text("SELECT * FROM receipt_lines WHERE receipt_id = :rid ORDER BY line_ordinal"),
                {"rid": rec["id"]},
            )
        )
        .mappings()
        .fetchall()
    ]
    skipped = sum(1 for x in lines if x.get("match_status") == "skipped")
    out.append(f"total lines: {len(lines)} | skipped: {skipped}")
    for x in lines[:10]:
        out.append(str(_subset(x, _SAFE_LINE_KEYS)))
    bad = [
        x
        for x in lines
        if (x.get("line_type") or "item") != "item" and x.get("match_status") != "skipped"
    ]
    checks.append(("non-stock rows skipped (none receivable)", len(bad) == 0))
    # Adjustment-link sanity: only discount/credit rows may carry a link.
    bad_links = [
        x
        for x in lines
        if x.get("adjusts_line_id") is not None
        and (x.get("line_type") or "item") not in ("discount", "credit")
    ]
    checks.append(("only discount/credit rows carry adjustment links", len(bad_links) == 0))
    # Disposition summary — every discount/credit row's persisted decision is
    # part of the evidence. On a COMMITTED receipt none may be pending/NULL
    # (the Gate-1 silent-gross failure mode).
    adj_rows = [x for x in lines if (x.get("line_type") or "item") in ("discount", "credit")]
    for x in adj_rows:
        out.append(
            f"adjustment {x.get('id')}: disposition={x.get('adjustment_disposition')} "
            f"adjusts_line_id={x.get('adjusts_line_id')} "
            f"reason={x.get('disposition_reason')} amount={x.get('line_total_cents')}"
        )
    if rec["commit_state"] not in ("draft", "pending_review"):
        undecided = [x for x in adj_rows if x.get("adjustment_disposition") in (None, "pending")]
        checks.append(("committed receipt has no undecided adjustments", len(undecided) == 0))

    out.append("=== inventory_movements (via this receipt's lines) ===")
    # Movements record source_type='receipt_line', source_id=<LINE id> — join
    # through receipt_lines.emits_movement_id, never filter source_id by the
    # receipt id (the Gate-1 verifier bug: it reported 0 movements on a
    # correctly committed receipt).
    movs = [
        dict(r)
        for r in (
            await c.execute(
                text(
                    "SELECT m.* FROM inventory_movements m "
                    "JOIN receipt_lines rl ON rl.emits_movement_id = m.id "
                    "WHERE rl.receipt_id = :rid"
                ),
                {"rid": rec["id"]},
            )
        )
        .mappings()
        .fetchall()
    ]
    for m in movs:
        out.append(
            str(_subset(m, ["id", "inventory_item_id", "movement_type", "delta", "source_id"]))
        )
    out.append(f"movement count: {len(movs)}")

    out.append("=== ingredient_cost_snapshots (from this receipt's lines) ===")
    snaps = [
        dict(r)
        for r in (
            await c.execute(
                text(
                    "SELECT s.inventory_item_id, s.unit_cost_cents, s.unit_cost_cents_exact, "
                    "s.source_receipt_line_id "
                    "FROM ingredient_cost_snapshots s "
                    "JOIN receipt_lines rl ON rl.id = s.source_receipt_line_id "
                    "WHERE rl.receipt_id = :rid"
                ),
                {"rid": rec["id"]},
            )
        )
        .mappings()
        .fetchall()
    ]
    for sr in snaps:
        out.append(str(dict(sr)))
    out.append(f"snapshot count: {len(snaps)}")

    if rec["commit_state"] in ("draft", "pending_review"):
        checks.append(("draft receipt has 0 movements", len(movs) == 0))
        checks.append(("draft receipt has 0 cost snapshots", len(snaps) == 0))
    else:
        ok_commit = len(movs) > 0 and rec.get("confirmed_at") is not None
        checks.append(("committed receipt has movements + confirmed_at", ok_commit))
        receivable = [
            x
            for x in lines
            if x.get("match_status") != "skipped" and x.get("inventory_item_id") is not None
        ]
        checks.append(("movement count equals receivable line count", len(movs) == len(receivable)))

    return out, checks


async def verify(
    message_id: str | None,
    database_url: str,
    *,
    receipt_id: str | None = None,
) -> tuple[list[str], list[tuple[str, bool]]]:
    """Run all checks; returns (report_lines, [(invariant, ok)]). SELECT-only."""
    out: list[str] = []
    checks: list[tuple[str, bool]] = []

    url = database_url
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    if "sslmode" in url:
        url = url.split("?")[0]
    engine = create_async_engine(
        url, connect_args={"ssl": "require"} if "ondigitalocean" in url else {}
    )

    try:
        async with engine.connect() as c:
            if receipt_id is not None:
                out.append("=== receipt (direct --receipt-id mode; no email chain) ===")
                recs = [
                    dict(r)
                    for r in (
                        await c.execute(
                            text("SELECT * FROM receipts WHERE id = :rid"),
                            {"rid": receipt_id},
                        )
                    )
                    .mappings()
                    .fetchall()
                ]
                checks.append(("exactly one receipt", len(recs) == 1))
                if not recs:
                    return out, checks
                return await _verify_receipt_chain(c, out, checks, recs[0])

            out.append("=== inbound_email_inbox ===")
            inbox_rows = [
                dict(r)
                for r in (
                    await c.execute(
                        text("SELECT * FROM inbound_email_inbox WHERE postmark_message_id = :mid"),
                        {"mid": message_id},
                    )
                )
                .mappings()
                .fetchall()
            ]
            for r in inbox_rows:
                out.append(
                    str(
                        _subset(
                            r,
                            [
                                "id",
                                "tenant_id",
                                "processing_status",
                                "attachment_count",
                                "skip_reason",
                                "filter_flags",
                                "last_error",
                                "created_at",
                            ],
                        )
                    )
                )
            checks.append(("exactly one inbox row", len(inbox_rows) == 1))
            if len(inbox_rows) != 1:
                return out, checks
            inbox = inbox_rows[0]

            out.append("=== inbound_email_attachments ===")
            atts = [
                dict(r)
                for r in (
                    await c.execute(
                        text(
                            "SELECT attachment_index, original_filename, mime_type, "
                            "object_key IS NOT NULL AS stored, receipt_id "
                            "FROM inbound_email_attachments WHERE inbound_email_id = :iid "
                            "ORDER BY attachment_index"
                        ),
                        {"iid": inbox["id"]},
                    )
                )
                .mappings()
                .fetchall()
            ]
            for a in atts:
                out.append(str(a))
            checks.append(("exactly one qualifying attachment", len(atts) == 1))

            out.append("=== receipts ===")
            recs = [
                dict(r)
                for r in (
                    await c.execute(
                        text("SELECT * FROM receipts WHERE inbound_email_id = :iid"),
                        {"iid": inbox["id"]},
                    )
                )
                .mappings()
                .fetchall()
            ]
            checks.append(("exactly one receipt draft", len(recs) == 1))
            if not recs:
                return out, checks
            rec = recs[0]
            checks.append(("receipt source = email (Postmark)", rec["source"] == "email"))
            linked = {str(a["receipt_id"]) for a in atts}
            checks.append(("no duplicate drafts", len(recs) == 1 and linked == {str(rec["id"])}))
            return await _verify_receipt_chain(c, out, checks, rec)
    finally:
        await engine.dispose()


def format_report(out: list[str], checks: list[tuple[str, bool]]) -> str:
    lines = list(out)
    lines.append("")
    lines.append("=== INVARIANTS ===")
    for name, ok in checks:
        lines.append(("PASS " if ok else "FAIL ") + name)
    lines.append("")
    lines.append("OVERALL: " + ("PASS" if all(ok for _, ok in checks) else "FAIL"))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message-id", help="postmark_message_id to verify (email chain)")
    parser.add_argument("--receipt-id", help="receipt id to verify (upload/manual intake)")
    args = parser.parse_args()
    if not args.message_id and not args.receipt_id:
        print("provide --message-id or --receipt-id")
        return 2
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set")
        return 2
    out, checks = asyncio.run(verify(args.message_id, database_url, receipt_id=args.receipt_id))
    print(format_report(out, checks))
    return 0 if checks and all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
