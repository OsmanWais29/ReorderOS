"""Diagnostic: does the app's DB role bypass RLS / own the tenant tables?

Resolves whether migration 0022 (FORCE ROW LEVEL SECURITY on the Sprint 5 + a few
older tenant tables) closed a real cross-tenant hole or is defense-in-depth. FORCE
only changes behavior for queries run AS the table OWNER; a superuser / BYPASSRLS
role ignores RLS entirely, and a non-owner role is already bound by plain ENABLE.

Runs the same diagnostic against BOTH connection roles, because they can differ:
  - request path   -> app.core.database.get_sessionmaker      (DATABASE_URL, app_user)
  - webhook/worker -> app.core.service_db.get_service_sessionmaker (SERVICE_DATABASE_URL,
                                                                    service_worker)

Run in the DO console (api or inbox-worker component), from /srv:
    python -m scripts.rls_check

Interpretation per role:
  - current_user == owner AND is_super=f AND bypass_rls=f -> FORCE was load-bearing
    for that role (0022 closed a real hole on the tables it can reach).
  - is_super=t OR bypass_rls=t -> that role bypasses RLS entirely; 0022 is
    defense-in-depth for it (and RLS is NOT the relied-upon isolation control).
  - current_user != owner -> non-owner; already bound by ENABLE, FORCE is
    defense-in-depth for that role.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

_QUERY = text(
    "SELECT current_user AS current_user,"
    " (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_super,"
    " (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user) AS bypass_rls,"
    " (SELECT tableowner FROM pg_tables WHERE tablename = 'menu_items') AS owner"
)


def _interpret(current_user: str, is_super: bool, bypass_rls: bool, owner: str | None) -> str:
    if is_super or bypass_rls:
        return "BYPASSES RLS -> 0022 is defense-in-depth for this role (RLS not the relied-upon control)."
    if owner is not None and current_user == owner:
        return "NON-SUPERUSER OWNER -> FORCE is load-bearing for this role; 0022 closed a real hole."
    return "NON-OWNER -> already bound by ENABLE; FORCE is defense-in-depth for this role."


async def _run(label: str, make_sessionmaker) -> None:
    print(f"\n=== {label} ===")
    try:
        sm = make_sessionmaker()
    except Exception as exc:  # e.g. SERVICE_DATABASE_URL not set on this component
        print(f"  (skipped: engine unavailable — {type(exc).__name__}: {str(exc)[:200]})")
        return
    try:
        async with sm() as s:
            row = (await s.execute(_QUERY)).mappings().one()
    except Exception as exc:
        print(f"  (query failed: {type(exc).__name__}: {str(exc)[:200]})")
        return
    cu = str(row["current_user"])
    is_super = bool(row["is_super"])
    bypass = bool(row["bypass_rls"])
    owner = row["owner"]
    print(f"  current_user : {cu}")
    print(f"  is_super     : {is_super}")
    print(f"  bypass_rls   : {bypass}")
    print(f"  menu_items owner : {owner}")
    print(f"  -> {_interpret(cu, is_super, bypass, owner)}")


async def main() -> None:
    from app.core.database import get_sessionmaker
    from app.core.service_db import get_service_sessionmaker

    print("RLS / FORCE diagnostic — running against both connection roles")
    await _run("request path (DATABASE_URL / app_user)", get_sessionmaker)
    await _run("worker (SERVICE_DATABASE_URL / service_worker)", get_service_sessionmaker)
    print()


if __name__ == "__main__":
    asyncio.run(main())
