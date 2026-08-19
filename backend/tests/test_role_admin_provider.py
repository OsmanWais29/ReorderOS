"""Provider-shaped role-administration tests (correction round after the staging B1 failure).

The 2026-08-17 staging B1 attempt proved the old test environment unrepresentative: every
role-admin test ran THROUGH a true local superuser, while DigitalOcean's doadmin is
rolsuper=False / rolcreaterole=True / rolbypassrls=True. Precise PostgreSQL rule (ALTER
ROLE, 16+): changing SUPERUSER always requires a true superuser — even the no-op
NOSUPERUSER spelling — while changing REPLICATION or BYPASSRLS additionally requires an
administrator that itself HOLDS that attribute. The old combined `_HARDEN_ATTRS` ALTER
therefore always failed on DigitalOcean (on its SUPERUSER clause) and never could have
worked; per-clause behavior is recorded separately below. ReorderOS policy treats all
three attributes as verify-only regardless of which clauses a given provider's
administrator could legally change.

These tests run every mutating path through a DigitalOcean-shaped NON-superuser
administrator (`do_admin_like`) against the local PG 17 compose DB and pin:
  - the original defect (the old ALTER string is refused for this admin);
  - success + full contract through the provider-shaped admin;
  - fail-closed refusal with ZERO mutation when a verify-only dangerous attribute is
    already enabled;
  - real end-to-end atomicity (failed fresh provision leaves NO role; failed update
    leaves the previous state byte-intact);
  - capability refusal when the administrator lacks ADMIN OPTION (weak_admin_like);
  - idempotent absorption of the inert NOLOGIN shell the failed staging B1 left behind;
  - no password/DSN in any CLI output.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from typing import Any
from urllib.parse import urlparse

import pytest

import scripts.role_admin as ra
from scripts.role_admin import (
    check_contract,
    provision_reorderos_app,
    provision_versioned_worker,
    role_attributes,
    rotate_password,
)
from tests.conftest import DB_URL_SYNC

pytestmark = pytest.mark.integration

_DO_ADMIN = "do_admin_like"
_WEAK_ADMIN = "weak_admin_like"
# Local-only test credentials for the disposable compose DB (same class as the suite's
# existing 'service_worker' password) — never used against a remote host.
_DO_ADMIN_PW = "do-admin-like-local-test"
_WEAK_ADMIN_PW = "weak-admin-like-local-test"


def _is_local(dsn: str) -> bool:
    host = (urlparse(dsn.replace("postgresql+asyncpg://", "postgresql://")).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", ""}


def _dsn_as(user: str, password: str) -> str:
    u = urlparse(DB_URL_SYNC)
    return f"postgresql://{user}:{password}@{u.hostname}:{u.port or 5432}{u.path}"


_DO_DSN = _dsn_as(_DO_ADMIN, _DO_ADMIN_PW)
_WEAK_DSN = _dsn_as(_WEAK_ADMIN, _WEAK_ADMIN_PW)


async def _su_exec(*statements: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(DB_URL_SYNC)
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


async def _su_fetchrow(query: str, *args: object) -> Any:
    import asyncpg

    conn = await asyncpg.connect(DB_URL_SYNC)
    try:
        return await conn.fetchrow(query, *args)
    finally:
        await conn.close()


async def _authid_row(role: str) -> dict | None:
    row = await _su_fetchrow(
        "SELECT rolname, rolpassword, rolcanlogin, rolsuper, rolbypassrls, rolcreatedb,"
        " rolcreaterole, rolreplication, rolinherit FROM pg_authid WHERE rolname = $1",
        role,
    )
    return dict(row) if row is not None else None


async def _memberships(role: str) -> list[tuple[str, bool]]:
    import asyncpg

    conn = await asyncpg.connect(DB_URL_SYNC)
    try:
        rows = await conn.fetch(
            "SELECT r.rolname AS grp, am.admin_option AS admin "
            "FROM pg_auth_members am JOIN pg_roles r ON r.oid = am.roleid "
            "WHERE am.member = (SELECT oid FROM pg_roles WHERE rolname = $1) "
            "ORDER BY r.rolname",
            role,
        )
        return [(str(r["grp"]), bool(r["admin"])) for r in rows]
    finally:
        await conn.close()


async def _role_exists(role: str) -> bool:
    row = await _su_fetchrow("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
    return row is not None


async def _create_admin_fixtures() -> None:
    """True local superuser creates the DigitalOcean-shaped administrator (and a weak
    CREATEROLE-only admin with NO ADMIN OPTION anywhere, for capability-refusal tests)."""
    for role in (_DO_ADMIN, _WEAK_ADMIN):
        if await _role_exists(role):
            await _su_exec(f'DROP OWNED BY "{role}"', f'DROP ROLE "{role}"')
    await _su_exec(
        # DigitalOcean doadmin shape, observed live 2026-08-17: LOGIN, NOSUPERUSER,
        # CREATEROLE, CREATEDB, BYPASSRLS, NOREPLICATION.
        f"CREATE ROLE \"{_DO_ADMIN}\" LOGIN PASSWORD '{_DO_ADMIN_PW}' "
        "NOSUPERUSER CREATEROLE CREATEDB BYPASSRLS NOREPLICATION INHERIT",
        # Staging equivalence: doadmin created app_user/service_worker via migrations, so
        # PG 16+ auto-granted it ADMIN OPTION on both. Locally the superuser created
        # them, so the grant is explicit here.
        f'GRANT app_user TO "{_DO_ADMIN}" WITH ADMIN OPTION',
        f'GRANT service_worker TO "{_DO_ADMIN}" WITH ADMIN OPTION',
        f"CREATE ROLE \"{_WEAK_ADMIN}\" LOGIN PASSWORD '{_WEAK_ADMIN_PW}' "
        "NOSUPERUSER CREATEROLE NOCREATEDB NOBYPASSRLS NOREPLICATION INHERIT",
    )
    # Staging equivalence for reorderos_app: on staging, doadmin CREATED the role (the
    # failed B1 shell), so PG 16+ auto-granted it ADMIN OPTION. Locally the SUPERUSER
    # test suite may have created it first, so mirror that grant explicitly. (This is
    # exactly the capability the live B1 gate re-proves before provisioning.)
    if await _role_exists("reorderos_app"):
        await _su_exec(f'GRANT reorderos_app TO "{_DO_ADMIN}" WITH ADMIN OPTION')


async def _drop_admin_fixtures() -> None:
    import asyncpg

    # Membership grants made BY do_admin_like (as grantor) block DROP ROLE; re-own the
    # contract grant as the superuser first, then drop. Best-effort NOLOGIN fallback so
    # a partial teardown never leaves a usable extra login on the shared local DB.
    for role in (_DO_ADMIN, _WEAK_ADMIN):
        if not await _role_exists(role):
            continue
        try:
            if await _role_exists("reorderos_app"):
                await _su_exec(
                    "REVOKE app_user FROM reorderos_app",
                    "GRANT app_user TO reorderos_app",
                )
            await _su_exec(f'DROP OWNED BY "{role}"', f'DROP ROLE "{role}"')
        except asyncpg.PostgresError:
            await _su_exec(f'ALTER ROLE "{role}" NOLOGIN')


async def _server_version_num() -> int:
    import asyncpg

    conn = await asyncpg.connect(DB_URL_SYNC)
    try:
        return int(await conn.fetchval("SELECT current_setting('server_version_num')"))
    finally:
        await conn.close()


@pytest.fixture(scope="module", autouse=True)
def _admin_fixtures() -> Any:
    if not _is_local(DB_URL_SYNC):
        pytest.skip("mutates roles; only against a LOCAL database")
    # Provider-fidelity gate: these tests encode PG 17 CREATEROLE semantics and must
    # FAIL LOUDLY (never skip) on any other server — the 2026-08-17 round found a stray
    # local PG 15 silently answering on the compose port, which is exactly the
    # mis-wiring this guard exists to catch.
    version_num = asyncio.run(_server_version_num())
    if not (170000 <= version_num < 180000):
        pytest.fail(
            f"provider-shaped role tests require the PostgreSQL 17 compose service, but "
            f"the server at the test DSN reports server_version_num={version_num}. "
            f"Another PostgreSQL (e.g. a Homebrew install) is likely bound to the "
            f"compose host port — start the compose db and point DATABASE_URL at it "
            f"(override the host port with REORDEROS_DB_PORT if 5433 is taken)."
        )
    asyncio.run(_create_admin_fixtures())
    yield
    asyncio.run(_drop_admin_fixtures())


async def _disable_login(role: str) -> None:
    await _su_exec(
        f'ALTER ROLE "{role}" NOLOGIN',
        f"ALTER ROLE \"{role}\" PASSWORD '{secrets.token_hex(32)}'",
    )


# ── fixture fidelity ──────────────────────────────────────────────────────────
async def test_do_admin_fixture_matches_digitalocean_shape() -> None:
    """The admin these tests connect through must be exactly DO-shaped: NOT a superuser,
    CREATEROLE, BYPASSRLS — the combination that made the old code fail on staging while
    every old (superuser-run) test passed."""
    import asyncpg

    conn = await asyncpg.connect(_DO_DSN)
    try:
        row = await conn.fetchrow(
            "SELECT current_user AS who,"
            " (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS su,"
            " (SELECT rolcreaterole FROM pg_roles WHERE rolname = current_user) AS cr,"
            " (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user) AS rls"
        )
    finally:
        await conn.close()
    assert row["who"] == _DO_ADMIN
    assert row["su"] is False and row["cr"] is True and row["rls"] is True


async def test_old_harden_alter_is_refused_for_do_shaped_admin() -> None:
    """THE ORIGINAL DEFECT, pinned: the retired combined `_HARDEN_ATTRS` ALTER is
    refused by PostgreSQL for this non-superuser administrator — exactly the staging B1
    failure (it fails on its SUPERUSER clause; this does NOT prove every clause is
    individually illegal — see test_alter_clause_rules_recorded_per_clause). A test
    environment where the combined statement SUCCEEDS is not provider-representative."""
    import asyncpg

    conn = await asyncpg.connect(_DO_DSN)
    try:
        await conn.execute("CREATE ROLE tmp_pg17_probe NOLOGIN")
        try:
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.execute(
                    "ALTER ROLE tmp_pg17_probe "
                    "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT"
                )
            # the replacement mutable-only ALTER must be allowed for the same admin:
            await conn.execute(f"ALTER ROLE tmp_pg17_probe {ra._MUTABLE_HARDEN_ATTRS}")
        finally:
            await conn.execute("DROP ROLE tmp_pg17_probe")
    finally:
        await conn.close()


async def test_alter_clause_rules_recorded_per_clause() -> None:
    """Per-clause PG 17 rules, recorded through provider-shaped administrators:
    - NOSUPERUSER: always superuser-only → refused for do_admin_like;
    - NOBYPASSRLS: legal ONLY for an administrator itself holding BYPASSRLS →
      ALLOWED for do_admin_like (which has it), refused for weak_admin_like (lacks it);
    - NOREPLICATION: requires REPLICATION on the administrator → refused for
      do_admin_like (which lacks it).
    ReorderOS keeps all three VERIFY-ONLY regardless — which clauses happen to be legal
    for one provider's administrator is not portable."""
    import asyncpg

    da = await asyncpg.connect(_DO_DSN)
    try:
        await da.execute("CREATE ROLE tmp_clause_probe NOLOGIN")
        try:
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await da.execute("ALTER ROLE tmp_clause_probe NOSUPERUSER")
            # do_admin_like HOLDS BYPASSRLS → this clause alone is legal for it:
            await da.execute("ALTER ROLE tmp_clause_probe NOBYPASSRLS")
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await da.execute("ALTER ROLE tmp_clause_probe NOREPLICATION")
        finally:
            await da.execute("DROP ROLE tmp_clause_probe")
    finally:
        await da.close()
    wa = await asyncpg.connect(_WEAK_DSN)
    try:
        await wa.execute("CREATE ROLE tmp_weak_probe NOLOGIN")
        try:
            # weak_admin_like does NOT hold BYPASSRLS → the same clause is refused:
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await wa.execute("ALTER ROLE tmp_weak_probe NOBYPASSRLS")
        finally:
            await wa.execute("DROP ROLE tmp_weak_probe")
    finally:
        await wa.close()


