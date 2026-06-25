"""In-container: create a burger order and CASH-tender it to LOCKED via Clover REST —
no card gateway, no Virtual Terminal.

Run in the DO console (api component), from /srv:
    python -m scripts.clover_cash_pay                 # self-creates a fresh order
    CLOVER_ORDER_ID=ETNF17ORKE676 python -m scripts.clover_cash_pay   # reuse an Open order

DEFAULT (no CLOVER_ORDER_ID): creates a fresh order, adds the burger line item
(Clover item QG3FB16ZSQ25G), then applies a cash tender so the order locks — you
never touch the web terminal. Pass CLOVER_ORDER_ID to instead pay an existing Open
order.

NEEDS: Orders:Write (create order + line item) and Payments:Write (cash payment).
The script names the missing scope on a 401/403.

Why a payment, not just a lock: resolver.py requires payment_state == 'PAID';
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
ITEM_ID = os.environ.get("CLOVER_ITEM_ID", "QG3FB16ZSQ25G")  # Bluebird Café Classic Burger
ORDER_ID = os.environ.get("CLOVER_ORDER_ID")  # None => self-create a fresh order

_ENV_API_BASES = {
    "sandbox": "https://apisandbox.dev.clover.com",
    "production": "https://api.clover.com",
}


def _hint(status: int) -> str:
    if status in (401, 403):
        return ("  -> 401/403: token lacks WRITE for this call. Confirm Orders:Write + "
                "Payments:Write are saved, re-authorize (uninstall+reinstall), re-run.")
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
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    with httpx.Client(timeout=20) as c:
        # ── 0) obtain an order id: reuse or self-create ───────────────────────
        if ORDER_ID:
            order_id = ORDER_ID
            print(f"reusing order {order_id}")
        else:
            r = c.post(f"{base}/v3/merchants/{mid}/orders", headers=H, json={"state": "open"})
            print("POST create order:", r.status_code)
            if r.status_code not in (200, 201):
                print(r.text[:600], _hint(r.status_code))
                return
            order_id = r.json()["id"]
            print("  created order:", order_id)

            r = c.post(
                f"{base}/v3/merchants/{mid}/orders/{order_id}/line_items",
                headers=H,
                json={"item": {"id": ITEM_ID}},
            )
            print("POST line_item:", r.status_code)
            if r.status_code not in (200, 201):
                print(r.text[:600], _hint(r.status_code))
                return
            print("  added line item:", r.json().get("id"))

        # ── 1) read order total + state ───────────────────────────────────────
        r = c.get(
            f"{base}/v3/merchants/{mid}/orders/{order_id}?expand=lineItems,payments", headers=H
        )
        if r.status_code != 200:
            print("GET order:", r.status_code, r.text[:400], _hint(r.status_code))
            return
        order = r.json()
        total = int(order.get("total") or 0) or 1200
        print(f"  order {order_id}: state={order.get('state')} "
              f"paymentState={order.get('paymentState')} total={total}")

        # ── 2) cash tender id ─────────────────────────────────────────────────
        r = c.get(f"{base}/v3/merchants/{mid}/tenders", headers=H)
        if r.status_code != 200:
            print("GET tenders:", r.status_code, r.text[:400], _hint(r.status_code))
            return
        tenders = r.json().get("elements") or []
        cash = next((t for t in tenders if t.get("labelKey") == "com.clover.tender.cash"), None)
        if cash is None:
            print("  no cash tender. tenders:", [t.get("labelKey") for t in tenders])
            return
        print("  cash tender id:", cash["id"])

        # ── 3) create cash payment (fully tenders -> locks) ───────────────────
        body = {
            "order": {"id": order_id},
            "tender": {"id": cash["id"]},
            "amount": total,
            "offline": False,
            "cashTendered": total,
        }
        r = c.post(f"{base}/v3/merchants/{mid}/orders/{order_id}/payments", headers=H, json=body)
        print("POST payment:", r.status_code)
        print("  ", r.text[:600])
        if r.status_code not in (200, 201):
            print(_hint(r.status_code))
            print("  (if 400, the cash body may need a tweak — paste this output.)")
            return

        # ── 4) report ─────────────────────────────────────────────────────────
        r = c.get(f"{base}/v3/merchants/{mid}/orders/{order_id}?expand=payments", headers=H)
        o = r.json()
        print(f"\nFINAL order {order_id}: state={o.get('state')} paymentState={o.get('paymentState')}")
        if o.get("state") == "locked":
            print("LOCKED clover_order_id:", order_id)
            print("-> webhook should fire now; trace pos_event_inbox -> sale_line_items"
                  " -> inventory_movements by this id.")
        else:
            print("NOT locked — inspect the payment response above.")


if __name__ == "__main__":
    asyncio.run(main())
