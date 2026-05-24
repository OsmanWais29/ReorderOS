"""Reconciliation service — backfills missed Clover orders into pos_event_inbox.

Design rules:
  - reconcile_connection(connection_id: str) — never raises; errors are caught
    and logged internally so run_all() always completes all connections.
  - last_reconciliation_at is updated on every run (success or failure) so
    ops can see the job ran even when it found nothing or errored.
  - last_reconciliation_cursor is only updated on SUCCESS; errors leave it
    unchanged so the next run re-tries from the same window.
  - Cursor never moves backward: new_cursor = max(existing_cursor, observed_ms).
  - Non-initial runs use a 5-minute overlap window (start_ms = cursor - OVERLAP_MS)
    to catch boundary orders that were committed during the previous run's API call.
    The cursor itself only advances — it never regresses to start_ms.
  - MAX_PAGES safety valve prevents a single merchant with a Clover API bug from
    wedging the worker in an infinite pagination loop.
  - fetched_payload is pre-populated so the inbox worker skips the Clover
    API call (Phase 7 Test 6 proves this behaviour is used).
  - ON CONFLICT requeue logic matches reconciliation_upsert() in
    tests/helpers/pos_inbox.py exactly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from app.core.encryption import TokenEncryption
from app.core.logging import get_logger
from app.core.service_db import get_service_sessionmaker
from app.modules.pos.clover_client import CloverClient
from uuid6 import uuid7

log = get_logger(__name__)


class ReconciliationService:
    BATCH_SIZE: int = 100
    INITIAL_LOOKBACK_DAYS: int = 30
    # Overlap window for non-initial syncs: re-fetch orders from this many ms
    # before the stored cursor to catch orders that were mid-commit last run.
    OVERLAP_MS: int = 5 * 60 * 1000  # 5 minutes
    # Safety valve: stop after this many pages regardless of Clover responses.
    MAX_PAGES: int = 50

    # ── Public entry points ───────────────────────────────────────────────────

    async def run_all(self) -> None:
        """Reconcile every active connection; isolate failures per-connection."""
        sm = get_service_sessionmaker()
        async with sm() as session:
            result = await session.execute(text("""
                SELECT connection_id
                FROM tenant_pos_connections
                WHERE state = 'active'
                ORDER BY
                    COALESCE(last_reconciliation_at,
                             '-infinity'::timestamptz) ASC,
                    created_at ASC
                FOR UPDATE SKIP LOCKED
            """))
            ids = [str(r[0]) for r in result.fetchall()]

        for cid in ids:
            try:
                await self.reconcile_connection(cid)
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "reconciliation.run_all_error",
                    connection_id=cid,
                    error=str(exc)[:500],
                )

    async def reconcile_connection(self, connection_id: str) -> None:
        """Reconcile one connection: fetch orders since cursor, upsert inbox.

        Never raises — all API and processing errors are caught internally.
        """
        conn = await self._get_connection(connection_id)
        if conn is None:
            log.warning(
                "reconciliation.connection_not_found",
                connection_id=connection_id,
            )
            return

        enc = TokenEncryption()
        tenant_id = str(conn["tenant_id"])
        merchant_id = str(conn["merchant_id"])
        environment = str(conn["environment"])

        try:
            access_token = enc.decrypt(conn["access_token_enc"])
        except Exception as exc:  # noqa: BLE001
            log.error(
                "reconciliation.decrypt_failed",
                connection_id=connection_id,
                error=str(exc)[:200],
            )
            return

        raw_cursor = conn.get("last_reconciliation_cursor")
        is_initial = raw_cursor is None
        existing_cursor: int = 0 if is_initial else int(raw_cursor)

        if is_initial:
            start_ms = int(
                (datetime.now(timezone.utc)
                 - timedelta(days=self.INITIAL_LOOKBACK_DAYS)
                 ).timestamp() * 1000
            )
        else:
            # Overlap: start 5 min before the stored cursor to catch orders
            # that were being committed when the previous run fetched its last
            # page. The cursor itself only advances — start_ms is for the API
            # filter only.
            start_ms = max(0, existing_cursor - self.OVERLAP_MS)

        if is_initial:
            await self._mark_initial_sync_started(connection_id)

        clover = CloverClient(
            access_token=access_token,
            merchant_id=merchant_id,
            environment=environment,
        )

        # new_cursor tracks the highest modifiedTime we've successfully stored.
        # Initialised to existing_cursor for non-initial runs so it never regresses
        # even if all observed modifiedTimes are older than the stored cursor.
        new_cursor = existing_cursor
        total_upserted = 0
        success = True

        try:
            offset = 0
            page_num = 0
            while True:
                if page_num >= self.MAX_PAGES:
                    log.warning(
                        "reconciliation.max_pages_reached",
                        connection_id=connection_id,
                        pages=page_num,
                    )
                    break

                orders = await clover.list_orders(
                    since_ms=start_ms,
                    offset=offset,
                    limit=self.BATCH_SIZE,
                )

                for order in orders:
                    if not isinstance(order, dict):
                        continue
                    if (order.get("state") or "").lower() != "locked":
                        continue

                    modified_ms = _safe_ms(order.get("modifiedTime"))
                    if modified_ms > new_cursor:
                        new_cursor = modified_ms

                    await self._upsert_inbox(
                        tenant_id=tenant_id,
                        connection_id=connection_id,
                        order=order,
                        modified_ms=modified_ms,
                    )
                    total_upserted += 1

                if len(orders) < self.BATCH_SIZE:
                    break
                offset += self.BATCH_SIZE
                page_num += 1

        except Exception as exc:  # noqa: BLE001
            log.error(
                "reconciliation.api_error",
                connection_id=connection_id,
                tenant_id=tenant_id,
                error=str(exc)[:500],
            )
            success = False

        await self._persist_run_result(
            connection_id=connection_id,
            new_cursor=new_cursor,
            is_initial=is_initial,
            success=success,
        )

        if success:
            log.info(
                "reconciliation.complete",
                connection_id=connection_id,
                tenant_id=tenant_id,
                upserted=total_upserted,
                new_cursor=new_cursor,
                is_initial=is_initial,
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _get_connection(self, connection_id: str) -> Any:
        sm = get_service_sessionmaker()
        async with sm() as session:
            result = await session.execute(
                text("""
                    SELECT connection_id, tenant_id, merchant_id, environment,
                           access_token_enc, last_reconciliation_cursor,
                           initial_sync_completed_at
                    FROM tenant_pos_connections
                    WHERE connection_id = :cid AND state = 'active'
                """),
                {"cid": connection_id},
            )
            return result.mappings().fetchone()

    async def _mark_initial_sync_started(self, connection_id: str) -> None:
        sm = get_service_sessionmaker()
        async with sm() as session:
            await session.execute(
                text("""
                    UPDATE tenant_pos_connections
                    SET initial_sync_started_at = now()
                    WHERE connection_id = :cid
                      AND initial_sync_started_at IS NULL
                """),
                {"cid": connection_id},
            )
            await session.commit()

    async def _persist_run_result(
        self,
        connection_id: str,
        new_cursor: int,
        is_initial: bool,
        success: bool,
    ) -> None:
        """Update timestamps (always) and cursor (only on success)."""
        sm = get_service_sessionmaker()
        async with sm() as session:
            if success:
                await session.execute(
                    text("""
                        UPDATE tenant_pos_connections
                        SET last_reconciliation_at    = now(),
                            last_reconciliation_cursor = :cursor,
                            initial_sync_completed_at  = CASE
                                WHEN :is_initial
                                     AND initial_sync_completed_at IS NULL
                                THEN now()
                                ELSE initial_sync_completed_at
                            END
                        WHERE connection_id = :cid
                    """),
                    {
                        "cursor": new_cursor,
                        "is_initial": is_initial,
                        "cid": connection_id,
                    },
                )
            else:
                await session.execute(
                    text("""
                        UPDATE tenant_pos_connections
                        SET last_reconciliation_at = now()
                        WHERE connection_id = :cid
                    """),
                    {"cid": connection_id},
                )
            await session.commit()

    async def _upsert_inbox(
        self,
        tenant_id: str,
        connection_id: str,
        order: dict[str, Any],
        modified_ms: int,
    ) -> None:
        """Upsert one order into pos_event_inbox with source='reconciliation'.

        Matches reconciliation_upsert() in tests/helpers/pos_inbox.py exactly.
        """
        sm = get_service_sessionmaker()
        async with sm() as session:
            await session.execute(
                text("""
                    INSERT INTO pos_event_inbox (
                        inbox_id, tenant_id, connection_id, vendor,
                        vendor_event_id, vendor_object_type, vendor_event_type,
                        vendor_ts, raw_payload, fetched_payload, fetched_at,
                        signature_verified, source
                    ) VALUES (
                        :id, :tid, :cid, 'clover', :eid, 'O', 'UPDATE',
                        :ts,
                        '{"source":"reconciliation"}'::jsonb,
                        CAST(:payload AS jsonb), now(),
                        false, 'reconciliation'
                    )
                    ON CONFLICT ON CONSTRAINT pos_inbox_dedup_unique
                    DO UPDATE SET
                        fetched_payload = EXCLUDED.fetched_payload,
                        fetched_at      = now(),
                        state = CASE
                            WHEN pos_event_inbox.state
                                 IN ('processed', 'dead_letter')
                            THEN 'pending'
                            ELSE pos_event_inbox.state
                        END,
                        next_attempt_at = CASE
                            WHEN pos_event_inbox.state
                                 IN ('processed', 'dead_letter')
                            THEN now()
                            ELSE pos_event_inbox.next_attempt_at
                        END
                """),
                {
                    "id": str(uuid7()),
                    "tid": tenant_id,
                    "cid": connection_id,
                    "eid": str(order["id"]),
                    "ts": modified_ms,
                    "payload": json.dumps(order),
                },
            )
            await session.commit()


def _safe_ms(v: Any) -> int:
    if v is None:
        return 0
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0