# ── success paths through the provider-shaped admin ───────────────────────────
async def test_provision_app_via_do_admin_succeeds_and_contract_holds() -> None:
    pw = secrets.token_hex(32)
    try:
        await provision_reorderos_app(_DO_DSN, pw)
        attrs = await role_attributes(_DO_DSN, "reorderos_app", pw)
        assert check_contract(attrs, "reorderos_app") == []
        assert attrs["member_of"] == ["app_user"]
    finally:
        await _disable_login("reorderos_app")


async def test_inert_staging_shell_is_provisioned_idempotently() -> None:
    """REQUIREMENT 6: the failed staging B1 left `reorderos_app` as an inert shell —
    NOLOGIN, no password, no memberships, all dangerous attributes False. Recreate that
    exact residue and prove the corrected provisioner absorbs it idempotently (no manual
    DROP ROLE required)."""
    if await _role_exists("reorderos_app"):
        await _su_exec("REVOKE app_user FROM reorderos_app", "DROP ROLE reorderos_app")
    await _su_exec(
        "CREATE ROLE reorderos_app NOLOGIN "
        "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT",
        # On staging, doadmin created the shell, so it holds ADMIN OPTION on it —
        # replicate that relationship for the DO-shaped admin:
        f'GRANT reorderos_app TO "{_DO_ADMIN}" WITH ADMIN OPTION',
    )
    shell = await _authid_row("reorderos_app")
    assert shell is not None and shell["rolcanlogin"] is False and shell["rolpassword"] is None
    assert await _memberships("reorderos_app") == []
    pw = secrets.token_hex(32)
    try:
        await provision_reorderos_app(_DO_DSN, pw)
        attrs = await role_attributes(_DO_DSN, "reorderos_app", pw)
        assert check_contract(attrs, "reorderos_app") == []
    finally:
        await _disable_login("reorderos_app")


