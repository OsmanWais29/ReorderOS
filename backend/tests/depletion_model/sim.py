"""V5b — restaurant-day simulation harness: drive the REAL worker with concurrent terminals.

Unlike V2/V3/V5a (which call process_line directly), this seeds Clover-format events into
pos_event_inbox and runs N concurrent InboxWorker loops that claim_batch → ingest → deplete —
exercising claim_batch (the F4 MATERIALIZED path under real contention), the T1-ingest/T2-deplete
boundary, _snapshot_modifiers, and _insert_line_item replay. Counts are injected at BARRIERS
between order waves (the inbox is drained first) so each count's re-anchor sees a well-defined set
of prior sales and the oracle stays deterministic. Final per-ingredient ledger + on-hand are
compared to oracle.expected_timeline.

Uses real commits (the worker commits on its own service-engine connections); the autoclean
fixture wipes the fresh tenant afterward (every tenant_id table), so no custom teardown.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.modules.inventory.services import record_count_event
from app.modules.pos.worker import InboxWorker
from tests.depletion_model.seed import SALE_AT, Seeded
from tests.depletion_model.world import Count, Order

_BASE_MS = int(SALE_AT.timestamp() * 1000)
_HOUR_MS = 3_600_000


def _wave_created_ms(wave: int) -> int:
    """createdTime for orders in a wave — 1h apart so waves are strictly time-ordered and the
    historical on_hand boundary (recorded_at > last_count_at) partitions waves cleanly."""
    return _BASE_MS + wave * _HOUR_MS


def _count_at(wave: int) -> Any:
    """counted_at for a barrier count AFTER `wave` — 30 min after the wave, before the next."""
    return SALE_AT + timedelta(hours=wave, minutes=30)


def build_clover_order(seeded: Seeded, order: Order, created_ms: int) -> dict[str, Any]:
    """Translate a model Order into a Clover order payload the worker can ingest."""
    line_items: list[dict[str, Any]] = []
    for line in order.lines:
        li: dict[str, Any] = {
            "id": f"li_{uuid.uuid4().hex}",
            "item": {"id": seeded.pos_item_ids[line.menu_item]},
            "name": line.menu_item,
            "unitQty": float(line.qty),
            "price": 100,
        }
        if line.is_refunded:
            li["refunded"] = True
        if line.is_voided:
            li["exchanged"] = True
        if line.modifiers:
            li["modifications"] = {
                "elements": [
                    {"modifier": {"id": seeded.pos_modifier_ids[mk]}, "quantity": float(q)}
                    for mk, q in line.modifiers
                ]
            }
        line_items.append(li)
    return {
        "id": f"order_{uuid.uuid4().hex}",
        "state": order.order_state,
        "paymentState": order.payment_state,
        "total": 0,
        "createdTime": created_ms,
        "clientCreatedTime": created_ms,
        "lineItems": {"elements": line_items},
    }


async def seed_inbox_payload(
    session: Any, seeded: Seeded, payload: dict[str, Any], *, vendor_event_type: str = "UPDATE"
) -> None:
    """Insert ONE pending inbox row for an already-built Clover payload. The inbox dedups on
    (tenant, vendor, vendor_event_id, vendor_event_type, vendor_ts), so re-delivery of the SAME
    order is modelled by varying vendor_event_type (e.g. CREATE then UPDATE) with one payload —
    both reach the worker; uq_sli_clover then dedups the lines (depletion-level idempotency)."""
    await session.execute(
        text("""
            INSERT INTO pos_event_inbox
                (inbox_id, tenant_id, vendor, vendor_event_id, vendor_object_type,
                 vendor_event_type, vendor_ts, raw_payload, fetched_payload, fetched_at,
                 signature_verified, source, state)
            VALUES (:iid, :t, 'clover', :vev, 'O', :vet, :vts, '{}',
                    CAST(:fp AS jsonb), now(), true, 'webhook', 'pending')
        """),
        {
            "iid": str(uuid.uuid4()),
            "t": seeded.tenant_id,
            "vev": payload["id"],
            "vet": vendor_event_type,
            "vts": int(payload.get("createdTime") or _BASE_MS),
            "fp": json.dumps(payload),
        },
    )


async def seed_inbox_wave(
    session: Any, seeded: Seeded, orders: list[Order], created_ms: int
) -> None:
    """Insert one pending pos_event_inbox row per order, with fetched_payload set (so the worker
    skips the Clover API and ingests directly). Committed by the caller."""
    for order in orders:
        await seed_inbox_payload(session, seeded, build_clover_order(seeded, order, created_ms))


async def _pending_count(session: Any, tenant_id: str) -> int:
    return int(
        (
            await session.execute(
                text("""
                    SELECT count(*) FROM pos_event_inbox
                    WHERE tenant_id = :t AND vendor_object_type = 'O'
                      AND (state = 'pending'
                           OR (state = 'processing' AND claim_expires_at < now()))
                """),
                {"t": tenant_id},
            )
        ).scalar()
    )


async def drain_concurrent(
    session: Any, tenant_id: str, *, n_workers: int = 3, batch_size: int = 10
) -> None:
    """Run N concurrent InboxWorker loops until the tenant's inbox is fully drained. This is the
    real contention on claim_batch (3 terminals in a rush = the realistic F4 conditions). Loops
    until no pending / claim-expired events remain (a worker that breaks on a momentary empty
    can't strand work)."""

    async def worker_loop() -> None:
        w = InboxWorker()
        while True:
            events = await w.claim_batch(batch_size=batch_size)
            if not events:
                return
            for event in events:
                try:
                    await w.process_event(event)
                except Exception:  # isolation mirrors run(); a stuck row fails the drain retry
                    pass

    for _ in range(50):  # bounded retries; each pass spawns n_workers
        await asyncio.gather(*[worker_loop() for _ in range(n_workers)])
        if await _pending_count(session, tenant_id) == 0:
            return
    raise RuntimeError("inbox did not drain (stuck events)")


async def run_count(session: Any, seeded: Seeded, count: Count, wave: int) -> None:
    """Inject one operator count at a barrier (after `wave`)."""
    from decimal import Decimal

    await record_count_event(
        session,
        tenant_id=UUID(seeded.tenant_id),
        inventory_item_id=UUID(seeded.item_ids[count.item]),
        counted_quantity=Decimal(str(count.counted)),
        counted_at=_count_at(wave),
    )
