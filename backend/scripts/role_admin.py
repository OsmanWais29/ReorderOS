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
  - Managed-PG reality (DigitalOcean): the effective administrator (doadmin) is NOT a
    PostgreSQL superuser (rolsuper=False, rolcreaterole=True, rolbypassrls=True).
    Precise PostgreSQL rule: changing SUPERUSER always requires a true superuser (even
    the no-op NOSUPERUSER spelling — the live B1 failure, DO managed PG 17.10,
    2026-08-17); changing REPLICATION or BYPASSRLS additionally requires an
    administrator that itself HOLDS the attribute. What a provider's admin holds is
    not portable, so ReorderOS deliberately treats all three as VERIFY-ONLY: read from
    pg_roles and required False BEFORE any mutation, fail-closed. They are never sent
    in ALTER and never silently preserved; remediation of an enabled one needs a true
    superuser or the database provider's support.
  - Every mutating command (provision-app / provision-worker / rotate) is ONE
    transaction end-to-end: capability check, existence check, optional CREATE ROLE,
    mutable-attribute normalization (NOCREATEDB NOCREATEROLE INHERIT), password+LOGIN,
    membership normalization, and a FINAL in-transaction catalog contract assertion.
    Any failure rolls back everything: a failed fresh provision leaves NO role behind;
    a failed update leaves password, LOGIN state, attributes, and memberships untouched.
  - `capability` (also enforced inside every mutating path BEFORE any write) proves
    READ-ONLY that the current administrator can administer the target role and
    grant/revoke every expected membership (CREATEROLE + ADMIN OPTION, or superuser).
    rolcreaterole alone is NOT treated as sufficient for an existing role.
  - The EXACT direct-membership set is enforced (unexpected memberships revoked;
    ADMIN OPTION stripped).
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

# Attributes every CREATEROLE administrator may legally set on roles it administers
# (PG 16/17 rules), applied idempotently on every provision/rotate so a pre-existing
# role that somehow acquired CREATEDB / CREATEROLE is NORMALIZED, not preserved.
# INHERIT is intended (members inherit their group's grants). SUPERUSER / BYPASSRLS /
# REPLICATION are deliberately ABSENT — see _VERIFY_ONLY_ATTRS.
_MUTABLE_HARDEN_ATTRS = "NOCREATEDB NOCREATEROLE INHERIT"