async def test_versioned_worker_roundtrip_via_do_admin() -> None:
    pw = secrets.token_hex(32)
    before = await _authid_row("service_worker")
    try:
        await provision_versioned_worker(_DO_DSN, "service_worker_v2", pw)
        attrs = await role_attributes(_DO_DSN, "service_worker_v2", pw)
        assert check_contract(attrs, "service_worker_v2") == []
        assert attrs["member_of"] == ["service_worker"]
        after = await _authid_row("service_worker")
        assert before == after, "provisioning v2 must not touch service_worker's credential"
    finally:
        await _su_exec("DROP ROLE IF EXISTS service_worker_v2")


# ── fail-closed dangerous-attribute gate ──────────────────────────────────────
async def test_fail_closed_zero_mutation_on_verify_only_attribute() -> None:
    """A role already carrying a verify-only dangerous attribute must REFUSE with
    ZERO mutation — by policy the tool never alters SUPERUSER/BYPASSRLS/REPLICATION
    (even where a clause would be legal for this admin), and silently preserving one
    is forbidden. The error names role + attributes only (no DSNs, no secrets)."""
    pw = secrets.token_hex(32)
    await provision_reorderos_app(_DO_DSN, pw)  # start from a clean, existing role
    await _su_exec("ALTER ROLE reorderos_app BYPASSRLS")
    before = await _authid_row("reorderos_app")
    before_members = await _memberships("reorderos_app")
    try:
        with pytest.raises(SystemExit) as exc:
            await provision_reorderos_app(_DO_DSN, secrets.token_hex(32))
        message = str(exc.value)
        assert "verify-only dangerous attribute" in message
        assert "rolbypassrls=True" in message and "reorderos_app" in message
        assert "postgresql://" not in message and "@" not in message
        assert pw not in message
        after = await _authid_row("reorderos_app")
        after_members = await _memberships("reorderos_app")
        assert before == after, "refusal must not change the role at all"
        assert before_members == after_members
    finally:
        await _su_exec("ALTER ROLE reorderos_app NOBYPASSRLS")
        await _disable_login("reorderos_app")


