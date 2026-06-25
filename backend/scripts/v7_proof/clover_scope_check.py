"""Verify the stored token actually has Orders:Write and Payments:Write — WITHOUT
committing a sale. Run BEFORE clover_cash_pay so you don't burn an attempt.

Run in the DO console (api component), from /srv:
    python -m scripts.clover_scope_check

Why a probe: Clover OAuth tokens are opaque (no readable scope claim), and the DB's
configured_permissions is a hardcoded literal — neither reflects the real grant. So a
write-probe is the only definitive check.

What it does (commits nothing):
  1. POST an EMPTY order            -> Orders:Write present iff 200/201
  2. POST a malformed payment {}    -> Payments:Write present iff NOT 401/403
     (a 400/422 means auth passed but the body is invalid = scope IS present;
      an empty body never creates a real payment, so nothing locks)
  3. DELETE the probe order         -> cleanup, no junk left behind
"""

from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import text

from app.core.encryption import TokenEncryption
from app.core.service_db import get_service_sessionmaker

TENANT_ID = "aaa772e8-c714-4f74-945e-85fc13399f1d"
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
                    "SELECT merchant_id, environment, access_token_enc"
                    " FROM tenant_pos_connections"
                    " WHERE tenant_id = :t AND vendor = 'clover'"
                    "   AND state IN ('active', 'error') LIMIT 1"
                ),
                {"t": TENANT_ID},
            )
        ).mappings().fetchone()
    if row is None:
        print("NO ACTIVE CLOVER CONNECTION")
        return

    token = TokenEncryption().decrypt(row["access_token_enc"])
    base = _ENV_API_BASES.get(row["environment"], _ENV_API_BASES["sandbox"])
    mid = row["merchant_id"]
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    print(f"=== scope check for merchant {mid} (nothing is committed) ===")
    with httpx.Client(timeout=20) as c:
        # 1) Orders:Write — create an empty order
        r = c.post(f"{base}/v3/merchants/{mid}/orders", headers=H, json={"state": "open"})
        orders_w = r.status_code in (200, 201)
        print(f"Orders:Write   -> {'OK' if orders_w else 'MISSING'}  (POST /orders {r.status_code})")
        oid = r.json().get("id") if orders_w else None

        # 2) Payments:Write — malformed payment; 401/403 = missing scope, else auth passed
        pay_w = None
        if oid:
            rp = c.post(f"{base}/v3/merchants/{mid}/orders/{oid}/payments", headers=H, json={})
            pay_w = rp.status_code not in (401, 403)
            print(f"Payments:Write -> {'OK' if pay_w else 'MISSING'}  (POST payment {rp.status_code})")
            rd = c.delete(f"{base}/v3/merchants/{mid}/orders/{oid}", headers=H)
            print(f"cleanup        -> DELETE probe order {oid} ({rd.status_code})")
        else:
            print("Payments:Write -> SKIPPED (couldn't create a probe order)")

    print()
    if orders_w and pay_w:
        print("BOTH WRITE SCOPES PRESENT — safe to run: python -m scripts.clover_cash_pay")
    else:
        print("MISSING SCOPE(S) — the uninstall+reinstall did NOT propagate the new scopes.")
        print("Fully uninstall the app from the merchant, reinstall via connect-url, re-run this.")


if __name__ == "__main__":
    asyncio.run(main())
