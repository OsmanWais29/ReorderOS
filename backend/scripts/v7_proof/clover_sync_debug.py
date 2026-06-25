"""In-container diagnostic: why is catalog sync not landing the burger?

Run in the DigitalOcean App Platform Console (api component), from /srv:
    python -m scripts.clover_sync_debug

It uses the app's own token decryption + Clover client to:
  1. list all connection rows for the tenant (merchant_id + state),
  2. call list_inventory_items with the ACTIVE connection's token and print the
     count or the EXACT error (a 401 here = the token lacks INVENTORY_R read),
  3. run the real sync_connection and print the resulting menu_items row count.

This pinpoints the cause that catalog_sync's try/except otherwise swallows.
No secret handling — TOKEN_ENCRYPTION_KEY is read from the container env.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.core.encryption import TokenEncryption
from app.core.service_db import get_service_sessionmaker
from app.modules.pos.catalog_sync import CatalogSyncService
from app.modules.pos.clover_client import CloverClient

TENANT_ID = "aaa772e8-c714-4f74-945e-85fc13399f1d"


async def main() -> None:
    sm = get_service_sessionmaker()
    async with sm() as s:
        conns = (
            await s.execute(
                text(
                    "SELECT connection_id, merchant_id, environment, access_token_enc, state,"
                    " created_at FROM tenant_pos_connections"
                    " WHERE tenant_id = :t AND vendor = 'clover' ORDER BY created_at DESC"
                ),
                {"t": TENANT_ID},
            )
        ).mappings().all()

    print("connections (newest first):")
    for c in conns:
        print(f"  merchant={c['merchant_id']} state={c['state']} created={c['created_at']}")

    active = [c for c in conns if c["state"] == "active"]
    if not active:
        print("NO ACTIVE CONNECTION — nothing to sync.")
        return
    c = active[0]
    print(f"\nusing ACTIVE merchant={c['merchant_id']} conn={c['connection_id']}")

    token = TokenEncryption().decrypt(c["access_token_enc"])
    client = CloverClient(
        access_token=token, merchant_id=c["merchant_id"], environment=c["environment"]
    )

    # Step 2: the read the sync depends on.
    try:
        items = await client.list_inventory_items(offset=0, limit=100)
        print(f"list_inventory_items OK — count={len(items)}")
        for it in items[:15]:
            print(f"  - {it.get('id')}  {it.get('name')!r}")
        if not items:
            print("  (empty — the item isn't visible to the API for this merchant/token)")
    except Exception as exc:  # noqa: BLE001 - diagnostic
        print(f"list_inventory_items ERROR: {type(exc).__name__}: {str(exc)[:300]}")
        print("  -> a 401 here means the token lacks INVENTORY_R read scope.")

    # Step 3: run the real sync and count what landed.
    await CatalogSyncService().sync_connection(str(c["connection_id"]))
    async with sm() as s:
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_ID})
        n = (
            await s.execute(
                text("SELECT count(*) FROM menu_items WHERE tenant_id = :t"), {"t": TENANT_ID}
            )
        ).scalar()
    print(f"\nmenu_items rows for tenant after sync: {n}")


if __name__ == "__main__":
    asyncio.run(main())
