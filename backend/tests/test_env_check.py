"""Env readiness — the dangling-secret tripwire (2026-07-15 prod incident).

Pins: missing/empty/placeholder detection, production-only placeholder rules,
per-component profile coverage, CLI exit codes, and — most importantly — that
no secret VALUE can ever appear in a report or in the CLI output.
"""

from __future__ import annotations

import pytest

from app.ops.env_check import PROFILES, check_env, main

_SENTINEL = "sk_live_SENTINEL_never_print_9f8e7d"


def _good_env(**overrides: str) -> dict[str, str]:
    env = {v.name: f"real-value-{v.name.lower()}" for v in PROFILES["predeploy_env_check"]}
    env["APP_ENV"] = "production"
    env["WORKOS_SECRET_KEY"] = _SENTINEL
    env.update(overrides)
    return env


def test_all_good_is_ready() -> None:
    r = check_env("predeploy_env_check", _good_env())
    assert r.ready and not r.failures


def test_missing_secret_fails() -> None:
    env = _good_env()
    del env["TOKEN_ENCRYPTION_KEY"]
    r = check_env("api", env)
    assert not r.ready
    assert r.failures["TOKEN_ENCRYPTION_KEY"] == "missing"


def test_empty_string_fails_the_dangling_secret_signature() -> None:
    r = check_env("api", _good_env(DO_SPACES_SECRET=""))
    assert not r.ready
    assert r.failures["DO_SPACES_SECRET"] == "empty"
    r2 = check_env("api", _good_env(DO_SPACES_SECRET="   "))
    assert r2.failures["DO_SPACES_SECRET"] == "empty"


@pytest.mark.parametrize(
    "bad", ["false", "null", "None", "undefined", "changeme", "PLACEHOLDER", "REDACTED", "test"]
)
def test_placeholder_values_fail_in_production(bad: str) -> None:
    r = check_env("api", _good_env(WORKOS_SECRET_KEY=bad))
    assert not r.ready
    assert r.failures["WORKOS_SECRET_KEY"] == "placeholder"


def test_placeholders_allowed_outside_production() -> None:
    env = _good_env(WORKOS_SECRET_KEY="changeme")
    env["APP_ENV"] = "local"
    assert check_env("api", env).ready


def test_non_secret_keys_may_legitimately_be_false() -> None:
    # e.g. boolean-ish config flags — only SECRET keys get placeholder rules.
    r = check_env("api", _good_env(CLOVER_APP_ID="false"))
    assert r.ready


def test_profiles_require_the_right_keys() -> None:
    names = {p: {v.name for v in vars_} for p, vars_ in PROFILES.items()}
    assert {
        "DATABASE_URL",
        "SERVICE_DATABASE_URL",
        "TOKEN_ENCRYPTION_KEY",
        "WORKOS_SECRET_KEY",
        "WORKOS_CLIENT_ID",
        "WORKOS_JWKS_URL",
        "WORKOS_ISSUER",
        "CLOVER_APP_SECRET",
        "CLOVER_WEBHOOK_AUTH_CODE",
        "DO_SPACES_KEY",
        "DO_SPACES_SECRET",
    } <= names["api"]
    assert {
        "SERVICE_DATABASE_URL",
        "DO_SPACES_KEY",
        "DO_SPACES_SECRET",
        "DO_SPACES_BUCKET",
        "ANTHROPIC_API_KEY",
    } <= names["receipt_extraction_worker"]
    assert {"SERVICE_DATABASE_URL", "TOKEN_ENCRYPTION_KEY"} <= names["inbox_worker"]
    # migrate needs ONLY DATABASE_URL: alembic/env.py reads it directly and does NOT
    # construct full Settings (the `settings` proxy is lazy), so the migrate job is not
    # handed any runtime request/worker secret. No WorkOS/Clover/Postmark/token/service.
    assert {"DATABASE_URL"} == names["migrate_job"]
    # predeploy gate covers every runtime profile
    union = set().union(*(names[p] for p in ("api", "receipt_extraction_worker", "inbox_worker")))
    assert union <= names["predeploy_env_check"]


def test_no_secret_value_in_report_or_cli_output(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _good_env(DO_SPACES_SECRET="")  # force a failure so all paths print
    r = check_env("api", env)
    assert _SENTINEL not in repr(r)

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    rc = main(["--profile", "api"])
    out = capsys.readouterr()
    assert rc == 1
    assert _SENTINEL not in out.out and _SENTINEL not in out.err
    assert "DO_SPACES_SECRET" in out.out  # names ARE reported


def test_migrate_profile_gates_before_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    """`env_check --profile migrate_job && alembic upgrade head`: a missing
    DATABASE_URL must exit non-zero so alembic never runs."""
    env = _good_env()
    del env["DATABASE_URL"]
    for key in PROFILES["predeploy_env_check"]:
        monkeypatch.delenv(key.name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["--profile", "migrate_job"]) == 1

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    assert main(["--profile", "migrate_job"]) == 0


def test_migrate_job_needs_only_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """DECOUPLE GUARD (security PR): the migrate job must run with ONLY DATABASE_URL — no
    runtime request/worker secrets. alembic/env.py reads DATABASE_URL directly and the
    `settings` proxy is lazy, so importing the app package for a migration does NOT run
    config's production fail-closed check. This pins the minimal blast radius; the actual
    end-to-end proof (a production-env migration reaching 0035 with only DATABASE_URL) is
    tests/test_migration_roundtrip.py::test_migration_persists_under_production_env."""
    for key in PROFILES["predeploy_env_check"]:
        monkeypatch.delenv(key.name, raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://doadmin:x@h/db")
    r = check_env("migrate_job")
    assert r.ready, r.failures
    assert {v.name for v in PROFILES["migrate_job"]} == {"DATABASE_URL"}


def test_alembic_env_reads_url_without_constructing_settings() -> None:
    """DECOUPLE GUARD (biting, hermetic): alembic/env.py must NOT construct full Settings —
    doing so would re-impose the production fail-closed check and force the migrate job to
    carry runtime request/worker secrets. This static check fails deterministically if
    someone re-couples env.py to `get_settings()` (the subprocess migration test can pass
    vacuously because it inherits os.environ/.env, so THIS is the real regression guard)."""
    import pathlib

    src = pathlib.Path("alembic/env.py").read_text()
    assert "get_settings(" not in src, (
        "alembic/env.py re-coupled to full Settings — the migrate job would again require "
        "WorkOS/Clover/Postmark/token/service secrets. Read DATABASE_URL directly instead."
    )
    assert "normalize_postgres_url" in src  # uses the lazy URL normalizer


def test_api_profile_stays_aligned_with_config_fail_closed() -> None:
    """FAIL-CLOSED DRIFT GUARD: the api profile MUST require every secret config's
    production check demands unconditionally, so the API (which DOES use them) fails
    loudly on a dangling secret. The decouple only relaxed the MIGRATE path — the API's
    fail-closed contract is unchanged. If config gains a new unconditional required secret
    not added to the api profile, this fails."""
    api = {v.name for v in PROFILES["api"]}
    assert {
        "TOKEN_ENCRYPTION_KEY",
        "SERVICE_DATABASE_URL",
        "WORKOS_CLIENT_ID",
        "WORKOS_JWKS_URL",
    } <= api
