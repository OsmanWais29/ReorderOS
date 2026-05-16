"""Request-level idempotency for inventory write endpoints.

Protocol
--------
1. Client sends ``Idempotency-Key: <opaque string>`` header.
2. Fingerprint = SHA-256(METHOD \\n PATH \\n canonical_json_body).
   Canonical JSON: keys sorted recursively, arrays kept in order,
   no trailing zeros, UTF-8 NFC strings.
3. Lookup (tenant_id, key) in idempotency_keys:
   - Not found                              → INSERT row (response_status=NULL), proceed.
   - Found, same fingerprint, status NOT NULL → replay cached response.
   - Found, same fingerprint, status IS NULL  → 409 request_in_flight.
   - Found, different fingerprint             → 409 Conflict.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

# ── fingerprint ───────────────────────────────────────────────────────────────


def _canonical(obj: Any) -> Any:
    """Recursively sort object keys; NFC-normalise strings; preserve arrays."""
    if isinstance(obj, dict):
        return {k: _canonical(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [_canonical(v) for v in obj]
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    return obj


def compute_fingerprint(method: str, path: str, body: bytes | dict) -> str:
    """Compute a stable fingerprint for a request.

    *body* may be raw bytes (from ``await request.body()`` in route handlers)
    or a plain dict (for direct calls in tests / services).
    """
    if isinstance(body, dict):
        canonical_str = json.dumps(_canonical(body), separators=(",", ":"), ensure_ascii=False)
    else:
        try:
            parsed = json.loads(body) if body else {}
            canonical_str = json.dumps(
                _canonical(parsed), separators=(",", ":"), ensure_ascii=False
            )
        except Exception:
            canonical_str = body.decode("utf-8", errors="replace")
    raw = f"{method}\n{path}\n{canonical_str}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── result dataclass ──────────────────────────────────────────────────────────


@dataclass
class IdempotencyState:
    status: str  # 'proceed' | 'cached' | 'in_flight' | 'conflict'
    response_status: int | None = None
    response_body: dict[str, Any] | None = field(default=None)


# ── DB operations ─────────────────────────────────────────────────────────────


async def check_and_lock(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    key: str,
    fingerprint: str,
) -> IdempotencyState:
    """Atomic check-and-lock of an idempotency key.

    Inserts a pending row when the key is new.  Raises 409 immediately for
    conflicts so the caller never reaches the handler body.
    """
    # Try optimistic insert inside a SAVEPOINT.
    # Using begin_nested() means a duplicate-key error only rolls back the
    # savepoint, not the caller's outer transaction.  This is critical when
    # tests bind the session to a connection whose outer transaction is used
    # for test-data isolation via rollback.
    try:
        async with session.begin_nested():
            await session.execute(
                text("""
                    INSERT INTO idempotency_keys (tenant_id, key, request_fingerprint)
                    VALUES (:tid, :key, :fp)
                """),
                {"tid": tenant_id, "key": key, "fp": fingerprint},
            )
        return IdempotencyState(status="proceed")
    except IntegrityError:
        pass  # key already exists — fall through to read it

    # Key already exists — read what's there.
    row = await session.execute(
        text("""
            SELECT request_fingerprint, response_status, response_body
              FROM idempotency_keys
             WHERE tenant_id = :tid AND key = :key
        """),
        {"tid": tenant_id, "key": key},
    )
    existing = row.fetchone()
    if existing is None:
        # Extremely rare race: another process deleted it. Treat as proceed.
        return IdempotencyState(status="proceed")

    stored_fp, resp_status, resp_body = existing
    if stored_fp != fingerprint:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "idempotency_key_conflict",
                "message": "Idempotency-Key already used with a different request body",
            },
        )
    if resp_status is None:
        raise HTTPException(
            status_code=409,
            detail={"error": "request_in_flight", "retry_after_seconds": 5},
        )
    # Completed replay.
    return IdempotencyState(
        status="cached",
        response_status=resp_status,
        response_body=resp_body,
    )


async def store_response(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    key: str,
    response_status: int,
    response_body: dict[str, Any],
) -> None:
    """Persist the completed response so future replays can short-circuit."""
    await session.execute(
        text("""
            UPDATE idempotency_keys
               SET response_status = :status,
                   response_body   = CAST(:body AS jsonb)
             WHERE tenant_id = :tid AND key = :key
        """),
        {
            "tid": tenant_id,
            "key": key,
            "status": response_status,
            "body": json.dumps(response_body),
        },
    )
    await session.flush()
