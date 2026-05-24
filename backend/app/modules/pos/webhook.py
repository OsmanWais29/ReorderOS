"""Clover webhook endpoint.

  POST /api/v1/webhooks/pos/clover

Two responsibilities:
  1. Respond to Clover's one-time verification ping (verificationCode echo).
  2. Accept signed event payloads, look up the tenant, and insert each
     order event into pos_event_inbox for async processing by the inbox worker.

Design decisions:
  - Uses the service_worker pool (no SET LOCAL required — pos_event_inbox
    has USING(true) RLS for service_worker).
  - Deduplication via ON CONFLICT DO NOTHING on the 5-column unique key
    (tenant_id, vendor, vendor_event_id, vendor_event_type, vendor_ts).
    Two UPDATEs for the same order at different timestamps each produce
    a separate row; identical events (replays) are silently ignored.
  - Only "O" (order) object_type events are processed. Clover sends events
    for Items (I), Payments (P), etc. — those are not needed in Sprint 4.
  - hmac.compare_digest for constant-time auth comparison (prevents timing attacks).
"""

from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import text
from uuid6 import uuid7

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.service_db import get_service_sessionmaker

log = get_logger(__name__)
router = APIRouter(prefix="/webhooks/pos", tags=["webhooks"])


@router.post("/clover")
async def handle_clover_webhook(request: Request) -> Response:
    """Handle incoming Clover webhook events.

    Clover sends a POST with JSON body. The body has two forms:
      - Setup verification: {"verificationCode": "<code>"}
        → echo the code as text/plain (Clover confirms delivery)
      - Event payload: {"merchants": {"<merchant_id>": [<events>]}}
        → validate auth, store each order event in pos_event_inbox

    Returns 200 for both forms. Returns 401 only when auth fails on an
    event payload (not for verificationCode, which has no auth header).
    """
    from fastapi import HTTPException

    raw_body = await request.body()
    payload = json.loads(raw_body)

    # ── Verification ping ─────────────────────────────────────────────────────
    if "verificationCode" in payload:
        return Response(
            content=payload["verificationCode"],
            status_code=200,
            media_type="text/plain",
        )

    # ── Auth validation ───────────────────────────────────────────────────────
    settings = get_settings()
    received_auth = request.headers.get("X-Clover-Auth", "")
    expected_auth = settings.clover_webhook_auth_code or ""

    if not expected_auth or not received_auth:
        raise HTTPException(status_code=401, detail="Missing webhook auth")
    if not hmac.compare_digest(received_auth, expected_auth):
        raise HTTPException(status_code=401, detail="Invalid webhook auth")

    # ── Event processing ──────────────────────────────────────────────────────
    # Defensive: Clover payload shape may vary; never 500 on malformed input.
    merchants = payload.get("merchants", {})
    if not isinstance(merchants, dict):
        return Response(status_code=200)

    sm = get_service_sessionmaker()
    async with sm() as session:
        for merchant_id, events in merchants.items():
            # Defensive: events must be a list of dicts.
            if not isinstance(events, list):
                continue

            # Resolve merchant → tenant via SECURITY DEFINER function.
            # No SET LOCAL required — runs as function owner (superuser).
            tenant_id = (await session.execute(
                text("SELECT lookup_tenant_by_merchant('clover', :m)"),
                {"m": merchant_id},
            )).scalar_one_or_none()

            if tenant_id is None:
                log.warning("webhook.unknown_merchant", merchant_id=merchant_id)
                continue

            # Fetch connection_id for the FK (nullable — not all events will
            # have a valid connection if it was recently revoked).
            conn_row = (await session.execute(
                text("""
                    SELECT connection_id FROM tenant_pos_connections
                    WHERE vendor = 'clover' AND merchant_id = :m
                      AND state IN ('active', 'error')
                    LIMIT 1
                """),
                {"m": merchant_id},
            )).fetchone()
            connection_id = str(conn_row.connection_id) if conn_row else None

            for event in events:
                # Defensive: each event must be a dict.
                if not isinstance(event, dict):
                    continue

                # Defensive: objectId may be None or missing.
                object_id = event.get("objectId") or ""
                if ":" not in object_id:
                    continue

                obj_type, obj_id = object_id.split(":", 1)
                if obj_type != "O":
                    # Only order events in Sprint 4.
                    continue

                # Defensive: type and ts may be wrong types from future Clover versions.
                event_type = event.get("type") or "UPDATE"
                if not isinstance(event_type, str) or event_type not in ("CREATE", "UPDATE", "DELETE"):
                    event_type = "UPDATE"
                try:
                    event_ts = int(event.get("ts") or 0)
                except (TypeError, ValueError):
                    event_ts = 0

                # ON CONFLICT DO NOTHING handles the 5-column dedup key
                # (tenant_id, vendor, vendor_event_id, vendor_event_type, vendor_ts).
                # Two UPDATEs at different ts both insert; same ts → NOTHING.
                result = await session.execute(
                    text("""
                        INSERT INTO pos_event_inbox (
                            inbox_id, tenant_id, connection_id, vendor,
                            vendor_event_id, vendor_object_type, vendor_event_type,
                            vendor_ts, raw_payload, signature_verified, source
                        ) VALUES (
                            :id, :tid, :cid, 'clover',
                            :oid, :otype, :etype, :ets,
                            CAST(:payload AS jsonb), true, 'webhook'
                        )
                        ON CONFLICT ON CONSTRAINT pos_inbox_dedup_unique DO NOTHING
                        RETURNING inbox_id
                    """),
                    {
                        "id": str(uuid7()),
                        "tid": str(tenant_id),
                        "cid": connection_id,
                        "oid": obj_id,
                        "otype": obj_type,
                        "etype": event_type,
                        "ets": event_ts,
                        "payload": json.dumps(payload),
                    },
                )
                if result.fetchone() is None:
                    log.info(
                        "webhook.duplicate",
                        event_id=obj_id,
                        event_type=event_type,
                        vendor_ts=event_ts,
                    )

        await session.commit()

    return Response(status_code=200)
