"""Restricted-runtime-role provisioning + attribute proof via asyncpg.

The runtime image installs only `curl` — NO `psql`. asyncpg is an app dependency, so role
administration runs as `python -m scripts.role_admin …` inside the DO api console, using the
doadmin DATABASE_URL already present in that component's env.

Safety contract:
  - Managed role names only: reorderos_app, service_worker, and versioned replacement
    workers service_worker_vN, N >= 1 with no leading zero (the outage-safe rotation path — runbook
    Phase B-alt: the versioned role is a LOGIN member of service_worker, inheriting its
    grants and RLS policies; the old login is disabled only after the new credential is
    deployed and verified).
  - Password comes from $PW (a hidden `read -s`), validated as ^[0-9a-f]{64}$ before use.
    asyncpg cannot parametrize DDL, so the password is interpolated — the strict hex regex
    is what makes that injection-safe (no quotes/backslashes/spaces possible).
  - Every mutating command (provision-app / provision-worker / rotate) runs its ALTERs and
    membership normalization inside ONE transaction: a failure in ANY step (including
    membership revocation) rolls back the whole change — no partially-mutated login role.
  - Attributes are NORMALIZED, not assumed: NOSUPERUSER NOBYPASSRLS NOCREATEDB
    NOCREATEROLE NOREPLICATION INHERIT on every run, and the EXACT direct-membership set
    is enforced (unexpected memberships revoked; ADMIN OPTION stripped).
  - `prove` connects AS the role, prints every attribute + the exact membership set, and
    VALIDATES the whole contract in code — exit 1 on ANY violation (the runbook gate is
    the exit code, not an operator eyeballing comments).
  - `preflight-rotate` proves (from inside the live container) that no live DSN
    authenticates as a role BEFORE any credential change.
  - Prints ONLY role names and non-secret booleans. Never a password or DSN.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from typing import Any
from urllib.parse import urlparse

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FIXED_MANAGED = {"reorderos_app", "service_worker"}
_VERSIONED_WORKER = re.compile(r"^service_worker_v[1-9][0-9]*$")  # N >= 1, no leading zero
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "", None}

# Every dangerous attribute a runtime role must NOT have. Applied idempotently on BOTH
# provision and rotate, so a pre-existing role that somehow acquired CREATEDB /
# CREATEROLE / REPLICATION (or superuser/bypassrls) is NORMALIZED, not preserved,
# across a rerun. INHERIT is intended (members inherit their group's grants).
_HARDEN_ATTRS = "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT"


def is_managed(role: str) -> bool:
    return role in _FIXED_MANAGED or bool(_VERSIONED_WORKER.match(role))


def expected_memberships(role: str) -> frozenset[str]:
    """The EXACT direct-membership set each managed role may hold. Anything else —
    including an unexpected privileged group, or ADMIN OPTION on an allowed
    membership — is revoked by provisioning/rotation. Documented additions require
    editing this function."""
    if role == "reorderos_app":
        return frozenset({"app_user"})
    if role == "service_worker":
        return frozenset()
    if _VERSIONED_WORKER.match(role):
        # Versioned replacement worker: inherits service_worker's grants + policies.
        return frozenset({"service_worker"})
    raise SystemExit(f"refusing to reason about non-managed role {role!r}")


def validate_pw(pw: str | None) -> str:
    """Return pw iff it is exactly 64 lowercase hex chars; else abort. This is the injection
    guard for the DDL interpolation below."""
    if not pw or not _HEX64.match(pw):
        raise SystemExit("PW must be exactly 64 lowercase hex chars (openssl rand -hex 32)")
    return pw


def _plain(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgres+asyncpg://", "postgres://"
    )


def _ssl_for(host: str | None) -> Any:
    # Local compose DB has no TLS; managed staging/prod requires it.
    return False if host in _LOCAL_HOSTS else "require"


async def _normalize_memberships(conn: Any, role: str) -> None:
    """Enforce the exact membership set: revoke every unexpected direct membership and
    strip ADMIN OPTION from allowed ones (revoke + plain re-grant). Group names come
    from pg_roles itself and are still shape-checked before quoting."""
    expected = expected_memberships(role)
    rows = await conn.fetch(
        "SELECT r.rolname AS grp, am.admin_option AS admin "
        "FROM pg_auth_members am JOIN pg_roles r ON r.oid = am.roleid "
        "WHERE am.member = (SELECT oid FROM pg_roles WHERE rolname = $1)",
        role,
    )
    for row in rows:
        grp = str(row["grp"])
        if not grp.replace("_", "").isalnum():
            raise SystemExit(f"unexpected role name shape from catalog: {grp!r}")
        if grp not in expected:
            await conn.execute(f'REVOKE "{grp}" FROM "{role}"')
        elif bool(row["admin"]):
            await conn.execute(f'REVOKE "{grp}" FROM "{role}"')
            await conn.execute(f'GRANT "{grp}" TO "{role}"')  # plain, no ADMIN OPTION
    for grp in expected:
        await conn.execute(f'GRANT "{grp}" TO "{role}"')  # idempotent (no-op if present)


async def _harden_and_set_password(conn: Any, role: str, pw: str) -> None:
    """ALTERs + membership normalization as ONE transaction: any failure (including a
    failed membership revocation) rolls back everything — never a partially-changed
    login role."""
    async with conn.transaction():
        await conn.execute(f'ALTER ROLE "{role}" {_HARDEN_ATTRS}')
        await conn.execute(f"ALTER ROLE \"{role}\" LOGIN PASSWORD '{pw}'")  # pw validated hex
        await _normalize_memberships(conn, role)


async def provision_reorderos_app(admin_dsn: str, pw: str) -> None:
    """Provision the request-pool role. Deliberately INDEPENDENT of service_worker —
    the parent-contract validation belongs exclusively to the versioned-worker path
    (a versioned worker INHERITS its parent; reorderos_app inherits app_user only)."""
    import asyncpg

    conn = await asyncpg.connect(_plain(admin_dsn))
    try:
        await conn.execute(
            # literal on purpose (matches _HARDEN_ATTRS; the transactional ALTER below
            # normalizes a pre-existing role, so the CREATE branch never drifts from it)
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='reorderos_app') "
            "THEN CREATE ROLE reorderos_app NOLOGIN "
            "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT; "
            "END IF; END $$;"
        )
        await _harden_and_set_password(conn, "reorderos_app", pw)
    finally:
        await conn.close()


async def _assert_parent_worker_contract(conn: Any) -> None:
    """Refuse to create/alter a versioned worker unless its PARENT (service_worker) is
    clean. The versioned role INHERITS everything the parent has — a drifted parent
    (dangerous attribute, unexpected membership, ADMIN OPTION, or any app_user
    membership, direct or indirect) would silently flow into the 'fresh' replacement
    credential. Raises SystemExit with role/attribute NAMES ONLY. Runs BEFORE any
    CREATE/ALTER, so a refusal leaves no new role and changes no existing one."""
    attrs = await _catalog_attributes(conn, "service_worker")
    if not attrs:
        raise SystemExit("refusing versioned worker: parent role service_worker does not exist")
    violations: list[str] = []
    for attribute in ("rolsuper", "rolbypassrls", "rolcreatedb", "rolcreaterole", "rolreplication"):
        if attrs.get(attribute) is not False:
            violations.append(f"{attribute}={attrs.get(attribute)} (expected False)")
    unexpected = [
        g for g in attrs.get("member_of", []) if g not in expected_memberships("service_worker")
    ]
    if unexpected:
        violations.append(f"unexpected direct membership(s): {unexpected}")
    if attrs.get("admin_option_anywhere") is not False:
        violations.append("admin_option_anywhere=True (expected False)")
    # direct OR indirect membership of the request-path group is a pool-crossing hazard:
    if attrs.get("app_user_member") is not False:
        violations.append("app_user_member=True (expected False; direct or indirect)")
    if violations:
        raise SystemExit(
            "refusing versioned worker: parent service_worker contract is not clean: "
            + "; ".join(violations)
        )


async def provision_versioned_worker(admin_dsn: str, role: str, pw: str) -> None:
    """Outage-safe worker-credential rotation (Phase B-alt): create the versioned
    replacement login role as a hardened MEMBER of service_worker. The old login stays
    valid until the new DSN is deployed and verified; only then is the old role's login
    disabled (never dropped). The PARENT contract is asserted FIRST — a refusal creates
    nothing and alters nothing."""
    if not _VERSIONED_WORKER.match(role):
        raise SystemExit(
            f"refusing to provision {role!r}: versioned workers must be service_worker_vN "
            f"(N an integer >= 1, no leading zero)"
        )
    import asyncpg

    conn = await asyncpg.connect(_plain(admin_dsn))
    try:
        await _assert_parent_worker_contract(conn)
        await conn.execute(
            "DO $$ BEGIN "  # noqa: S608 - role is constrained by _VERSIONED_WORKER.
            "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='" + role + "') "
            'THEN CREATE ROLE "' + role + '" NOLOGIN '
            "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT; "
            "END IF; END $$;"
        )
        await _harden_and_set_password(conn, role, pw)
    finally:
        await conn.close()


async def _catalog_attributes(conn: Any, role: str) -> dict[str, Any]:
    """Read a role contract through an administrative connection.

    A versioned worker inherits the parent service_worker's grants and RLS-policy
    applicability. Its own direct membership can look clean while the parent has drifted,
    so the parent contract is checked before the replacement role is created.
    """
    row = await conn.fetchrow(
        "SELECT rolname AS role, rolcanlogin, rolsuper, rolbypassrls, rolcreatedb,"
        " rolcreaterole, rolreplication, rolinherit,"
        " pg_has_role(rolname,'app_user','MEMBER') AS app_user_member"
        " FROM pg_roles WHERE rolname = $1",
        role,
    )
    if row is None:
        return {}
    members = await conn.fetch(
        "SELECT r.rolname AS grp, am.admin_option AS admin "
        "FROM pg_auth_members am JOIN pg_roles r ON r.oid = am.roleid "
        "WHERE am.member = (SELECT oid FROM pg_roles WHERE rolname = $1) "
        "ORDER BY r.rolname",
        role,
    )
    attrs = dict(row)
    attrs["member_of"] = [str(m["grp"]) for m in members]
    attrs["admin_option_anywhere"] = any(bool(m["admin"]) for m in members)
    return attrs


async def rotate_password(admin_dsn: str, role: str, pw: str) -> None:
    if not is_managed(role):
        raise SystemExit(f"refusing to touch non-managed role {role!r}")
    import asyncpg

    conn = await asyncpg.connect(_plain(admin_dsn))
    try:
        await _harden_and_set_password(conn, role, pw)
    finally:
        await conn.close()


async def role_attributes(admin_dsn: str, role: str, pw: str) -> dict[str, Any]:
    """Connect AS `role` and return its non-secret attributes (booleans + memberships)."""
    if not is_managed(role):
        raise SystemExit(f"refusing to connect as non-managed role {role!r}")
    import asyncpg

    u = urlparse(_plain(admin_dsn))
    conn = await asyncpg.connect(
        user=role,
        password=pw,
        host=u.hostname,
        port=u.port or 5432,
        database=(u.path or "/").lstrip("/"),
        ssl=_ssl_for(u.hostname),
    )
    try:
        row = await conn.fetchrow(
            "SELECT current_user AS role,"
            " (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) AS rolsuper,"
            " (SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user) AS rolbypassrls,"
            " (SELECT rolcanlogin FROM pg_roles WHERE rolname=current_user) AS rolcanlogin,"
            " (SELECT rolcreatedb FROM pg_roles WHERE rolname=current_user) AS rolcreatedb,"
            " (SELECT rolcreaterole FROM pg_roles WHERE rolname=current_user) AS rolcreaterole,"
            " (SELECT rolreplication FROM pg_roles WHERE rolname=current_user) AS rolreplication,"
            " (SELECT rolinherit FROM pg_roles WHERE rolname=current_user) AS rolinherit,"
            " pg_has_role(current_user,'app_user','MEMBER') AS app_user_member"
        )
        # EXACT direct-membership set + any ADMIN OPTION (pg_auth_members is world-
        # readable, unlike pg_authid) — the contract check covers the whole set.
        members = await conn.fetch(
            "SELECT r.rolname AS grp, am.admin_option AS admin "
            "FROM pg_auth_members am JOIN pg_roles r ON r.oid = am.roleid "
            "WHERE am.member = (SELECT oid FROM pg_roles WHERE rolname = current_user) "
            "ORDER BY r.rolname"
        )
    finally:
        await conn.close()
    attrs = dict(row) if row else {}
    attrs["member_of"] = [str(m["grp"]) for m in members]
    attrs["admin_option_anywhere"] = any(bool(m["admin"]) for m in members)
    return attrs


def check_contract(attrs: dict[str, Any], role: str) -> list[str]:
    """PURE contract check for `prove` — every violation returned, values are booleans
    and role names only. The runbook gate is `prove`'s exit code, not eyeballed output."""
    violations: list[str] = []
    if attrs.get("role") != role:
        violations.append(f"connected as {attrs.get('role')!r}, expected {role!r}")
    for attribute, want in (
        ("rolcanlogin", True),
        ("rolsuper", False),
        ("rolbypassrls", False),
        ("rolcreatedb", False),
        ("rolcreaterole", False),
        ("rolreplication", False),
        ("rolinherit", True),
    ):
        if attrs.get(attribute) is not want:
            violations.append(f"{attribute}={attrs.get(attribute)} (expected {want})")
    expected_members = sorted(expected_memberships(role))
    if attrs.get("member_of") != expected_members:
        violations.append(
            f"member_of={attrs.get('member_of')} (expected exactly {expected_members})"
        )
    if attrs.get("admin_option_anywhere") is not False:
        violations.append("admin_option_anywhere=True (expected False)")
    expected_app_user_member = role == "reorderos_app"
    if attrs.get("app_user_member") is not expected_app_user_member:
        violations.append(
            f"app_user_member={attrs.get('app_user_member')} (expected {expected_app_user_member})"
        )
    return violations


