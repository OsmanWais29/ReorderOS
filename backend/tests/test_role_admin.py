"""Unit tests for scripts.role_admin — the asyncpg role provisioner the runbook calls
(the runtime image has no psql). Pure hex-validator tests + a localhost-guarded
provision→prove round-trip against the compose DB.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlparse

import pytest

from scripts.role_admin import (
    check_contract,
    expected_memberships,
    is_managed,
    provision_reorderos_app,
    provision_versioned_worker,
    role_attributes,
    rotate_password,
    rotation_preflight,
    validate_pw,
)
from tests.conftest import DB_URL_SYNC

pytestmark = pytest.mark.integration


def _is_local(dsn: str) -> bool:
    host = (urlparse(dsn.replace("postgresql+asyncpg://", "postgresql://")).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", ""}


# ── the injection guard ───────────────────────────────────────────────────────
def test_validate_pw_accepts_64_hex() -> None:
    pw = secrets.token_hex(32)  # 64 lowercase hex chars
    assert validate_pw(pw) == pw


@pytest.mark.parametrize(
    "bad",
    ["", None, "short", "A" * 64, "g" * 64, "0" * 63, "0" * 65, "'; DROP ROLE x;--" + "0" * 47],
)
def test_validate_pw_rejects_bad(bad: object) -> None:
    with pytest.raises(SystemExit):
        validate_pw(bad)  # type: ignore[arg-type]


# Every dangerous attribute the runbook gate asserts (all must be False after
# provision/rotate) plus the intended-True ones.
_MUST_BE_FALSE = ("rolsuper", "rolbypassrls", "rolcreatedb", "rolcreaterole", "rolreplication")


# The EXACT direct-membership contract the runbook gate asserts.
_EXPECTED_MEMBER_OF = {"reorderos_app": ["app_user"], "service_worker": []}


def _assert_hardened(attrs: dict, *, role: str, app_user_member: bool) -> None:
    assert attrs["role"] == role
    assert attrs["rolcanlogin"] is True
    assert attrs["rolinherit"] is True
    assert attrs["app_user_member"] is app_user_member
    for attribute in _MUST_BE_FALSE:
        assert attrs[attribute] is False, f"{role}: {attribute} not disabled"
    # EXACT membership set (review finding 5): nothing beyond the contract, and never
    # with ADMIN OPTION.
    assert attrs["member_of"] == _EXPECTED_MEMBER_OF[role], attrs["member_of"]
    assert attrs["admin_option_anywhere"] is False


async def _disable_login(role: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(DB_URL_SYNC)
    try:
        await conn.execute(f"ALTER ROLE {role} NOLOGIN")
        await conn.execute(f"ALTER ROLE {role} PASSWORD '{secrets.token_hex(32)}'")
    finally:
        await conn.close()


# ── provision → prove round-trip (LOCAL only) ─────────────────────────────────
async def test_provision_and_prove_roundtrip() -> None:
    if not _is_local(DB_URL_SYNC):
        pytest.skip("mutates roles; only against a LOCAL database")
    pw = secrets.token_hex(32)
    await provision_reorderos_app(DB_URL_SYNC, pw)
    try:
        attrs = await role_attributes(DB_URL_SYNC, "reorderos_app", pw)
        _assert_hardened(attrs, role="reorderos_app", app_user_member=True)
    finally:
        await _disable_login("reorderos_app")  # leave no usable login behind


async def test_provision_fails_closed_on_verify_only_attributes() -> None:
    """CORRECTION ROUND (staging B1, 2026-08-17): SUPERUSER / BYPASSRLS / REPLICATION
    are VERIFY-ONLY by policy (SUPERUSER changes always need a true superuser;
    BYPASSRLS/REPLICATION need an administrator that itself holds the attribute — not
    assumed portable across managed providers), so a pre-existing role carrying one
    must REFUSE fail-closed with ZERO mutation (never silently preserved, never
    partially changed), even when the connected administrator happens to be a true
    superuser: remediation is a deliberate manual act, not an automatic side effect."""
    if not _is_local(DB_URL_SYNC):
        pytest.skip("mutates roles; only against a LOCAL database")
    pw = secrets.token_hex(32)
    await provision_reorderos_app(DB_URL_SYNC, pw)  # ensure the role exists, clean
    await _admin_execute(
        "ALTER ROLE reorderos_app SUPERUSER BYPASSRLS CREATEDB CREATEROLE REPLICATION"
    )
    import asyncpg

    conn = await asyncpg.connect(DB_URL_SYNC)
    try:
        before = await conn.fetchrow(
            "SELECT rolpassword, rolcanlogin, rolsuper, rolbypassrls, rolcreatedb,"
            " rolcreaterole, rolreplication FROM pg_authid WHERE rolname='reorderos_app'"
        )
    finally:
        await conn.close()
    try:
        with pytest.raises(SystemExit) as exc:
            await provision_reorderos_app(DB_URL_SYNC, secrets.token_hex(32))
        message = str(exc.value)
        assert "verify-only dangerous attribute" in message
        for fragment in ("rolsuper=True", "rolbypassrls=True", "rolreplication=True"):
            assert fragment in message
        assert "postgresql://" not in message and "@" not in message
        conn = await asyncpg.connect(DB_URL_SYNC)
        try:
            after = await conn.fetchrow(
                "SELECT rolpassword, rolcanlogin, rolsuper, rolbypassrls, rolcreatedb,"
                " rolcreaterole, rolreplication FROM pg_authid WHERE rolname='reorderos_app'"
            )
        finally:
            await conn.close()
        assert dict(before) == dict(after), "refusal must not change ANYTHING, mutable or not"
    finally:
        await _admin_execute("ALTER ROLE reorderos_app NOSUPERUSER NOBYPASSRLS NOREPLICATION")
    # Mutable dirt (CREATEDB/CREATEROLE, still set from above) IS normalized by a rerun:
    pw2 = secrets.token_hex(32)
    try:
        await provision_reorderos_app(DB_URL_SYNC, pw2)
        attrs = await role_attributes(DB_URL_SYNC, "reorderos_app", pw2)
        _assert_hardened(attrs, role="reorderos_app", app_user_member=True)
    finally:
        await _disable_login("reorderos_app")


async def test_rotate_refuses_missing_role() -> None:
    """CORRECTION ROUND: rotation never creates — a managed-but-absent role is refused
    by name, and nothing is created."""
    if not _is_local(DB_URL_SYNC):
        pytest.skip("mutates roles; only against a LOCAL database")
    assert not await _role_exists("service_worker_v9")
    with pytest.raises(SystemExit, match="does not exist"):
        await rotate_password(DB_URL_SYNC, "service_worker_v9", secrets.token_hex(32))
    assert not await _role_exists("service_worker_v9")


async def test_rotate_also_normalizes_service_worker_attributes() -> None:
    """Password rotation must never leave a managed role over-privileged: grant
    service_worker CREATEDB + CREATEROLE, rotate, prove every dangerous attribute is
    back off (and the new password actually works — prove connects AS the role)."""
    if not _is_local(DB_URL_SYNC):
        pytest.skip("mutates roles; only against a LOCAL database")
    import asyncpg

    conn = await asyncpg.connect(DB_URL_SYNC)
    try:
        old_password_row = await conn.fetchrow(
            "SELECT rolcanlogin FROM pg_roles WHERE rolname='service_worker'"
        )
        assert old_password_row is not None, "service_worker must exist (migration 0006)"
        await conn.execute("ALTER ROLE service_worker CREATEDB CREATEROLE")
    finally:
        await conn.close()
    pw = secrets.token_hex(32)
    try:
        await rotate_password(DB_URL_SYNC, "service_worker", pw)
        attrs = await role_attributes(DB_URL_SYNC, "service_worker", pw)
        _assert_hardened(attrs, role="service_worker", app_user_member=False)
    finally:
        # restore the local test password so the rest of the suite's service pool works
        conn = await asyncpg.connect(DB_URL_SYNC)
        try:
            await conn.execute("ALTER ROLE service_worker PASSWORD 'service_worker'")
        finally:
            await conn.close()


async def test_role_attributes_refuses_non_managed_role() -> None:
    with pytest.raises(SystemExit):
        await role_attributes(DB_URL_SYNC, "doadmin", secrets.token_hex(32))


# ── exact membership normalization (review finding 5) ────────────────────────
async def test_provision_strips_unexpected_membership_and_admin_option() -> None:
    """A pre-existing reorderos_app holding an UNEXPECTED privileged membership
    (service_worker here — cross-pool privileges) and ADMIN OPTION on its allowed
    app_user membership must come out of provisioning with EXACTLY {app_user}, plain."""
    if not _is_local(DB_URL_SYNC):
        pytest.skip("mutates roles; only against a LOCAL database")
    import asyncpg

    conn = await asyncpg.connect(DB_URL_SYNC)
    try:
        await conn.execute("GRANT service_worker TO reorderos_app")
        await conn.execute("REVOKE app_user FROM reorderos_app")
        await conn.execute("GRANT app_user TO reorderos_app WITH ADMIN OPTION")
    finally:
        await conn.close()
    pw = secrets.token_hex(32)
    try:
        await provision_reorderos_app(DB_URL_SYNC, pw)
        attrs = await role_attributes(DB_URL_SYNC, "reorderos_app", pw)
        _assert_hardened(attrs, role="reorderos_app", app_user_member=True)
        assert attrs["member_of"] == ["app_user"]  # service_worker membership revoked
        assert attrs["admin_option_anywhere"] is False  # admin option stripped
    finally:
        await _disable_login("reorderos_app")


async def test_rotate_strips_unexpected_membership_from_service_worker() -> None:
    """service_worker's contract is the EMPTY membership set — a stray GRANT (app_user
    here, which would leak request-path privileges into the worker pool) must be revoked
    by rotation."""
    if not _is_local(DB_URL_SYNC):
        pytest.skip("mutates roles; only against a LOCAL database")
    import asyncpg

    conn = await asyncpg.connect(DB_URL_SYNC)
    try:
        await conn.execute("GRANT app_user TO service_worker")
    finally:
        await conn.close()
    pw = secrets.token_hex(32)
    try:
        await rotate_password(DB_URL_SYNC, "service_worker", pw)
        attrs = await role_attributes(DB_URL_SYNC, "service_worker", pw)
        _assert_hardened(attrs, role="service_worker", app_user_member=False)
        assert attrs["member_of"] == []
    finally:
        conn = await asyncpg.connect(DB_URL_SYNC)
        try:
            await conn.execute("ALTER ROLE service_worker PASSWORD 'service_worker'")
        finally:
            await conn.close()


# ── rotation-safety preflight (review finding 1) ──────────────────────────────
def test_preflight_reports_role_not_in_use() -> None:
    env = {
        "DATABASE_URL": "postgresql+asyncpg://doadmin:pw@db.internal:25060/app",
        "SERVICE_DATABASE_URL": "postgresql://doadmin:pw@db.internal:25060/app",
    }
    usage = rotation_preflight("service_worker", env)
    assert usage == {"DATABASE_URL": False, "SERVICE_DATABASE_URL": False}


def test_preflight_detects_live_use_of_the_role() -> None:
    """BITING (review finding 1): if the RUNNING deployment authenticates as the role,
    rotation would strand its pools AND poison the rollback spec — the preflight must
    say so (and the runbook then forbids in-place rotation, directing to Phase B-alt)."""
    env = {
        "DATABASE_URL": "postgresql://doadmin:pw@db.internal:25060/app",
        "SERVICE_DATABASE_URL": "postgresql+asyncpg://service_worker:pw@db.internal:25060/app",
    }
    usage = rotation_preflight("service_worker", env)
    assert usage["SERVICE_DATABASE_URL"] is True and usage["DATABASE_URL"] is False


def test_preflight_never_exposes_credentials(capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.role_admin import main as role_admin_main

    env = {
        "DATABASE_URL": "postgresql://doadmin:sekret-pw@db.internal:25060/app",
        "SERVICE_DATABASE_URL": "postgresql://service_worker:sekret-pw@db.internal:25060/app",
    }
    import os as _os

    old = {k: _os.environ.get(k) for k in env}
    _os.environ.update(env)
    try:
        rc = role_admin_main(["preflight-rotate", "service_worker"])
    finally:
        for k, v in old.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v
    captured = capsys.readouterr()
    assert rc == 1  # in use → non-zero
    assert "sekret-pw" not in captured.out and "sekret-pw" not in captured.err
    assert "db.internal" not in captured.out and "db.internal" not in captured.err


# ── contract validation in code (review round 4, finding 3) ───────────────────
def test_check_contract_flags_every_violation_class() -> None:
    """`prove`'s gate is its exit code, driven by this pure check — one violation per
    broken field, none for a clean contract."""
    clean = {
        "role": "reorderos_app",
        "rolcanlogin": True,
        "rolsuper": False,
        "rolbypassrls": False,
        "rolcreatedb": False,
        "rolcreaterole": False,
        "rolreplication": False,
        "rolinherit": True,
        "app_user_member": True,
        "member_of": ["app_user"],
        "admin_option_anywhere": False,
    }
    assert check_contract(clean, "reorderos_app") == []
    dirty = dict(clean)
    dirty.update(
        rolsuper=True,
        rolcreatedb=True,
        member_of=["app_user", "service_worker"],
        admin_option_anywhere=True,
    )
    violations = check_contract(dirty, "reorderos_app")
    assert any("rolsuper" in v for v in violations)
    assert any("rolcreatedb" in v for v in violations)
    assert any("member_of" in v for v in violations)
    assert any("admin_option" in v for v in violations)


def test_prove_exits_nonzero_on_contract_violation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """END-TO-END prove gate: grant a dangerous attribute directly (bypassing the
    normalizing tools), run `prove` via main() — it must print the violation and return
    1; after a normalizing rotate it returns 0 with CONTRACT OK. (Sync test: main()
    owns its own asyncio.run loop.)"""
    if not _is_local(DB_URL_SYNC):
        pytest.skip("mutates roles; only against a LOCAL database")
    import asyncio

    import asyncpg

    from scripts.role_admin import main as role_admin_main

    async def _admin_exec(*statements: str) -> None:
        conn = await asyncpg.connect(DB_URL_SYNC)
        try:
            for statement in statements:
                await conn.execute(statement)
        finally:
            await conn.close()

    pw = secrets.token_hex(32)
    asyncio.run(rotate_password(DB_URL_SYNC, "service_worker", pw))
    asyncio.run(_admin_exec("ALTER ROLE service_worker CREATEDB"))
    monkeypatch.setenv("PW", pw)
    monkeypatch.setenv("ADMIN_DATABASE_URL", DB_URL_SYNC)
    try:
        rc = role_admin_main(["prove", "service_worker"])
        out = capsys.readouterr()
        assert rc == 1, "prove must FAIL on a contract violation, not just print"
        assert "CONTRACT VIOLATION" in out.err and "rolcreatedb" in out.err
        # normalize and prove again — clean:
        assert role_admin_main(["rotate", "service_worker"]) == 0
        rc3 = role_admin_main(["prove", "service_worker"])
        out3 = capsys.readouterr()
        assert rc3 == 0 and "CONTRACT OK" in out3.out
    finally:
        asyncio.run(
            _admin_exec(
                "ALTER ROLE service_worker NOCREATEDB",
                "ALTER ROLE service_worker PASSWORD 'service_worker'",
            )
        )


async def test_role_mutation_is_atomic_when_membership_step_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILURE-INJECT (review round 4, finding 3): if membership normalization raises,
    the WHOLE mutation must roll back — the new password must NOT work and a
    pre-existing dangerous attribute must survive untouched (no partially-changed
    login role)."""
    if not _is_local(DB_URL_SYNC):
        pytest.skip("mutates roles; only against a LOCAL database")
    import asyncpg

    import scripts.role_admin as ra

    conn = await asyncpg.connect(DB_URL_SYNC)
    try:
        await conn.execute("ALTER ROLE service_worker CREATEDB")
    finally:
        await conn.close()

    async def boom(conn_: object, role: str) -> None:
        raise RuntimeError("injected membership failure")

    monkeypatch.setattr(ra, "_normalize_memberships", boom)
    # capture the CURRENT password hash (superuser-readable pg_authid; local-only test)
    conn = await asyncpg.connect(DB_URL_SYNC)
    try:
        hash_before = await conn.fetchval(
            "SELECT rolpassword FROM pg_authid WHERE rolname='service_worker'"
        )
    finally:
        await conn.close()
    new_pw = secrets.token_hex(32)
    try:
        with pytest.raises(RuntimeError, match="injected membership failure"):
            await rotate_password(DB_URL_SYNC, "service_worker", new_pw)
        conn = await asyncpg.connect(DB_URL_SYNC)
        try:
            createdb = await conn.fetchval(
                "SELECT rolcreatedb FROM pg_roles WHERE rolname='service_worker'"
            )
            hash_after = await conn.fetchval(
                "SELECT rolpassword FROM pg_authid WHERE rolname='service_worker'"
            )
        finally:
            await conn.close()
        assert createdb is True, "ALTER before the failure must have rolled back"
        # Password comparison must be catalog-based: local pg_hba trust-auth would make a
        # connect-with-new-password check vacuously succeed.
        assert hash_after == hash_before, "PASSWORD change must have rolled back with the tx"
    finally:
        monkeypatch.undo()
        conn = await asyncpg.connect(DB_URL_SYNC)
        try:
            await conn.execute("ALTER ROLE service_worker NOCREATEDB")
            await conn.execute("ALTER ROLE service_worker PASSWORD 'service_worker'")
        finally:
            await conn.close()


