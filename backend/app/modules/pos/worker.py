"""Inbox worker — claims and processes pos_event_inbox rows.

Design rules:
  - service_worker pool for all DB work.
  - SELECT set_config('app.tenant_id', :tid, true) before every orders /
    sale_line_items write — both use T1 RLS for service_worker.
  - Never synthesize clover_line_item_id. Skip items with missing/empty id.
  - CAST(:payload AS jsonb) — never ::jsonb (SQLAlchemy asyncpg dialect bug).
  - datetime.now(timezone.utc) — never datetime.utcnow().
  - ON CONFLICT payment_state guard: never revert a higher-priority state
    (e.g., REFUNDED must not become PAID on replay).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from uuid6 import uuid7

from app.core.encryption import TokenEncryption
from app.core.logging import get_logger
from app.core.service_db import get_service_sessionmaker
from app.modules.inventory.depletion import handler
from app.modules.pos.clover_client import (
    CloverClient,
    OrderNotFoundError,
    RateLimitedError,
    TokenExpiredError,
)

log = get_logger(__name__)

# Payment state priority — higher = more terminal.
# ON CONFLICT guard uses this to prevent stale events from reverting state.
_PAYMENT_STATE_PRIORITY = {
    "REFUNDED": 5,
    "PARTIALLY_REFUNDED": 4,
    "CREDITED": 3,
    "PAID": 2,
    "OPEN": 1,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _from_ms(ms: int | None) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def _safe_int(v: Any) -> int:
    if v is None:
        return 0
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0


def _safe_float(v: Any) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


class InboxWorker:
    """Async inbox worker.  One instance per worker process."""

    CLAIM_TTL_SECONDS = 300
    MAX_RETRIES = 5

    async def run(self) -> None:
        """Main loop — runs until cancelled."""
        while True:
            events = await self.claim_batch(batch_size=10)
            if not events:
                await asyncio.sleep(2)
                continue
            for event in events:
                try:
                    await self.process_event(event)
                except Exception as exc:
                    await self.mark_failed(event, f"Unhandled: {type(exc).__name__}: {exc!s}")

    # ── Claim ─────────────────────────────────────────────────────────────────

    async def claim_batch(self, batch_size: int = 10) -> list[Any]:
        """Atomically claim up to batch_size pending / stale-processing events.

        Returns RowMapping objects that support both row["key"] and row.key.
        """
        now = _now()
        expires = now + timedelta(seconds=self.CLAIM_TTL_SECONDS)
        sm = get_service_sessionmaker()
        async with sm() as session:
            result = await session.execute(
                text("""
                    UPDATE pos_event_inbox
                    SET state                 = 'processing',
                        processing_started_at = :now,
                        claim_expires_at      = :exp
                    WHERE inbox_id IN (
                        SELECT inbox_id FROM pos_event_inbox
                        WHERE (
                            (state = 'pending'
                             AND (next_attempt_at IS NULL OR next_attempt_at <= :now))
                            OR
                            (state = 'processing' AND claim_expires_at < :now)
                        )
                        AND vendor_object_type = 'O'
                        ORDER BY received_at ASC
                        LIMIT :batch
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING *
                """),
                {"now": now, "exp": expires, "batch": batch_size},
            )
            rows = result.mappings().all()
            await session.commit()
        return list(rows)

    # ── Process ───────────────────────────────────────────────────────────────

    async def process_event(self, event: Any) -> None:
        """Fetch the order from Clover and persist it."""
        tenant_id = str(event["tenant_id"])
        order_id = str(event["vendor_event_id"])
        inbox_id = str(event["inbox_id"])

        # ── 1. Use pre-fetched payload or fetch from Clover ───────────────
        pre_fetched = event.get("fetched_payload")
        if pre_fetched:
            order_data: dict[str, Any] = pre_fetched
        else:
            sm = get_service_sessionmaker()
            async with sm() as session:
                conn_row = (
                    await session.execute(
                        text("""
                            SELECT connection_id, access_token_enc,
                                   merchant_id, environment
                            FROM tenant_pos_connections
                            WHERE tenant_id = :tid AND vendor = 'clover'
                              AND state IN ('active', 'error')
                            LIMIT 1
                        """),
                        {"tid": tenant_id},
                    )
                ).fetchone()

            if conn_row is None:
                await self.mark_failed(event, "No active Clover connection for tenant")
                return

            enc = TokenEncryption()
            access_token = enc.decrypt(conn_row.access_token_enc)
            clover = CloverClient(
                access_token=access_token,
                merchant_id=conn_row.merchant_id,
                environment=conn_row.environment,
            )

            try:
                order_data = await clover.get_order(order_id)
            except OrderNotFoundError:
                # Order deleted from Clover — ACK cleanly, do not write to orders.
                await self.mark_processed(event)
                return
            except TokenExpiredError as exc:
                await self.mark_failed(event, str(exc))
                return
            except RateLimitedError as exc:
                await self.mark_failed(event, str(exc))
                return
            except Exception as exc:
                await self.mark_failed(event, f"{type(exc).__name__}: {exc!s}")
                return

            # ── 2. Persist fetched payload so replays skip the API call ──
            sm2 = get_service_sessionmaker()
            async with sm2() as session:
                await session.execute(
                    text("""
                        UPDATE pos_event_inbox
                        SET fetched_payload = CAST(:payload AS jsonb),
                            fetched_at      = :now
                        WHERE inbox_id = :id
                    """),
                    {
                        "payload": json.dumps(order_data),
                        "now": _now(),
                        "id": inbox_id,
                    },
                )
                await session.commit()

        # ── 3. Skip non-locked orders (open orders don't write to `orders`) ─
        order_state = (order_data.get("state") or "").lower()
        if order_state != "locked":
            await self.mark_processed(event)
            return

        # ── 4. Upsert order + line items + inventory effects ──────────────
        # vendor_ts is the POS business-event timestamp — passed as recorded_at
        # so the three-timestamp model (recorded_at vs created_at) is preserved.
        created_ms = order_data.get("clientCreatedTime") or order_data.get("createdTime")
        vendor_ts: datetime | None = _from_ms(_safe_int(created_ms) if created_ms else None)

        try:
            sm3 = get_service_sessionmaker()
            async with sm3() as session, session.begin():
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :tid, true)"),
                    {"tid": tenant_id},
                )
                order_row_id = await self._upsert_order(session, tenant_id, inbox_id, order_data)
                line_items = (order_data.get("lineItems") or {}).get("elements") or []
                for li in line_items:
                    if not isinstance(li, dict):
                        continue
                    li_id = li.get("id") or ""
                    if not li_id:
                        log.warning(
                            "worker.skip_line_item_missing_id",
                            order_id=order_id,
                            tenant_id=tenant_id,
                        )
                        continue
                    sli_id = await self._insert_line_item(
                        session, tenant_id, order_row_id, li
                    )
                    is_refunded = bool(li.get("refunded", False))
                    is_voided = bool(li.get("exchanged", False))
                    if sli_id and not is_refunded and not is_voided:
                        await handler.emit_inventory_effects(
                            session, tenant_id, sli_id, vendor_ts
                        )
        except Exception as exc:
            await self.mark_failed(event, f"{type(exc).__name__}: {exc!s}")
            return

        await self.mark_processed(event)

    # ── Order upsert ──────────────────────────────────────────────────────────

    async def _upsert_order(
        self,
        session: Any,
        tenant_id: str,
        inbox_id: str,
        order_data: dict[str, Any],
    ) -> str:
        clover_order_id: str = order_data["id"]
        total = _safe_int(order_data.get("total"))
        tax = _safe_int(order_data.get("taxAmount"))
        discount = _safe_int(order_data.get("discountAmount"))
        subtotal = total - tax - discount

        state = (order_data.get("state") or "open").lower()
        if state not in ("open", "locked"):
            state = "open"

        payment_state = self._derive_payment_state(order_data)

        created_ms = order_data.get("clientCreatedTime") or order_data.get("createdTime")
        modified_ms = order_data.get("modifiedTime")
        opened_at = _from_ms(_safe_int(created_ms) if created_ms else None)
        closed_at = (
            _from_ms(_safe_int(modified_ms) if modified_ms else None) if state == "locked" else None
        )

        device_id: str | None = (order_data.get("device") or {}).get("id")
        employee_id: str | None = (order_data.get("employee") or {}).get("id")
        ext_ref: str | None = order_data.get("externalReferenceId")

        result = await session.execute(
            text("""
                INSERT INTO orders (
                    id, tenant_id, pos_event_inbox_id, clover_order_id,
                    external_reference_id, channel_label, channel_category,
                    device_id, clover_employee_id,
                    subtotal_cents, discount_amount_cents, tax_amount_cents,
                    tip_amount_cents, total_amount_cents,
                    state, payment_state,
                    opened_at, closed_at, processed_at
                ) VALUES (
                    :id, :tid, :inbox_id, :clover_order_id,
                    :ext_ref, 'pos', 'unknown',
                    :device_id, :employee_id,
                    :subtotal, :discount, :tax,
                    0, :total,
                    :state, :payment_state,
                    :opened_at, :closed_at, now()
                )
                ON CONFLICT ON CONSTRAINT uq_orders_clover DO UPDATE SET
                    payment_state_previous = orders.payment_state,
                    payment_state = CASE
                        WHEN (
                            CASE orders.payment_state
                                WHEN 'REFUNDED'           THEN 5
                                WHEN 'PARTIALLY_REFUNDED' THEN 4
                                WHEN 'CREDITED'           THEN 3
                                WHEN 'PAID'               THEN 2
                                ELSE 1
                            END
                        ) > (
                            CASE EXCLUDED.payment_state
                                WHEN 'REFUNDED'           THEN 5
                                WHEN 'PARTIALLY_REFUNDED' THEN 4
                                WHEN 'CREDITED'           THEN 3
                                WHEN 'PAID'               THEN 2
                                ELSE 1
                            END
                        )
                        THEN orders.payment_state
                        ELSE EXCLUDED.payment_state
                    END,
                    total_amount_cents    = EXCLUDED.total_amount_cents,
                    discount_amount_cents = EXCLUDED.discount_amount_cents,
                    tax_amount_cents      = EXCLUDED.tax_amount_cents,
                    closed_at             = EXCLUDED.closed_at,
                    state                 = EXCLUDED.state,
                    processed_at          = now()
                RETURNING id
            """),
            {
                "id": str(uuid7()),
                "tid": tenant_id,
                "inbox_id": inbox_id,
                "clover_order_id": clover_order_id,
                "ext_ref": ext_ref,
                "device_id": device_id,
                "employee_id": employee_id,
                "subtotal": subtotal,
                "discount": discount,
                "tax": tax,
                "total": total,
                "state": state,
                "payment_state": payment_state,
                "opened_at": opened_at,
                "closed_at": closed_at,
            },
        )
        row = result.fetchone()
        return str(row.id)

    def _derive_payment_state(self, order_data: dict[str, Any]) -> str:
        """Map Clover order data to our payment_state enum.

        Priority:
          1. Explicit order-level paymentState field.
          2. Refund totals (refundTotal vs total).
          3. Payments array scan (SUCCESS + REFUNDED mix → PARTIALLY_REFUNDED).
          4. payType present → PAID.
          5. Default → OPEN.
        """
        # 1. Explicit paymentState (set by Clover or test helpers)
        ps: str | None = order_data.get("paymentState")
        if ps and ps in _PAYMENT_STATE_PRIORITY:
            return ps

        # 2. Refund totals
        total = _safe_int(order_data.get("total"))
        refund_total = _safe_int(order_data.get("refundTotal"))
        if refund_total > 0 and total > 0:
            return "REFUNDED" if refund_total >= total else "PARTIALLY_REFUNDED"

        # 3. Payments array
        payments = (order_data.get("payments") or {}).get("elements") or []
        results = {p.get("result") for p in payments if isinstance(p, dict) and p.get("result")}
        has_paid = bool(results & {"SUCCESS", "AUTH"})
        has_refund = bool(results & {"REFUNDED", "REFUND"})
        if has_paid and has_refund:
            return "PARTIALLY_REFUNDED"
        if has_refund:
            return "REFUNDED"
        if has_paid:
            return "PAID"

        # 4. payType field (payment method indicator)
        if order_data.get("payType"):
            return "PAID"

        return "OPEN"

    # ── Line item insert ──────────────────────────────────────────────────────

    async def _insert_line_item(
        self,
        session: Any,
        tenant_id: str,
        order_id: str,
        li: dict[str, Any],
    ) -> str:
        """Insert a sale_line_item and return its UUID string.

        ON CONFLICT DO NOTHING (idempotent on clover_line_item_id).  If the row
        already exists from a prior attempt, falls back to a SELECT to return
        the existing ID so inventory effects can still be triggered idempotently.
        """
        li_id: str = li["id"]  # caller guarantees non-empty

        name: str = li.get("name") or "Unknown"
        # unitQty is the raw quantity (1 = 1 unit, 2 = 2 units)
        quantity = _safe_float(li.get("unitQty")) or 1.0
        price = _safe_int(li.get("price"))
        discount = _safe_int(li.get("discountAmount"))
        net_revenue = int(price * quantity - discount)

        is_refunded = bool(li.get("refunded", False))
        is_voided = bool(li.get("exchanged", False))

        # Best-effort menu_item_id + recipe_version_id lookup.
        # recipe_version_id is snapshotted at insert time (Section 10 of the
        # accounting ADR) — changing menu_items.recipe_version_id later has no
        # effect on depletions that already have a snapshot.
        item_id: str | None = (li.get("item") or {}).get("id")
        menu_item_id: str | None = None
        recipe_version_id: str | None = None
        if item_id:
            mi_row = (
                await session.execute(
                    text("""
                        SELECT id, recipe_version_id FROM menu_items
                        WHERE tenant_id = :tid AND pos_item_id = :pid
                        LIMIT 1
                    """),
                    {"tid": tenant_id, "pid": item_id},
                )
            ).fetchone()
            if mi_row:
                menu_item_id = str(mi_row.id)
                if mi_row.recipe_version_id is not None:
                    recipe_version_id = str(mi_row.recipe_version_id)

        result = await session.execute(
            text("""
                INSERT INTO sale_line_items (
                    id, tenant_id, order_id, clover_line_item_id, menu_item_id,
                    name_at_sale, quantity, price_cents_at_sale,
                    discount_amount_cents, net_revenue_cents,
                    is_refunded, is_voided, recipe_version_id
                ) VALUES (
                    :id, :tid, :order_id, :li_id, :menu_item_id,
                    :name, :qty, :price,
                    :discount, :net,
                    :refunded, :voided, :rvid
                )
                ON CONFLICT ON CONSTRAINT uq_sli_clover DO NOTHING
                RETURNING id
            """),
            {
                "id": str(uuid7()),
                "tid": tenant_id,
                "order_id": order_id,
                "li_id": li_id,
                "menu_item_id": menu_item_id,
                "name": name,
                "qty": quantity,
                "price": price,
                "discount": discount,
                "net": net_revenue,
                "refunded": is_refunded,
                "voided": is_voided,
                "rvid": recipe_version_id,
            },
        )
        row = result.fetchone()
        if row:
            return str(row.id)

        # Conflict: row exists from a prior attempt — fetch existing id.
        existing = (
            await session.execute(
                text("""
                    SELECT id FROM sale_line_items
                    WHERE tenant_id = :tid AND clover_line_item_id = :li_id
                    LIMIT 1
                """),
                {"tid": tenant_id, "li_id": li_id},
            )
        ).fetchone()
        return str(existing.id)

    # ── State transitions ─────────────────────────────────────────────────────

    async def mark_processed(self, event: Any) -> None:
        sm = get_service_sessionmaker()
        async with sm() as session:
            await session.execute(
                text("""
                    UPDATE pos_event_inbox
                    SET state            = 'processed',
                        processed_at     = :now,
                        claim_expires_at = NULL
                    WHERE inbox_id = :id
                """),
                {"now": _now(), "id": str(event["inbox_id"])},
            )
            await session.commit()

    async def mark_failed(self, event: Any, error: str) -> None:
        retry_count: int = _safe_int(event.get("retry_count")) + 1
        if retry_count >= self.MAX_RETRIES:
            await self._dead_letter(event, error, retry_count)
            return

        # Exponential backoff: 2^(n-1) minutes (1 → 2 → 4 → 8 min)
        backoff_minutes = 2 ** (retry_count - 1)
        next_attempt = _now() + timedelta(minutes=backoff_minutes)

        sm = get_service_sessionmaker()
        async with sm() as session:
            await session.execute(
                text("""
                    UPDATE pos_event_inbox
                    SET state           = 'failed',
                        retry_count     = :count,
                        last_error      = :error,
                        next_attempt_at = :next
                    WHERE inbox_id = :id
                """),
                {
                    "count": retry_count,
                    "error": error[:2000],
                    "next": next_attempt,
                    "id": str(event["inbox_id"]),
                },
            )
            await session.commit()

        log.warning(
            "worker.event_failed",
            inbox_id=str(event["inbox_id"]),
            retry_count=retry_count,
            next_attempt_minutes=backoff_minutes,
            error=error[:200],
        )

    async def _dead_letter(self, event: Any, error: str, retry_count: int) -> None:
        tenant_id = str(event["tenant_id"])
        sm = get_service_sessionmaker()
        async with sm() as session:
            await session.execute(
                text("""
                    UPDATE pos_event_inbox
                    SET state       = 'dead_letter',
                        retry_count = :count,
                        last_error  = :error
                    WHERE inbox_id = :id
                """),
                {
                    "count": retry_count,
                    "error": error[:2000],
                    "id": str(event["inbox_id"]),
                },
            )
            # monitoring_alerts ON CONFLICT requires the conflicting row to be
            # visible via SELECT policy. Set app.tenant_id so the tenant-scoped
            # SELECT policy resolves the conflict row correctly.
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": tenant_id},
            )
            await session.execute(
                text("""
                    INSERT INTO monitoring_alerts
                        (tenant_id, monitor_name, severity, trigger_payload)
                    VALUES (
                        :tid,
                        'pos_event_dead_letter',
                        'critical',
                        CAST(:payload AS jsonb)
                    )
                    ON CONFLICT (tenant_id, monitor_name) WHERE resolved_at IS NULL
                    DO UPDATE SET
                        last_seen_at    = now(),
                        alert_count     = monitoring_alerts.alert_count + 1,
                        trigger_payload = EXCLUDED.trigger_payload
                """),
                {
                    "tid": tenant_id,
                    "payload": json.dumps(
                        {
                            "inbox_id": str(event["inbox_id"]),
                            "vendor_event_id": str(event.get("vendor_event_id") or ""),
                            "error": error[:500],
                        }
                    ),
                },
            )
            await session.commit()

        log.error(
            "worker.dead_letter",
            inbox_id=str(event["inbox_id"]),
            tenant_id=str(event["tenant_id"]),
            error=error[:200],
        )
