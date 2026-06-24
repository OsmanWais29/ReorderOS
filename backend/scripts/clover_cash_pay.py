"""In-container: apply a CASH tender to an order via Clover REST so it reaches
PAID/LOCKED — no card gateway, no Virtual Terminal.

Run in the DO console (api component), from /srv:
    python -m scripts.clover_cash_pay
    # optionally choose which open order to pay:
    CLOVER_ORDER_ID=ETNF17ORKE676 python -m scripts.clover_cash_pay

Reuses an EXISTING open order (default below) that already has the burger line item,
so we avoid needing ORDERS write to *create* an order/line item. It then creates a
CASH payment, which fully tenders the order -> Clover locks it.

PERMISSIONS (read this first):
  * Creating a payment needs **PAYMENTS write**.
  * Mutating/locking the order may need **ORDERS write**.
  You currently have READ-only on both. If a write call 401/403s, the script says so.
  Fix: add 'Payments: Write' (and 'Orders: Write') to app DJFFAT14DS7QM Requested
  Permissions -> Save -> UNINSTALL+REINSTALL the app on the merchant (permission
  changes only take effect on reinstall) -> re-run. The new token upserts into the
  same connection row, so this script picks it up automatically.

Why a payment at all (not just lock): resolver.py requires payment_state == 'PAID';
a locked-but-unpaid order is sale_ineligible and depletes nothing.
"""

from __future__ import annotations

import asyncio
import os

import httpx
from sqlalchemy import text

from app.core.encryption import TokenEncryption
from app.core.service_db import get_service_sessionmaker

TENANT_ID = "aaa772e8-c714-4f74-945e-85fc13399f1d"
ORDER_ID = os.environ.get("CLOVER_ORDER_ID", "ARYAYZ4P5661J")

_ENV_API_BASES = {
    "sandbox": "https://apisandbox.dev.clover.com",
    "production": "https://api.clover.com",
}


def _scope_hint(status: int) -> str:
    if status in (401, 403):
        return ("  -> 401/403: the token lacks WRITE for this call. Add Payments:Write "
                "(and Orders:Write), re-authorize the merchant (uninstall+reinstall), re-run.")
    return ""


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
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    with httpx.Client(timeout=20) as c:
        # 1) read the order (total + current state)
        r = c.get(
            f"{base}/v3/merchants/{mid}/orders/{ORDER_ID}?expand=lineItems,payments",
            headers=headers,
        )
        print("GET order:", r.status_code)
        if r.status_code != 200:
            print(r.text[:600], _scope_hint(r.status_code))
            return
        order = r.json()
        total = int(order.get("total") or 0) or 1200
        print(f"  order {ORDER_ID}: state={order.get('state')} "
              f"paymentState={order.get('paymentState')} total={total}")

        # 2) find the cash tender id (read)
        r = c.get(f"{base}/v3/merchants/{mid}/tenders", headers=headers)
        print("GET tenders:", r.status_code)
        if r.status_code != 200:
            print(r.text[:600], _scope_hint(r.status_code))
            return
        tenders = r.json().get("elements") or []
        cash = next((t for t in tenders if t.get("labelKey") == "com.clover.tender.cash"), None)
        if cash is None:
            print("  no cash tender found. tenders:", [t.get("labelKey") for t in tenders])
            return
        print("  cash tender id:", cash["id"])

        # 3) create the cash payment (WRITE) -> fully tenders -> Clover locks the order
        body = {
            "order": {"id": ORDER_ID},
            "tender": {"id": cash["id"]},
            "amount": total,
            "offline": False,
            "cashTendered": total,
        }
        r = c.post(
            f"{base}/v3/merchants/{mid}/orders/{ORDER_ID}/payments",
            headers=headers,
            json=body,
        )
        print("POST payment:", r.status_code)
        print("  ", r.text[:600])
        if r.status_code not in (200, 201):
            print(_scope_hint(r.status_code))
            print("  (if 400, the cash payment body shape may need a tweak — paste this output.)")
            return

        # 4) re-read and report
        r = c.get(f"{base}/v3/merchants/{mid}/orders/{ORDER_ID}?expand=payments", headers=headers)
        o = r.json()
        print(f"\nFINAL order {ORDER_ID}: state={o.get('state')} paymentState={o.get('paymentState')}")
        if o.get("state") == "locked":
            print("LOCKED clover_order_id:", ORDER_ID)
            print("-> the order webhook should now fire; trace pos_event_inbox -> "
                  "sale_line_items -> inventory_movements by this id.")
        else:
            print("NOT locked yet — inspect the payment response above.")


if __name__ == "__main__":
    asyncio.run(main())