# ── real end-to-end atomicity through the provider-shaped admin ───────────────
async def test_create_rollback_leaves_no_role_via_do_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure AFTER CREATE must leave no new role: CREATE runs inside the one
    transaction now (the staging residue existed precisely because the old code created
    outside it)."""

    async def boom(conn_: object, role: str) -> None:
        raise RuntimeError("injected membership failure")

    monkeypatch.setattr(ra, "_normalize_memberships", boom)
    assert not await _role_exists("service_worker_v7")
    with pytest.raises(RuntimeError, match="injected membership failure"):
        await provision_versioned_worker(_DO_DSN, "service_worker_v7", secrets.token_hex(32))
    assert not await _role_exists("service_worker_v7"), (
        "failed fresh provisioning must roll back its CREATE — no inert shell"
    )


async def test_update_failure_preserves_previous_state_via_do_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure while UPDATING an existing role must leave password, LOGIN state,
    attributes, and memberships byte-identical (pg_authid-proven)."""
    pw = secrets.token_hex(32)
    await provision_reorderos_app(_DO_DSN, pw)
    before = await _authid_row("reorderos_app")
    before_members = await _memberships("reorderos_app")

    async def boom(conn_: object, role: str) -> None:
        raise RuntimeError("injected membership failure")

    monkeypatch.setattr(ra, "_normalize_memberships", boom)
    try:
        with pytest.raises(RuntimeError, match="injected membership failure"):
            await rotate_password(_DO_DSN, "reorderos_app", secrets.token_hex(32))
        assert await _authid_row("reorderos_app") == before
        assert await _memberships("reorderos_app") == before_members
    finally:
        monkeypatch.undo()
        await _disable_login("reorderos_app")


