"""One-off: print the raw fetched_payload JSON for one order, to capture as a fixture.

Run in the DO console (api component), from /srv:
    python -m scripts.dump_payload
    VENDOR_EVENT_ID=3W9V62MQ043B2 python -m scripts.dump_payload

Prints the payload as a single compact JSON line between <<<PAYLOAD / PAYLOAD>>>
markers (so it's easy to copy past any log noise the console emits).
"""

from __future__ import annotations

import asyncio
import json
import os

from sqlalchemy import text

from app.core.service_db import get_service_sessionmaker

TENANT_ID = "aaa772e8-c714-4f74-945e-85fc13399f1d"
VENDOR_EVENT_ID = os.environ.get("VENDOR_EVENT_ID", "3W9V62MQ043B2")


async def main() -> None:
    sm = get_service_sessionmaker()
    async with sm() as s:
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_ID})
        fp = (
            await s.execute(
                text(
                    "SELECT fetched_payload FROM pos_event_inbox"
                    " WHERE tenant_id = :t AND vendor_event_id = :o"
                    "   AND fetched_payload IS NOT NULL"
                    " ORDER BY received_at DESC LIMIT 1"
                ),
                {"t": TENANT_ID, "o": VENDOR_EVENT_ID},
            )
        ).scalar()

    if fp is None:
        print(f"NO fetched_payload found for vendor_event_id={VENDOR_EVENT_ID}")
        return

    obj = fp if isinstance(fp, dict) else json.loads(fp)
    print("<<<PAYLOAD")
    print(json.dumps(obj))
    print("PAYLOAD>>>")


if __name__ == "__main__":
    asyncio.run(main())