# ── versioned replacement worker (review round 4, finding 4 / Phase B-alt) ────
def test_versioned_worker_names_and_memberships() -> None:
    assert is_managed("service_worker_v2") and is_managed("service_worker_v13")
    assert not is_managed("service_worker_evil") and not is_managed("doadmin")
    assert expected_memberships("service_worker_v2") == frozenset({"service_worker"})
    with pytest.raises(SystemExit):
        expected_memberships("doadmin")


async def test_provision_versioned_worker_roundtrip() -> None:
    """B-alt's replacement role: hardened, LOGIN, member of service_worker (inheriting
    its grants/policies), contract-clean — and the OLD role's password is untouched."""
    if not _is_local(DB_URL_SYNC):
        pytest.skip("mutates roles; only against a LOCAL database")
    import asyncpg

    with pytest.raises(SystemExit):
        await provision_versioned_worker(DB_URL_SYNC, "not_a_versioned_name", "0" * 64)
    pw = secrets.token_hex(32)
    try:
        # capture old role's password hash first — B-alt's core promise is it is untouched
        conn = await asyncpg.connect(DB_URL_SYNC)
        try:
            old_hash = await conn.fetchval(
                "SELECT rolpassword FROM pg_authid WHERE rolname='service_worker'"
            )
        finally:
            await conn.close()
        await provision_versioned_worker(DB_URL_SYNC, "service_worker_v2", pw)
        attrs = await role_attributes(DB_URL_SYNC, "service_worker_v2", pw)
        assert check_contract(attrs, "service_worker_v2") == []
        assert attrs["member_of"] == ["service_worker"]
        conn = await asyncpg.connect(DB_URL_SYNC)
        try:
            new_hash = await conn.fetchval(
                "SELECT rolpassword FROM pg_authid WHERE rolname='service_worker'"
            )
        finally:
            await conn.close()
        assert new_hash == old_hash, "provisioning v2 must not touch service_worker's credential"
    finally:
        conn2 = await asyncpg.connect(DB_URL_SYNC)
        try:
            await conn2.execute("DROP ROLE IF EXISTS service_worker_v2")
        finally:
            await conn2.close()