# Verify-only dangerous attributes: must ALREADY be False on the target role before any
# mutation; never sent in ALTER. Precise PostgreSQL rule: SUPERUSER changes always
# require a true superuser; REPLICATION and BYPASSRLS changes additionally require an
# administrator that itself HOLDS the attribute. What a managed provider's admin holds
# is not portable (DO's doadmin: BYPASSRLS yes, SUPERUSER/REPLICATION no — its combined
# old ALTER failed live on the SUPERUSER clause, 2026-08-17), so ReorderOS policy is
# uniform verify-only fail-closed regardless of which clauses would happen to be legal.
_VERIFY_ONLY_ATTRS = ("rolsuper", "rolbypassrls", "rolreplication")


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
    strip ADMIN OPTION from allowed ones. Names come from pg_roles itself and are still
    shape-checked before quoting.

    PG 16+ tracks (group, GRANTOR) per grant: the same membership can exist as MULTIPLE
    rows under different grantors, and a plain REVOKE removes only the grants the
    current administrator made. So every wrong grant row is revoked PRECISELY
    (`GRANTED BY`), and an expected membership already present as exactly one plain
    grant is LEFT UNTOUCHED — the old unconditional re-grant would duplicate it under a
    second grantor. A grant this administrator cannot revoke (e.g. made by a superuser)
    raises InsufficientPrivilege and rolls back the whole mutation — fail-closed;
    remediation belongs to the original grantor or a true superuser."""
    expected = expected_memberships(role)
    rows = await conn.fetch(
        "SELECT r.rolname AS grp, am.admin_option AS admin, g.rolname AS grantor "
        "FROM pg_auth_members am "
        "JOIN pg_roles r ON r.oid = am.roleid "
        "JOIN pg_roles g ON g.oid = am.grantor "
        "WHERE am.member = (SELECT oid FROM pg_roles WHERE rolname = $1)",
        role,
    )
    grants: dict[str, list[tuple[str, bool]]] = {}
    for row in rows:
        grp, grantor = str(row["grp"]), str(row["grantor"])
        for name in (grp, grantor):
            if not name.replace("_", "").isalnum():
                raise SystemExit(f"unexpected role name shape from catalog: {name!r}")
        grants.setdefault(grp, []).append((grantor, bool(row["admin"])))
    for grp, grant_rows in grants.items():
        exactly_one_plain = len(grant_rows) == 1 and grant_rows[0][1] is False
        if grp in expected and exactly_one_plain:
            continue  # already contract-shaped; re-granting would duplicate it
        for grantor, _admin in grant_rows:
            await conn.execute(f'REVOKE "{grp}" FROM "{role}" GRANTED BY "{grantor}"')
        if grp in expected:
            await conn.execute(f'GRANT "{grp}" TO "{role}"')  # exactly one plain grant
    for grp in expected:
        if grp not in grants:
            await conn.execute(f'GRANT "{grp}" TO "{role}"')


def _create_role_sql(role: str) -> str:
    # Callers constrain `role` to managed names before this is reached. The NO* keywords
    # are the CREATE defaults and ARE permitted for a CREATEROLE non-superuser (unlike
    # ALTER's SUPERUSER clause, which is superuser-only — the live B1 failure). CREATE
    # ROLE is transactional DDL, so a later failure in the same transaction removes the
    # role again.
    return (
        f'CREATE ROLE "{role}" NOLOGIN '
        "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT"
    )


async def _assert_verify_only_attrs_disabled(conn: Any, role: str) -> None:
    """Fail-closed dangerous-attribute gate: rolsuper / rolbypassrls / rolreplication
    must ALREADY be False (missing/unknown counts as failure).

    Precise PostgreSQL rule (docs: ALTER ROLE / role attributes): changing SUPERUSER
    always requires a true superuser; changing REPLICATION or BYPASSRLS additionally
    requires an administrator that itself HOLDS that attribute. Which of those a
    managed-PG administrator holds varies by provider and is NOT assumed portable, so
    ReorderOS policy treats all three as VERIFY-ONLY for deterministic fail-closed
    behavior: an enabled one refuses before mutation and requires deliberate
    true-superuser / provider-support remediation. Silently preserving one is
    forbidden. Prints role and attribute names/booleans only — never DSNs, passwords,
    or catalog internals."""
    row = await conn.fetchrow(
        "SELECT rolsuper, rolbypassrls, rolreplication FROM pg_roles WHERE rolname = $1",
        role,
    )
    if row is None:
        raise SystemExit(
            f"refusing to touch {role!r}: role not readable in pg_roles during mutation"
        )
    enabled = [a for a in _VERIFY_ONLY_ATTRS if row[a] is not False]
    if enabled:
        raise SystemExit(
            f"refusing to touch {role!r}: verify-only dangerous attribute(s) enabled: "
            + ", ".join(f"{a}={row[a]}" for a in enabled)
            + " — this tool never alters SUPERUSER/BYPASSRLS/REPLICATION (SUPERUSER "
            "needs a true superuser; BYPASSRLS/REPLICATION need an administrator that "
            "itself holds the attribute — not assumed on managed PostgreSQL); "
            "remediate via a true superuser or the database provider's support, "
            "then re-run. Nothing was changed."
        )


async def admin_capability(conn: Any, role: str) -> dict[str, bool]:
    """READ-ONLY administrative-capability report for mutating `role`.

    PG 16+ CREATEROLE semantics: a non-superuser administrator may ALTER / GRANT /
    REVOKE only roles it holds ADMIN OPTION on (creating a role auto-grants the creator
    ADMIN OPTION on it) — rolcreaterole alone proves NOTHING about an existing role,
    so it is never treated as sufficient. Reports booleans only."""
    expected = expected_memberships(role)  # also refuses non-managed roles
    row = await conn.fetchrow(
        "SELECT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS su,"
        " (SELECT rolcreaterole FROM pg_roles WHERE rolname = current_user) AS cr,"
        " EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1) AS target_exists",
        role,
    )
    is_super = row["su"] is True
    createrole = row["cr"] is True
    exists = row["target_exists"] is True
    if exists:
        admin_on_target = await conn.fetchval(
            "SELECT pg_has_role(current_user, $1, 'MEMBER WITH ADMIN OPTION')", role
        )
        can_admin = is_super or (createrole and admin_on_target is True)
    else:
        # Creating: CREATEROLE suffices; PG 16+ auto-grants the creator ADMIN OPTION.
        can_admin = is_super or createrole
    caps: dict[str, bool] = {
        "admin_is_superuser": is_super,
        "admin_has_createrole": createrole,
        "target_exists": exists,
        "can_administer_target": can_admin,
    }
    for grp in sorted(expected):
        has_admin_on_group = await conn.fetchval(
            "SELECT pg_has_role(current_user, $1, 'MEMBER WITH ADMIN OPTION')", grp
        )
        caps[f"can_grant_{grp}"] = is_super or has_admin_on_group is True
    return caps


def capability_violations(caps: dict[str, bool]) -> list[str]:
    """PURE check over an admin_capability() report — names/booleans only."""
    violations: list[str] = []
    if not caps.get("can_administer_target"):
        violations.append(
            "can_administer_target=False (needs CREATEROLE plus ADMIN OPTION on the "
            "target role, or superuser)"
        )
    for key, value in caps.items():
        if key.startswith("can_grant_") and value is not True:
            violations.append(
                f"{key}=False (needs ADMIN OPTION on {key[len('can_grant_') :]!r}, or superuser)"
            )
    return violations


async def capability_report(admin_dsn: str, role: str) -> dict[str, bool]:
    import asyncpg

    conn = await asyncpg.connect(_plain(admin_dsn))
    try:
        return await admin_capability(conn, role)
    finally:
        await conn.close()


async def _apply_role_contract(conn: Any, role: str, pw: str, *, allow_create: bool) -> None:
    """The single mutation path for provision-app / provision-worker / rotate. MUST run
    inside the caller's sole (non-nested) transaction so that every step — capability
    check, existence check, optional CREATE, fail-closed dangerous-attribute gate,
    mutable-attribute normalization, password + LOGIN, membership normalization, final
    catalog contract assertion — commits or rolls back as ONE unit. A failure after
    CREATE leaves no role; a failure while updating an existing role leaves its previous
    password, LOGIN state, attributes, and memberships untouched."""
    caps = await admin_capability(conn, role)
    problems = capability_violations(caps)
    if problems:
        raise SystemExit(
            f"refusing to touch {role!r}: administrator capability check failed: "
            + "; ".join(problems)
        )
    if not caps["target_exists"]:
        if not allow_create:
            raise SystemExit(f"refusing to rotate {role!r}: role does not exist")
        await conn.execute(_create_role_sql(role))
    await _assert_verify_only_attrs_disabled(conn, role)
    await conn.execute(f'ALTER ROLE "{role}" {_MUTABLE_HARDEN_ATTRS}')
    await conn.execute(f"ALTER ROLE \"{role}\" LOGIN PASSWORD '{pw}'")  # pw validated hex
    await _normalize_memberships(conn, role)
    # Final in-transaction assertion: the COMPLETE contract (attributes, exact
    # memberships, admin option, app_user closure) must hold before commit.
    attrs = await _catalog_attributes(conn, role)
    violations = check_contract(attrs, role)
    if violations:
        raise SystemExit(
            f"post-mutation contract check failed for {role!r}; rolling back: "
            + "; ".join(violations)
        )


async def provision_reorderos_app(admin_dsn: str, pw: str) -> None:
    """Provision the request-pool role. Deliberately INDEPENDENT of service_worker —
    the parent-contract validation belongs exclusively to the versioned-worker path
    (a versioned worker INHERITS its parent; reorderos_app inherits app_user only)."""
    import asyncpg

    conn = await asyncpg.connect(_plain(admin_dsn))
    try:
        async with conn.transaction():
            await _apply_role_contract(conn, "reorderos_app", pw, allow_create=True)
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
        async with conn.transaction():
            await _assert_parent_worker_contract(conn)
            await _apply_role_contract(conn, role, pw, allow_create=True)
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
    """Rotation never creates: a missing role is a refusal (allow_create=False), and the
    capability gate refuses BEFORE any write when the administrator cannot administer
    the role — never inferred from rolcreaterole alone."""
    if not is_managed(role):
        raise SystemExit(f"refusing to touch non-managed role {role!r}")
    import asyncpg

    conn = await asyncpg.connect(_plain(admin_dsn))
    try:
        async with conn.transaction():
            await _apply_role_contract(conn, role, pw, allow_create=False)
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
    cap = sub.add_parser(
        "capability",
        help="READ-ONLY: prove the current administrator can provision/rotate this role "
        "(CREATEROLE + ADMIN OPTION, or superuser) — exit 1 if not; changes nothing",
    )
    cap.add_argument("role")
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

    if args.cmd == "capability":  # needs the admin DSN but no $PW; strictly read-only
        caps = asyncio.run(capability_report(_admin_dsn(), args.role))
        for key in sorted(caps):
            print(f"capability {args.role}: {key}={caps[key]}")
        problems = capability_violations(caps)
        if problems:
            for problem in problems:
                print(f"capability {args.role}: INSUFFICIENT: {problem}", file=sys.stderr)
            return 1
        print(f"capability {args.role}: ADMIN CAPABILITY OK")
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
