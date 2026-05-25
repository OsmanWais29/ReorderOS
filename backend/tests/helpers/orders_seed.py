"""Seed fixture for Phase 3+ tests.

Extends Phase 2's inbox prerequisites to also create an inbox event,
providing the FK target for orders.pos_event_inbox_id.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from tests.helpers.inbox_seed import seed_inbox_prereqs
from tests.helpers.pos_inbox import insert_inbox, make_inbox_row


async def seed_order_prereqs(admin_conn: Any, service_conn: Any) -> dict:
    """Create tenant + connection + inbox event.

    Returns dict with tenant_id, connection_id, inbox_id.
    Inbox insert uses service_conn (USING(true) on pos_event_inbox — no
    SET LOCAL required). Connection insert uses admin_conn (superuser).
    """
    seed = await seed_inbox_prereqs(admin_conn)

    inbox_row = make_inbox_row(
        {
            "tenant_id": seed["tenant_id"],
            "connection_id": seed["connection_id"],
            "vendor_event_id": f"seed_order_{uuid.uuid4().hex[:8]}",
            "vendor_event_type": "UPDATE",
            "vendor_ts": int(time.time() * 1000),
            "raw_payload": json.dumps({"source": "seed"}),
        }
    )
    await insert_inbox(service_conn, inbox_row)

    return {
        "tenant_id": seed["tenant_id"],
        "connection_id": seed["connection_id"],
        "inbox_id": inbox_row["inbox_id"],
    }