async def test_final_contract_assertion_rolls_back_via_do_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the FINAL in-transaction catalog assertion reports a violation, the whole
    mutation rolls back — the assertion is a gate, not a log line."""
    pw = secrets.token_hex(32)
    await provision_reorderos_app(_DO_DSN, pw)
    before = await _authid_row("reorderos_app")

    real_catalog = ra._catalog_attributes

    async def dirty_catalog(conn: object, role: str) -> dict:
        attrs = await real_catalog(conn, role)
        attrs["rolcreatedb"] = True  # simulate a contract violation at commit time
        return attrs

    monkeypatch.setattr(ra, "_catalog_attributes", dirty_catalog)
    try:
        with pytest.raises(SystemExit, match="post-mutation contract check failed"):
            await rotate_password(_DO_DSN, "reorderos_app", secrets.token_hex(32))
        monkeypatch.undo()
        assert await _authid_row("reorderos_app") == before
    finally:
        monkeypatch.undo()
        await _disable_login("reorderos_app")


# ── PG 16+ grantor-tracked membership semantics ───────────────────────────────
async def test_unrevocable_foreign_grant_fails_closed_via_do_admin() -> None:
    """PG 16+ tracks the GRANTOR per membership grant, and a non-superuser admin cannot
    revoke a superuser-made grant even with ADMIN OPTION. An unexpected membership the
    DO-shaped admin cannot remove must abort the WHOLE mutation (rolled back, zero
    change) rather than being silently left in place."""
    import asyncpg

    pw = secrets.token_hex(32)
    await provision_reorderos_app(_DO_DSN, pw)
    await _su_exec("GRANT service_worker TO reorderos_app")  # unexpected; grantor=superuser
    before = await _authid_row("reorderos_app")
    before_members = await _memberships("reorderos_app")
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await provision_reorderos_app(_DO_DSN, secrets.token_hex(32))
        assert await _authid_row("reorderos_app") == before
        assert await _memberships("reorderos_app") == before_members
    finally:
        await _su_exec("REVOKE service_worker FROM reorderos_app")
        await _disable_login("reorderos_app")


async def test_duplicate_grantor_membership_is_collapsed_by_capable_admin() -> None:
    """A duplicated expected membership (two grant rows under different grantors — the
    state the old unconditional re-grant used to CREATE) is collapsed to exactly one
    plain grant by an administrator that can revoke both rows; the final in-transaction
    contract assertion then passes."""
    import asyncpg

    pw = secrets.token_hex(32)
    await provision_reorderos_app(_DO_DSN, pw)
    # Force the mixed-grantor duplicate deterministically: remove every existing
    # app_user grant row (superuser can act for any grantor), then grant once as the
    # superuser and once as the DO-shaped admin.
    import asyncpg as _asyncpg

    su = await _asyncpg.connect(DB_URL_SYNC)
    try:
        grantors = await su.fetch(
            "SELECT g.rolname AS grantor FROM pg_auth_members am "
            "JOIN pg_roles r ON r.oid = am.roleid JOIN pg_roles g ON g.oid = am.grantor "
            "WHERE am.member = (SELECT oid FROM pg_roles WHERE rolname='reorderos_app') "
            "AND r.rolname = 'app_user'"
        )
        for row in grantors:
            await su.execute(f'REVOKE app_user FROM reorderos_app GRANTED BY "{row["grantor"]}"')
        await su.execute("GRANT app_user TO reorderos_app")
    finally:
        await su.close()
    do_conn = await asyncpg.connect(_DO_DSN)
    try:
        # second grant row for the SAME membership, grantor=do_admin_like
        await do_conn.execute("GRANT app_user TO reorderos_app")
    finally:
        await do_conn.close()
    assert len(await _memberships("reorderos_app")) == 2, "duplicate grant rows expected"
    pw2 = secrets.token_hex(32)
    try:
        await provision_reorderos_app(DB_URL_SYNC, pw2)  # superuser can revoke both rows
        assert await _memberships("reorderos_app") == [("app_user", False)]
        attrs = await role_attributes(DB_URL_SYNC, "reorderos_app", pw2)
        assert check_contract(attrs, "reorderos_app") == []
    finally:
        await _disable_login("reorderos_app")


# ── administrative-capability gate ────────────────────────────────────────────
async def test_rotate_refuses_without_admin_capability() -> None:
    """weak_admin_like holds CREATEROLE but NO ADMIN OPTION on service_worker — PG 16+
    would refuse its ALTER anyway, but the capability gate must refuse FIRST, by name,
    before any write. rolcreaterole=True alone is not capability."""
    before = await _authid_row("service_worker")
    with pytest.raises(SystemExit) as exc:
        await rotate_password(_WEAK_DSN, "service_worker", secrets.token_hex(32))
    message = str(exc.value)
    assert "capability check failed" in message
    assert "can_administer_target=False" in message
    assert "postgresql://" not in message and "@" not in message
    assert await _authid_row("service_worker") == before


async def test_provision_existing_refuses_without_admin_option_on_target() -> None:
    """An EXISTING role the administrator cannot administer refuses at the capability
    gate even on the provisioning path (allow_create does not bypass it)."""
    if not await _role_exists("reorderos_app"):
        pw = secrets.token_hex(32)
        await provision_reorderos_app(_DO_DSN, pw)
        await _disable_login("reorderos_app")
    with pytest.raises(SystemExit, match="capability check failed"):
        await provision_reorderos_app(_WEAK_DSN, secrets.token_hex(32))


def test_capability_cli_gates_and_reports_booleans_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`capability` is the runbook's read-only B1/B2 pre-gate: exit 0 with ADMIN
    CAPABILITY OK through the DO-shaped admin, exit 1 through the weak admin — and its
    output never carries credentials. (Sync test: main() owns its own asyncio.run.)"""
    monkeypatch.setenv("ADMIN_DATABASE_URL", _DO_DSN)
    rc = ra.main(["capability", "service_worker"])
    out = capsys.readouterr()
    assert rc == 0
    assert "capability service_worker: ADMIN CAPABILITY OK" in out.out
    assert "can_administer_target=True" in out.out
    for secret in (_DO_ADMIN_PW, _WEAK_ADMIN_PW):
        assert secret not in out.out and secret not in out.err
    monkeypatch.setenv("ADMIN_DATABASE_URL", _WEAK_DSN)
    rc = ra.main(["capability", "service_worker"])
    out = capsys.readouterr()
    assert rc == 1
    assert "INSUFFICIENT" in out.err
    for secret in (_DO_ADMIN_PW, _WEAK_ADMIN_PW):
        assert secret not in out.out and secret not in out.err


