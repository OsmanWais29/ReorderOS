"""One-off: create ONE sellable Clover item using THIS app's own stored OAuth token.

Why this exists: the Clover self-service "API tokens" page is US-only, and the
inventory UI threw "you do not have access to this app" for the Canadian sandbox
merchant. But our backend already holds a working, active OAuth access token for
that merchant (stored encrypted at connect-time in tenant_pos_connections). This
script decrypts it and POSTs one item to Clover's v3 inventory API directly — no
geo-blocked page, no dashboard.

Connection + SSL handling are copied verbatim from tests/staging_sim.py so it
behaves identically against the DO managed Postgres.

RUN (locally, pointed at STAGING — same way you run staging_sim.py):
    export STAGING_DB_URL='postgresql://doadmin:<pw>@<host>:25060/defaultdb?sslmode=require'
    export TOKEN_ENCRYPTION_KEY='<from DO console: staging app → Settings → env vars>'
    # ONLY if the key was rotated AFTER this merchant connected (token predates rotation):
    export TOKEN_ENCRYPTION_KEY_PREVIOUS='<previous key>'
    cd backend && source .venv/bin/activate
    python tools/clover_create_item.py

OUTCOMES:
  HTTP 200 + JSON with an "id"  -> SUCCESS. That id becomes menu_items.pos_item_id
                                   after catalog sync (step 2).
  HTTP 401 / 403                -> the app's token lacks inventory WRITE (catalog
                                   sync only ever needed read). Fix: add
                                   "Inventory: Read & Write" to app DJFFAT14DS7QM
                                   under Requested Permissions (app config — NOT
                                   geo-restricted), RE-AUTHORIZE the merchant
                                   (re-run the connect flow; the new token upserts
                                   into the same row), then re-run this script.
  "Token decryption failed"     -> wrong TOKEN_ENCRYPTION_KEY. Confirm you copied
                                   the CURRENT staging key; set _PREVIOUS if rotated.
"""

from __future__ import annotations

import asyncio
import os
import ssl
import sys

import asyncpg
import httpx
from cryptography.fernet import Fernet, MultiFernet

TENANT_ID = "aaa772e8-c714-4f74-945e-85fc13399f1d"
ITEM_NAME = "Bluebird Café Classic Burger"
ITEM_PRICE_CENTS = 1200  # $12.00

_ENV_API_BASES = {
    "sandbox": "https://apisandbox.dev.clover.com",
    "production": "https://api.clover.com",
}


def _dsn() -> str:
    raw = os.environ.get("STAGING_DB_URL")
    if not raw:
        sys.exit("ERROR: set STAGING_DB_URL to the staging doadmin connection string")
    return raw.replace("postgresql+asyncpg://", "postgresql://").split("?")[0]


async def _connect() -> asyncpg.Connection:
    dsn = _dsn()
    if "localhost" in dsn or "127.0.0.1" in dsn:
        return await asyncpg.connect(dsn)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return await asyncpg.connect(dsn, ssl=ctx)


def _decrypter() -> MultiFernet:
    key = os.environ.get("TOKEN_ENCRYPTION_KEY")
    if not key:
        sys.exit("ERROR: set TOKEN_ENCRYPTION_KEY (staging app env var, DO console)")
    fernets = [Fernet(key.encode())]
    prev = os.environ.get("TOKEN_ENCRYPTION_KEY_PREVIOUS")
    if prev:
        fernets.append(Fernet(prev.encode()))
    return MultiFernet(fernets)


async def main() -> None:
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            """
            SELECT access_token_enc, merchant_id, environment
            FROM tenant_pos_connections
            WHERE tenant_id = $1 AND vendor = 'clover'
              AND state IN ('active', 'error')
            LIMIT 1
            """,
            TENANT_ID,
        )
    finally:
        await conn.close()

    if row is None:
        sys.exit("No active Clover connection for that tenant (state not active/error).")

    token = _decrypter().decrypt(row["access_token_enc"].encode()).decode()
    base = _ENV_API_BASES.get(row["environment"], _ENV_API_BASES["sandbox"])
    url = f"{base}/v3/merchants/{row['merchant_id']}/items"

    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"name": ITEM_NAME, "price": ITEM_PRICE_CENTS},
        timeout=15,
    )

    print(f"merchant={row['merchant_id']} env={row['environment']}")
    print(f"HTTP {resp.status_code}")
    print(resp.text)
    if resp.status_code == 200:
        print("\nSUCCESS — item created. Copy the 'id' above; it becomes "
              "menu_items.pos_item_id after catalog sync.")
    elif resp.status_code in (401, 403):
        print("\nTOKEN LACKS INVENTORY WRITE — add 'Inventory: Read & Write' to app "
              "DJFFAT14DS7QM, re-authorize the merchant, then re-run this script.")


if __name__ == "__main__":
    asyncio.run(main())