def rotation_preflight(role: str, environ: dict[str, str] | None = None) -> dict[str, bool]:
    """Prove a password rotation for `role` CANNOT break the RUNNING deployment.

    Run inside the live api container, whose environment IS the running deployment's:
    if either live DSN (DATABASE_URL / SERVICE_DATABASE_URL) authenticates as `role`,
    rotating that role's password would strand the running pools on a dead credential
    — and any captured rollback spec would carry the dead credential too. In that case
    rotation must NOT proceed here; use the versioned-replacement-role procedure
    (runbook Phase B-alt) instead.

    SCOPE (be precise): this checks THIS container's effective env only. The runbook's
    B0 derives the complete running service/worker checklist from the fresh live spec and
    repeats this command inside EVERY listed container. Returns
    {env_name: uses_role} booleans. NEVER prints or returns a DSN/password."""
    env = environ if environ is not None else dict(os.environ)
    out: dict[str, bool] = {}
    for name in ("DATABASE_URL", "SERVICE_DATABASE_URL"):
        dsn = env.get(name) or ""
        username = urlparse(_plain(dsn)).username if dsn else None
        out[name] = username == role
    return out


def _admin_dsn() -> str:
    dsn = os.environ.get("ADMIN_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("set ADMIN_DATABASE_URL (or DATABASE_URL) to the doadmin DSN")
    return dsn


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Restricted-role provisioning (asyncpg; no psql).")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("provision-app", help="create/enforce reorderos_app LOGIN from $PW")
    pw_worker = sub.add_parser(
        "provision-worker",
        help="create/enforce a versioned replacement worker login (service_worker_vN) "
        "from $PW — the outage-safe rotation path (Phase B-alt)",
    )
    pw_worker.add_argument("role")
    r = sub.add_parser("rotate", help="rotate a managed role's password from $PW")
    r.add_argument("role")
    pr = sub.add_parser(
        "prove",
        help="connect AS the role, print every attribute + exact memberships, and "
        "VALIDATE the contract — exit 1 on any violation",
    )
    pr.add_argument("role")
    pf = sub.add_parser(
        "preflight-rotate",
        help="prove the RUNNING deployment does not authenticate as this role "
        "(run inside the live api container BEFORE any rotation)",
    )
    pf.add_argument("role")
    args = p.parse_args(argv)

    if hasattr(args, "role") and not is_managed(args.role):
        print(f"refusing to operate on non-managed role {args.role!r}", file=sys.stderr)
        return 2

    if args.cmd == "preflight-rotate":  # needs no $PW and no DB connection
        usage = rotation_preflight(args.role)
        for name, uses in usage.items():
            print(f"preflight-rotate {args.role}: {name}_uses_role={uses}")
        if any(usage.values()):
            print(
                f"preflight-rotate {args.role}: IN USE by the running deployment — "
                f"rotating here would strand its pools AND poison the rollback spec. "
                f"STOP; use the versioned-replacement procedure (runbook Phase B-alt).",
                file=sys.stderr,
            )
            return 1
        print(f"preflight-rotate {args.role}: safe — no live DSN authenticates as it")
        return 0

    pw = validate_pw(os.environ.get("PW"))
    dsn = _admin_dsn()
    if args.cmd == "provision-app":
        asyncio.run(provision_reorderos_app(dsn, pw))
        print("reorderos_app: provisioned (hardened attrs + exact memberships, atomic)")
    elif args.cmd == "provision-worker":
        asyncio.run(provision_versioned_worker(dsn, args.role, pw))
        print(f"{args.role}: provisioned (hardened member of service_worker, atomic)")
    elif args.cmd == "rotate":
        asyncio.run(rotate_password(dsn, args.role, pw))
        print(f"{args.role}: password rotated (attrs + memberships re-normalized, atomic)")
    elif args.cmd == "prove":
        attrs = asyncio.run(role_attributes(dsn, args.role, pw))
        member_of = ",".join(attrs.get("member_of", [])) or "(none)"
        print(
            f"role={attrs.get('role')} rolcanlogin={attrs.get('rolcanlogin')} "
            f"rolsuper={attrs.get('rolsuper')} rolbypassrls={attrs.get('rolbypassrls')} "
            f"rolcreatedb={attrs.get('rolcreatedb')} rolcreaterole={attrs.get('rolcreaterole')} "
            f"rolreplication={attrs.get('rolreplication')} rolinherit={attrs.get('rolinherit')} "
            f"member_of={member_of} admin_option={attrs.get('admin_option_anywhere')}"
        )
        violations = check_contract(attrs, args.role)
        if violations:
            for violation in violations:
                print(f"prove {args.role} CONTRACT VIOLATION: {violation}", file=sys.stderr)
            return 1
        print(f"prove {args.role}: CONTRACT OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