# ── parent-contract validation placement + behavior (round 5, finding 1) ──────
def test_parent_check_called_by_versioned_worker_not_by_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLACEMENT (the round-5 P1): the service_worker parent-contract check belongs to
    provision_versioned_worker ONLY. provision_reorderos_app must be fully independent
    of service_worker; the versioned path must consult the check BEFORE any DDL."""
    if not _is_local(DB_URL_SYNC):
        pytest.skip("mutates roles; only against a LOCAL database")
    import asyncio

    import scripts.role_admin as ra

    calls: list[str] = []

    async def recorder(conn: object) -> None:
        calls.append("parent-check")
        raise SystemExit("recorded — refusing before any DDL")

    monkeypatch.setattr(ra, "_assert_parent_worker_contract", recorder)
    pw = secrets.token_hex(32)
    # reorderos_app path: must complete WITHOUT ever invoking the parent check
    try:
        asyncio.run(provision_reorderos_app(DB_URL_SYNC, pw))
    finally:
        asyncio.run(_async_disable_login("reorderos_app"))
    assert calls == [], "provision_reorderos_app must not validate service_worker"
    # versioned path: must invoke it (and our recorder aborts before any DDL)
    with pytest.raises(SystemExit, match="recorded"):
        asyncio.run(provision_versioned_worker(DB_URL_SYNC, "service_worker_v2", pw))
    assert calls == ["parent-check"]
    assert not asyncio.run(_role_exists("service_worker_v2")), (
        "a refusal must leave no newly created versioned role"
    )


async def _role_exists(role: str) -> bool:
    import asyncpg

    conn = await asyncpg.connect(DB_URL_SYNC)
    try:
        return bool(await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role))
    finally:
        await conn.close()


async def _async_disable_login(role: str) -> None:
    await _disable_login(role)


async def _admin_execute(*statements: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(DB_URL_SYNC)
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


@pytest.mark.parametrize(
    ("setup", "teardown", "expected_fragment"),
    [
        (
            ("ALTER ROLE service_worker CREATEDB",),
            ("ALTER ROLE service_worker NOCREATEDB",),
            "rolcreatedb",
        ),
        (
            ("ALTER ROLE service_worker BYPASSRLS",),
            ("ALTER ROLE service_worker NOBYPASSRLS",),
            "rolbypassrls",
        ),
        (
            ("GRANT app_user TO service_worker",),
            ("REVOKE app_user FROM service_worker",),
            "app_user_member",  # direct membership → also the pool-crossing hazard
        ),
        (
            ("GRANT app_user TO service_worker WITH ADMIN OPTION",),
            ("REVOKE app_user FROM service_worker",),
            "admin_option",
        ),
    ],
)
async def test_versioned_worker_refuses_dirty_parent(
    setup: tuple[str, ...], teardown: tuple[str, ...], expected_fragment: str
) -> None:
    """Every dirty-parent class refuses BEFORE creating the child: dangerous attribute,
    unexpected direct membership, ADMIN OPTION, app_user membership. No versioned role
    is created; error names roles/attributes only."""
    if not _is_local(DB_URL_SYNC):
        pytest.skip("mutates roles; only against a LOCAL database")
    await _admin_execute(*setup)
    try:
        with pytest.raises(SystemExit) as exc:
            await provision_versioned_worker(
                DB_URL_SYNC, "service_worker_v2", secrets.token_hex(32)
            )
        message = str(exc.value)
        assert "parent service_worker contract is not clean" in message
        assert expected_fragment in message
        assert "postgresql://" not in message and "@" not in message  # names only
        assert not await _role_exists("service_worker_v2")
    finally:
        await _admin_execute(*teardown)


async def test_versioned_worker_refuses_indirect_app_user_membership() -> None:
    """INDIRECT membership of app_user (via an intermediate group) must also refuse —
    pg_has_role covers the transitive closure."""
    if not _is_local(DB_URL_SYNC):
        pytest.skip("mutates roles; only against a LOCAL database")
    await _admin_execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='tmp_bridge_grp') "
        "THEN CREATE ROLE tmp_bridge_grp NOLOGIN; END IF; END $$;",
        "GRANT app_user TO tmp_bridge_grp",
        "GRANT tmp_bridge_grp TO service_worker",
    )
    try:
        with pytest.raises(SystemExit) as exc:
            await provision_versioned_worker(
                DB_URL_SYNC, "service_worker_v2", secrets.token_hex(32)
            )
        assert "app_user_member" in str(exc.value)
        assert not await _role_exists("service_worker_v2")
    finally:
        await _admin_execute(
            "REVOKE tmp_bridge_grp FROM service_worker",
            "DROP ROLE IF EXISTS tmp_bridge_grp",
        )


async def test_versioned_worker_refuses_missing_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing parent refuses (service_worker cannot be dropped locally — it owns
    grants — so absence is simulated at the catalog-read seam)."""
    import scripts.role_admin as ra

    async def empty_catalog(conn: object, role: str) -> dict:
        return {}

    monkeypatch.setattr(ra, "_catalog_attributes", empty_catalog)
    with pytest.raises(SystemExit, match="service_worker does not exist"):
        await provision_versioned_worker(DB_URL_SYNC, "service_worker_v2", secrets.token_hex(32))