# ── CLI end-to-end through the provider-shaped admin, secret-free output ──────
def test_provision_and_prove_cli_via_do_admin_no_secret_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exact runbook B1 command sequence, through the DO-shaped admin: provision-app
    exit 0, prove exit 0 with CONTRACT OK — and neither the password nor any DSN
    fragment ever appears on stdout/stderr. (Sync test: main() owns its own
    asyncio.run.)"""
    pw = secrets.token_hex(32)
    monkeypatch.setenv("PW", pw)
    monkeypatch.setenv("ADMIN_DATABASE_URL", _DO_DSN)
    try:
        assert ra.main(["provision-app"]) == 0
        out = capsys.readouterr()
        assert "provisioned" in out.out
        assert ra.main(["prove", "reorderos_app"]) == 0
        out2 = capsys.readouterr()
        assert "prove reorderos_app: CONTRACT OK" in out2.out
        for chunk in (out.out, out.err, out2.out, out2.err):
            assert pw not in chunk
            assert _DO_ADMIN_PW not in chunk
            assert "postgresql://" not in chunk
            host = urlparse(DB_URL_SYNC).hostname or ""
            if host and host != "localhost":
                assert host not in chunk
    finally:
        os.environ.pop("PW", None)
        asyncio.run(_disable_login("reorderos_app"))
