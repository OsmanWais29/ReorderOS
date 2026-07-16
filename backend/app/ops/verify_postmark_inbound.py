"""Postmark inbound live-test verifier — SELECT-only, PASS/FAIL invariants.

Usage (inside any component with DATABASE_URL, or locally):
  python -m app.ops.verify_postmark_inbound --message-id <postmark_message_id>

Prints the inbox row, attachments, linked receipts, extraction jobs, a safe
receipt-line sample (item names/qty/unit only — never body text or bytes), the
movement state, and a PASS/FAIL invariant table. Replaces the doctl-console
paste scripts used during the first live cert.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

_SAFE_LINE_KEYS = (
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


async def verify(message_id: str, database_url: str) -> tuple[list[str], list[tuple[str, bool]]]:
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
            for r in recs:
                out.append(
                    str(
                        _subset(
                            r,
                            [
                                "id",
                                "source",
                                "commit_state",
                                "extraction_status",
                                "manual_entry_required",
                                "review_visibility_status",
                                "file_size_bytes",
                            ],
                        )
                    )
                )
            checks.append(("exactly one receipt draft", len(recs) == 1))
            if not recs:
                return out, checks
            rec = recs[0]
            checks.append(("receipt source = email (Postmark)", rec["source"] == "email"))
            linked = {str(a["receipt_id"]) for a in atts}
            checks.append(("no duplicate drafts", len(recs) == 1 and linked == {str(rec["id"])}))
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
            checks.append(("exactly one extraction job", len(jobs) == 1))
            ok_provider = (
                len(jobs) == 1
                and jobs[0]["status"] == "complete"
                and jobs[0]["raw_extraction_present"]
            )
            checks.append(("extraction complete + raw_extraction present", ok_provider))

            out.append("=== receipt_lines (safe sample) ===")
            lines = [
                dict(r)
                for r in (
                    await c.execute(
                        text("SELECT * FROM receipt_lines WHERE receipt_id = :rid"),
                        {"rid": rec["id"]},
                    )
                )
                .mappings()
                .fetchall()
            ]
            skipped = sum(1 for x in lines if x.get("match_status") == "skipped")
            out.append(f"total lines: {len(lines)} | skipped: {skipped}")
            for x in lines[:5]:
                out.append(str(_subset(x, _SAFE_LINE_KEYS)))
            bad = [
                x
                for x in lines
                if (x.get("line_type") or "item") != "item" and x.get("match_status") != "skipped"
            ]
            checks.append(("non-stock rows skipped (none receivable)", len(bad) == 0))

            out.append("=== inventory_movements (source = this receipt) ===")
            movs = [
                dict(r)
                for r in (
                    await c.execute(
                        text("SELECT * FROM inventory_movements WHERE source_id = :rid"),
                        {"rid": rec["id"]},
                    )
                )
                .mappings()
                .fetchall()
            ]
            out.append(f"movement count: {len(movs)}")
            if rec["commit_state"] in ("draft", "pending_review"):
                checks.append(("draft receipt has 0 movements", len(movs) == 0))
            else:
                ok_commit = len(movs) > 0 and rec.get("confirmed_at") is not None
                checks.append(("committed receipt has movements + confirmed_at", ok_commit))
    finally:
        await engine.dispose()

    return out, checks


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
    parser.add_argument("--message-id", required=True, help="postmark_message_id to verify")
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set")
        return 2
    out, checks = asyncio.run(verify(args.message_id, database_url))
    print(format_report(out, checks))
    return 0 if checks and all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
