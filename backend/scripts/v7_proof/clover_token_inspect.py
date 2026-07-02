"""Inspect ALL stored Clover connection rows for the tenant — to pinpoint whether a
reinstall actually refreshed the token, and whether the probe scripts are using the
RIGHT row/merchant.

Run in the DO console (api component), from /srv:
    python -m scripts.clover_token_inspect

Prints, per row: merchant_id, state, created_at, updated_at, token_expires,
token fingerprint (first6…last6 + length), and configured_permissions.

NOTE: configured_permissions is a HARDCODED literal in the OAuth callback — it does
NOT reflect the token's real scopes. Use it only to confirm the column, not the grant.
The real scope test is scripts.clover_scope_check (a write probe).

How to read it:
  * Compare the ACTIVE row's token fingerprint + updated_at BEFORE vs AFTER a reinstall.
      - changed  -> reinstall issued a NEW token. If scope_check still 401s, the app's
                    Requested Permissions don't actually carry Orders/Payments WRITE
                    (boxes didn't save, or the change didn't take) — fix on the app side.
      - unchanged-> the reinstall did NOT complete a code exchange (app-launch / no code)
                    OR a stale row is being picked — see the "candidate rows" note.
  * The ACTIVE row's merchant MUST be 523DA3C5ADPG1. If it's a different/older merchant,
    the probe is hitting the wrong install.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.core.encryption import TokenEncryption
from app.core.service_db import get_service_sessionmaker

TENANT_ID = "aaa772e8-c714-4f74-945e-85fc13399f1d"
EXPECTED_MERCHANT = "523DA3C5ADPG1"


async def main() -> None:
    sm = get_service_sessionmaker()
    async with sm() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT connection_id, merchant_id, state, configured_permissions,"
                    " created_at, updated_at, access_token_expires_at, access_token_enc"
                    " FROM tenant_pos_connections"
                    " WHERE tenant_id = :t AND vendor = 'clover'"
                    " ORDER BY created_at"
                ),
                {"t": TENANT_ID},
            )
        ).mappings().all()

    if not rows:
        print("no clover connection rows for this tenant.")
        return

    enc = TokenEncryption()
    print(f"{len(rows)} clover connection row(s) for tenant {TENANT_ID}:\n")
    for r in rows:
        try:
            tok = enc.decrypt(r["access_token_enc"])
            fp = f"{tok[:6]}...{tok[-6:]} (len {len(tok)})"
        except Exception as exc:  # noqa: BLE001 - diagnostic
            fp = f"<decrypt failed: {type(exc).__name__}: {str(exc)[:80]}>"
        flag = "  <-- ACTIVE" if r["state"] == "active" else ""
        merch_warn = "" if r["merchant_id"] == EXPECTED_MERCHANT else "  (NOT the expected merchant!)"
        print(f"merchant={r['merchant_id']}  state={r['state']}{flag}{merch_warn}")
        print(f"  created={r['created_at']}")
        print(f"  updated={r['updated_at']}")
        print(f"  token_expires={r['access_token_expires_at']}")
        print(f"  token={fp}")
        print(f"  configured_permissions={r['configured_permissions']!r}  (HARDCODED — ignore for scopes)")
        print()

    usable = [r for r in rows if r["state"] in ("active", "error")]
    print(f"probe scripts select `state IN ('active','error') LIMIT 1` -> {len(usable)} candidate(s).")
    if len(usable) > 1:
        print("  WARNING: multiple candidates. LIMIT 1 with NO ORDER BY is non-deterministic —")
        print("  the probe may be using a stale/wrong-merchant row. Expected active merchant:")
        print(f"  {EXPECTED_MERCHANT}.")
    elif usable and usable[0]["merchant_id"] != EXPECTED_MERCHANT:
        print(f"  The single candidate is merchant {usable[0]['merchant_id']}, NOT {EXPECTED_MERCHANT}.")


if __name__ == "__main__":
    asyncio.run(main())
