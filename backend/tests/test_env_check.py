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
    # migrate needs DATABASE_URL plus the Settings fail-closed four (alembic
    # env.py loads full Settings).
    assert {
        "DATABASE_URL",
        "TOKEN_ENCRYPTION_KEY",
        "SERVICE_DATABASE_URL",
        "CLOVER_APP_SECRET",
        "CLOVER_WEBHOOK_AUTH_CODE",
    } == names["migrate_job"]
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