async def test_versioned_worker_dirty_parent_leaves_existing_child_unchanged() -> None:
    """A refusal must not change an EXISTING versioned role either: attrs and the
    pg_authid password hash are byte-identical before/after the refused rerun."""
    if not _is_local(DB_URL_SYNC):
        pytest.skip("mutates roles; only against a LOCAL database")
    import asyncpg

    pw = secrets.token_hex(32)
    await provision_versioned_worker(DB_URL_SYNC, "service_worker_v2", pw)
    try:
        conn = await asyncpg.connect(DB_URL_SYNC)
        try:
            before = await conn.fetchrow(
                "SELECT rolpassword, rolcanlogin, rolcreatedb FROM pg_authid "
                "WHERE rolname='service_worker_v2'"
            )
        finally:
            await conn.close()
        await _admin_execute("ALTER ROLE service_worker CREATEDB")
        try:
            with pytest.raises(SystemExit, match="not clean"):
                await provision_versioned_worker(
                    DB_URL_SYNC, "service_worker_v2", secrets.token_hex(32)
                )
        finally:
            await _admin_execute("ALTER ROLE service_worker NOCREATEDB")
        conn = await asyncpg.connect(DB_URL_SYNC)
        try:
            after = await conn.fetchrow(
                "SELECT rolpassword, rolcanlogin, rolcreatedb FROM pg_authid "
                "WHERE rolname='service_worker_v2'"
            )
        finally:
            await conn.close()
        assert dict(before) == dict(after), "refusal must not touch the existing child"
    finally:
        await _admin_execute("DROP ROLE IF EXISTS service_worker_v2")


async def test_versioned_worker_clean_parent_provisions() -> None:
    if not _is_local(DB_URL_SYNC):
        pytest.skip("mutates roles; only against a LOCAL database")
    pw = secrets.token_hex(32)
    try:
        await provision_versioned_worker(DB_URL_SYNC, "service_worker_v2", pw)
        attrs = await role_attributes(DB_URL_SYNC, "service_worker_v2", pw)
        assert check_contract(attrs, "service_worker_v2") == []
    finally:
        await _admin_execute("DROP ROLE IF EXISTS service_worker_v2")
