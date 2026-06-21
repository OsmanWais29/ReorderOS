"""Create ONE sellable Clover item from INSIDE the running staging container.

Run in the DigitalOcean App Platform Console (api or inbox-worker component):
    python -m tools.clover_create_item_incontainer
    # fallback if 'app' import fails: PYTHONPATH=. python tools/clover_create_item_incontainer.py

No heredoc, no quoting, no secret handling. It reuses the app's own
TokenEncryption (which reads TOKEN_ENCRYPTION_KEY already present in the
container, and auto-falls back to the rotated previous key) and
get_service_sessionmaker (the app's own DB connection) — so the raw encryption
key never leaves the container and you never paste it anywhere.

Outcomes:
  HTTP 200 + an "id"  -> success; that id becomes menu_items.pos_item_id after sync.
  HTTP 401 / 403      -> the app's token lacks inventory WRITE. Add
                         "Inventory: Read & Write" to app DJFFAT14DS7QM under
                         Requested Permissions, re-authorize the merchant, then
                         re-run this (no redeploy needed for the re-run).
"""

from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import text

from app.core.encryption import TokenEncryption
from app.core.service_db import get_service_sessionmaker

TENANT_ID = "aaa772e8-c714-4f74-945e-85fc13399f1d"
ITEM_NAME = "Bluebird Café Classic Burger"
ITEM_PRICE_CENTS = 1200  # $12.00

_ENV_API_BASES = {
    "sandbox": "https://apisandbox.dev.clover.com",
    "production": "https://api.clover.com",
}


async def main() -> None:
    sm = get_service_sessionmaker()
    async with sm() as s:
        row = (
            await s.execute(
                text(
                    "SELECT access_token_enc, merchant_id, environment "
                    "FROM tenant_pos_connections "
                    "WHERE tenant_id = :t AND vendor = 'clover' "
                    "AND state IN ('active', 'error') LIMIT 1"
                ),
                {"t": TENANT_ID},
            )
        ).fetchone()

    if row is None:
        print("NO ACTIVE CLOVER CONNECTION for tenant", TENANT_ID)
        return

    token = TokenEncryption().decrypt(row.access_token_enc)
    base = _ENV_API_BASES.get(row.environment, _ENV_API_BASES["sandbox"])
    url = f"{base}/v3/merchants/{row.merchant_id}/items"

    r = httpx.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"name": ITEM_NAME, "price": ITEM_PRICE_CENTS},
        timeout=15,
    )

    print(f"merchant={row.merchant_id} env={row.environment}")
    print("HTTP", r.status_code)
    print(r.text)
    if r.status_code in (401, 403):
        print(
            "\nTOKEN LACKS INVENTORY WRITE — add 'Inventory: Read & Write' to app "
            "DJFFAT14DS7QM, re-authorize the merchant, then re-run this script."
        )


if __name__ == "__main__":
    asyncio.run(main())
