"""Helpers for Phase 2+ tests: pos_event_inbox row factory.

Matches the exact function signatures from the Sprint 4 Phase 2 spec.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from uuid6 import uuid7


def make_inbox_row(overrides: dict | None = None) -> dict:
    """Build a valid pos_event_inbox row with sensible defaults.

    vendor_ts defaults to current epoch milliseconds (Clover format).
    State defaults to 'pending' — the only state that doesn't require
    additional columns (processed_at etc.) to satisfy CHECK constraints.
    Tests that need other states do a separate UPDATE after inserting.
    """
    now_ms = int(time.time() * 1000)
    defaults: dict[str, Any] = {
        "inbox_id": str(uuid7()),
        "tenant_id": None,  # must be set by test
        "connection_id": None,
        "vendor": "clover",
        "vendor_event_id": f"order_{uuid.uuid4().hex[:8]}",
        "vendor_object_type": "O",
        "vendor_event_type": "CREATE",
        "vendor_ts": now_ms,
        "raw_payload": json.dumps({"source": "test"}),
        "signature_verified": True,
        "source": "webhook",
        "state": "pending",
        "retry_count": 0,
    }
    if overrides:
        defaults.update(overrides)
    if isinstance(defaults["raw_payload"], dict):
        defaults["raw_payload"] = json.dumps(defaults["raw_payload"])
    return defaults


async def insert_inbox(conn: Any, row: dict) -> None:
    """INSERT a row into pos_event_inbox.

    Only inserts the non-defaulted columns. state defaults to 'pending',
    retry_count to 0. Tests that need specific states do UPDATE afterwards.
    This keeps the helper clean and makes test intent explicit.
    """
    await conn.execute(
        """
        INSERT INTO pos_event_inbox (
            inbox_id, tenant_id, connection_id, vendor,
            vendor_event_id, vendor_object_type, vendor_event_type,
            vendor_ts, raw_payload, signature_verified, source
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11)
    """,
        row["inbox_id"],
        row["tenant_id"],
        row.get("connection_id"),
        row["vendor"],
        row["vendor_event_id"],
        row["vendor_object_type"],
        row["vendor_event_type"],
        row["vendor_ts"],
        row["raw_payload"],
        row["signature_verified"],
        row["source"],
    )


async def reconciliation_upsert(conn: Any, row: dict) -> None:
    """Reconciliation INSERT with ON CONFLICT DO UPDATE.

    Exact SQL from the Sprint 4 spec Phase 8 ReconciliationService.
    Normal case: INSERT creates a new row (new modifiedTime as vendor_ts).
    Rare collision: ON CONFLICT updates fetched_payload and conditionally
    requeues 'processed'/'dead_letter' → 'pending'. Leaves 'pending',
    'processing', 'failed' states unchanged.
    """
    await conn.execute(
        """
        INSERT INTO pos_event_inbox (
            inbox_id, tenant_id, connection_id, vendor,
            vendor_event_id, vendor_object_type, vendor_event_type,
            vendor_ts, raw_payload, fetched_payload, fetched_at,
            signature_verified, source
        ) VALUES (
            $1, $2, $3, 'clover', $4, 'O', 'UPDATE',
            $5, $6::jsonb, $7::jsonb, now(), false, 'reconciliation'
        )
        ON CONFLICT ON CONSTRAINT pos_inbox_dedup_unique
        DO UPDATE SET
            fetched_payload = EXCLUDED.fetched_payload,
            fetched_at = now(),
            state = CASE
                WHEN pos_event_inbox.state IN ('processed', 'dead_letter')
                THEN 'pending'
                ELSE pos_event_inbox.state
            END,
            next_attempt_at = CASE
                WHEN pos_event_inbox.state IN ('processed', 'dead_letter')
                THEN now()
                ELSE pos_event_inbox.next_attempt_at
            END
    """,
        str(uuid7()),  # $1 inbox_id (ignored on conflict)
        row["tenant_id"],  # $2
        row.get("connection_id"),  # $3
        row["vendor_event_id"],  # $4
        row["vendor_ts"],  # $5
        row.get("raw_payload", json.dumps({"source": "reconciliation"})),  # $6
        row.get("fetched_payload", json.dumps({"reconciled": True})),  # $7
    )
