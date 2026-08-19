"""Unit tests for scripts.deploy_verify — the deploy-spec + log verifier the runbook calls.

Covers SOURCE_COMMIT bindings (platform placeholder, component-level, no literal/app-level),
COMPLETE worker presence (Clover pair, Postmark, receipt-extraction — both directions), the
committed-spec secret/duplicate rules, JSON worker-log parsing (last VALID record wins), and
deterministic deployment-id selection. Also asserts the REAL repo specs pass the COMPLETE
validator — both of them, production included (no partial validation)."""

from __future__ import annotations

import os
import pathlib
from typing import Any

import pytest

from scripts.deploy_verify import (
    parse_starting_commit,
    select_new_deployment,
    validate_no_duplicate_envs,
    validate_no_secret_values,
    validate_source_commit_bindings,
    validate_spec,
    validate_worker_presence,
)

_PLACEHOLDER = "${_self.COMMIT_HASH}"
_DO_DIR = os.path.join(os.path.dirname(__file__), "..", "..", ".do")


def _svc(name: str, source_commit: str | None) -> dict:
    envs = []
    if source_commit is not None:
        envs.append({"key": "SOURCE_COMMIT", "value": source_commit})
    return {"name": name, "envs": envs}


# ── SOURCE_COMMIT bindings ────────────────────────────────────────────────────
def test_source_commit_placeholder_passes() -> None:
    spec = {
        "services": [_svc("api", _PLACEHOLDER)],
        "workers": [_svc("inbox-worker", _PLACEHOLDER)],
    }
    assert validate_source_commit_bindings(spec) == []


def test_literal_sha_fails() -> None:
    spec = {"services": [_svc("api", "a1b2c3d4e5f6")], "workers": []}
    errs = validate_source_commit_bindings(spec)
    assert any("must be" in e and "api" in e for e in errs)


def test_missing_source_commit_fails() -> None:
    spec = {"services": [_svc("api", None)], "workers": []}
    assert any("missing" in e for e in validate_source_commit_bindings(spec))


def test_app_level_source_commit_fails() -> None:
    spec = {
        "envs": [{"key": "SOURCE_COMMIT", "value": _PLACEHOLDER}],
        "services": [_svc("api", _PLACEHOLDER)],
        "workers": [],
    }
    assert any("app-level" in e for e in validate_source_commit_bindings(spec))


# ── worker presence vs flags (complete feature set) ───────────────────────────
def _flagged(
    flags: dict[str, str], workers: list[str], extra_envs: list[dict] | None = None
) -> dict:
    return {
        "envs": [{"key": k, "value": v} for k, v in flags.items()] + (extra_envs or []),
        "workers": [{"name": w, "envs": []} for w in workers],
    }


def test_clover_pair_present_passes() -> None:
    spec = _flagged({"CLOVER_ENABLED": "true"}, ["inbox-worker", "reconciliation-worker"])
    assert validate_worker_presence(spec) == []


def test_present_worker_with_flag_off_fails_both_clover_workers() -> None:
    for worker in ("inbox-worker", "reconciliation-worker"):
        spec = _flagged({"CLOVER_ENABLED": "false"}, [worker])
        errs = validate_worker_presence(spec)
        assert any(worker in e and "CLOVER_ENABLED" in e for e in errs), (worker, errs)


def test_clover_on_but_either_worker_absent_fails() -> None:
    spec = _flagged({"CLOVER_ENABLED": "true"}, ["inbox-worker"])  # reconciliation missing
    assert any("reconciliation-worker" in e for e in validate_worker_presence(spec))
    spec = _flagged({"CLOVER_ENABLED": "true"}, ["reconciliation-worker"])  # inbox missing
    assert any("inbox-worker" in e for e in validate_worker_presence(spec))


def test_postmark_on_requires_inbound_email_and_extraction_workers() -> None:
    spec = _flagged({"POSTMARK_INBOUND_ENABLED": "true"}, [])
    errs = validate_worker_presence(spec)
    assert any("inbound-email-worker" in e for e in errs)
    assert any("receipt-extraction-worker" in e for e in errs)


def test_postmark_off_forbids_inbound_email_worker() -> None:
    spec = _flagged({}, ["inbound-email-worker"])
    assert any(
        "inbound-email-worker" in e and "POSTMARK_INBOUND_ENABLED" in e
        for e in validate_worker_presence(spec)
    )


def test_anthropic_declared_requires_extraction_worker() -> None:
    """A staging-shaped spec that declares the extraction credential but drops the
    receipt-extraction-worker MUST fail — uploads/emails would strand at 'pending'."""
    spec = _flagged({}, [], extra_envs=[{"key": "ANTHROPIC_API_KEY", "type": "SECRET"}])
    assert any("receipt-extraction-worker" in e for e in validate_worker_presence(spec))


def test_extraction_worker_without_anthropic_fails() -> None:
    spec = _flagged({}, ["receipt-extraction-worker"])
    assert any("ANTHROPIC_API_KEY" in e for e in validate_worker_presence(spec))


def test_extraction_worker_with_anthropic_on_component_passes() -> None:
    spec = {
        "envs": [],
        "workers": [
            {
                "name": "receipt-extraction-worker",
                "envs": [{"key": "ANTHROPIC_API_KEY", "type": "SECRET"}],
            }
        ],
    }
    assert validate_worker_presence(spec) == []


# ── committed-spec secret/duplicate rules ─────────────────────────────────────
def test_secret_with_ev_reference_fails() -> None:
    spec = {
        "envs": [{"key": "TOKEN_ENCRYPTION_KEY", "type": "SECRET", "value": "EV[1:abc:def]"}],
        "services": [],
    }
    assert any("TOKEN_ENCRYPTION_KEY" in e for e in validate_no_secret_values(spec))


def test_secret_with_literal_value_fails() -> None:
    spec = {
        "services": [
            {
                "name": "api",
                "envs": [{"key": "WORKOS_SECRET_KEY", "type": "SECRET", "value": "sk_live_x"}],
            }
        ]
    }
    assert any("WORKOS_SECRET_KEY" in e and "api" in e for e in validate_no_secret_values(spec))


def test_secret_declaration_without_value_passes() -> None:
    spec = {"envs": [{"key": "TOKEN_ENCRYPTION_KEY", "type": "SECRET"}]}
    assert validate_no_secret_values(spec) == []


def test_duplicate_app_and_component_env_fails() -> None:
    spec = {
        "envs": [{"key": "TOKEN_ENCRYPTION_KEY", "type": "SECRET"}],
        "services": [{"name": "api", "envs": [{"key": "TOKEN_ENCRYPTION_KEY", "type": "SECRET"}]}],
    }
    errs = validate_no_duplicate_envs(spec)
    assert any("BOTH app-level and component-level" in e for e in errs)


def test_duplicate_key_within_one_component_fails() -> None:
    spec = {
        "services": [
            {
                "name": "api",
                "envs": [
                    {"key": "CORS_ORIGINS", "value": "a"},
                    {"key": "CORS_ORIGINS", "value": "b"},
                ],
            }
        ]
    }
    assert any("more than once" in e for e in validate_no_duplicate_envs(spec))


# ── JSON worker-log source_commit parsing ─────────────────────────────────────
def test_parse_starting_commit_from_json() -> None:
    logs = (
        "2026-07-27 noise\n"
        '{"event":"inbox_worker.env_ready"}\n'
        '{"event":"inbox_worker.starting","source_commit":"abc123","interval_s":900}\n'
    )
    assert parse_starting_commit(logs, "inbox_worker") == "abc123"


def test_parse_returns_last_match() -> None:
    logs = (
        '{"event":"inbox_worker.starting","source_commit":"OLD"}\n'
        '{"event":"inbox_worker.starting","source_commit":"NEW"}\n'
    )
    assert parse_starting_commit(logs, "inbox_worker") == "NEW"


def test_parse_none_on_missing_or_unknown() -> None:
    assert parse_starting_commit('{"event":"other.event"}', "inbox_worker") is None
    assert (
        parse_starting_commit(
            '{"event":"inbox_worker.starting","source_commit":"unknown"}', "inbox_worker"
        )
        is None
    )
    assert parse_starting_commit("not json at all", "inbox_worker") is None
    assert parse_starting_commit('{"event":"inbox_worker.starting"}', "inbox_worker") is None


def test_parse_starting_evidence_requires_both_fields() -> None:
    """Round-8 finding 2: a record is evidence only when it carries BOTH a valid
    source_commit AND a non-empty service_user (the logged assertion result)."""
    from scripts.deploy_verify import parse_starting_evidence

    both = (
        '{"event":"inbox_worker.starting","source_commit":"abc","service_user":"service_worker_v2"}'
    )
    assert parse_starting_evidence(both, "inbox_worker") == ("abc", "service_worker_v2")
    commit_only = '{"event":"inbox_worker.starting","source_commit":"abc"}'
    assert parse_starting_evidence(commit_only, "inbox_worker") is None
    null_user = '{"event":"inbox_worker.starting","source_commit":"abc","service_user":null}'
    assert parse_starting_evidence(null_user, "inbox_worker") is None
    empty_user = '{"event":"inbox_worker.starting","source_commit":"abc","service_user":" "}'
    assert parse_starting_evidence(empty_user, "inbox_worker") is None
    user_only = '{"event":"inbox_worker.starting","service_user":"service_worker_v2"}'
    assert parse_starting_evidence(user_only, "inbox_worker") is None


def test_parse_starting_evidence_last_valid_wins() -> None:
    from scripts.deploy_verify import parse_starting_evidence

    logs = (
        '{"event":"inbox_worker.starting","source_commit":"OLD","service_user":"v1"}\n'
        '{"event":"inbox_worker.starting","source_commit":"NEW","service_user":"v2"}\n'
        '{"event":"inbox_worker.starting","source_commit":"NEWEST"}\n'  # invalid: no user
    )
    assert parse_starting_evidence(logs, "inbox_worker") == ("NEW", "v2")


def test_parse_role_ok_from_representative_json() -> None:
    """The app logs via structlog's JSONRenderer — the record is JSON, not key=value
    text, so a `request_user=…` grep can never match it (review finding). The parser
    must extract both users from the real record shape."""
    from scripts.deploy_verify import parse_role_ok

    logs = (
        "2026-07-29 platform noise\n"
        '{"event": "schema.ok", "level": "info"}\n'
        '{"request_user": "reorderos_app", "service_user": "service_worker", '
        '"event": "role.ok", "level": "info", "timestamp": "2026-07-29T00:00:00Z"}\n'
    )
    assert parse_role_ok(logs) == ("reorderos_app", "service_worker")


def test_parse_role_ok_against_the_actual_renderer(capsys: pytest.CaptureFixture[str]) -> None:
    """Generate the record through the APP'S OWN logging pipeline (configure_logging →
    structlog JSONRenderer) and parse that — the test can never drift from the real
    log shape the runbook gate consumes."""
    import structlog

    from app.core.logging import configure_logging
    from scripts.deploy_verify import parse_role_ok

    configure_logging()
    structlog.get_logger("app.lifespan").info(
        "role.ok", request_user="reorderos_app", service_user="service_worker"
    )
    line = capsys.readouterr().out
    assert parse_role_ok(line) == ("reorderos_app", "service_worker")


def test_parse_role_ok_rejects_wrong_missing_and_malformed() -> None:
    from scripts.deploy_verify import parse_role_ok

    assert parse_role_ok("") is None
    assert parse_role_ok("not json") is None
    assert parse_role_ok('{"event": "role.ok"}') is None  # users missing
    assert parse_role_ok('{"event": "role.ok", "request_user": "", "service_user": "x"}') is None
    # wrong users are still RETURNED (the gate compares the tuple) — last valid wins:
    logs = (
        '{"event": "role.ok", "request_user": "doadmin", "service_user": "doadmin"}\n'
        '{"event": "role.ok", "request_user": "reorderos_app", "service_user": "service_worker"}\n'
        '{"event": "role.ok"}\n'
    )
    assert parse_role_ok(logs) == ("reorderos_app", "service_worker")


def test_parse_last_valid_wins_over_trailing_invalid() -> None:
    """A trailing malformed/unknown/wrong-event record must NOT erase an earlier valid one —
    the LAST VALID record from the (deployment-bounded) log wins."""
    logs = (
        '{"event":"inbox_worker.starting","source_commit":"GOOD"}\n'
        '{"event":"inbox_worker.starting","source_commit":"unknown"}\n'
        '{"event":"inbox_worker.starting"}\n'
        '{"event":"reconciliation_worker.starting","source_commit":"OTHER"}\n'
        "trailing garbage not json\n"
    )
    assert parse_starting_commit(logs, "inbox_worker") == "GOOD"


# ── deterministic deployment-id selection ─────────────────────────────────────
def test_select_new_deployment_exactly_one() -> None:
    assert select_new_deployment(["a", "b"], ["a", "b", "c"]) == "c"


def test_select_new_deployment_rejects_zero() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        select_new_deployment(["a"], ["a"])


def test_select_new_deployment_rejects_multiple() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        select_new_deployment(["a"], ["a", "b", "c"])


def test_select_new_deployment_ignores_blank_lines() -> None:
    assert select_new_deployment(["a", "", " "], ["a", "", "b\n".strip()]) == "b"


# ── the REAL repo specs: COMPLETE validation, no partial production check ─────
@pytest.mark.parametrize("spec_name", ["app.yaml", "staging.app.yaml"])
def test_real_spec_passes_complete_validator(spec_name: str) -> None:
    """BOTH committed specs pass the COMPLETE validator (SOURCE_COMMIT + worker presence +
    no secret values + no duplicate envs). This replaces the old partial production test
    that skipped worker-presence — production is not special-cased."""
    from scripts.deploy_verify import load_spec

    spec = load_spec(os.path.join(_DO_DIR, spec_name))
    assert validate_spec(spec) == [], validate_spec(spec)


def test_staging_spec_missing_extraction_worker_fails() -> None:
    """BITING: strip receipt-extraction-worker from the real staging spec → the validator
    must reject (the Sprint 6 receipt stack requires it)."""
    from scripts.deploy_verify import load_spec

    spec = load_spec(os.path.join(_DO_DIR, "staging.app.yaml"))
    spec["workers"] = [w for w in spec["workers"] if w["name"] != "receipt-extraction-worker"]
    errs = validate_spec(spec)
    assert any("receipt-extraction-worker" in e for e in errs), errs


# ── safe diagnostic boundary: the CLI must NEVER leak input-derived text ──────
# Validator functions return detailed strings for programmatic use; the CLI reports
# only fixed diagnostic codes + counts + fixed remediation. These tests poison every
# input-derived channel (component names, env keys, secret values, filenames, the
# service-role argument) and prove nothing poisoned reaches stdout or stderr.
_POISON = "sk_live_SHOULD_NEVER_APPEAR"


def _poisoned_spec() -> dict[str, Any]:
    """Invalid spec whose VALIDATOR ERROR STRINGS are guaranteed to carry the poison via
    component name, env key, and secret value — and to fail three distinct categories
    (SOURCE_COMMIT_BINDING, SECRET_VALUE_PRESENT, DUPLICATE_ENV)."""
    return {
        "services": [
            {
                "name": _POISON,
                "envs": [
                    {"key": _POISON, "type": "SECRET", "value": _POISON},
                    {"key": _POISON, "type": "SECRET", "value": _POISON},
                ],
            }
        ],
    }


def test_poison_reaches_internal_validator_errors() -> None:
    """Precondition for the no-leak tests: the poison DOES appear in the returned
    validation errors (otherwise the CLI tests below would be vacuous)."""
    errs = validate_spec(_poisoned_spec())
    assert errs, "poisoned spec must be invalid"
    assert _POISON in " ".join(errs)


def test_cli_validate_never_leaks_poison(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Poisoned spec + poisoned FILENAME → exit 1, fixed codes + counts on stderr,
    and the poison in NEITHER stdout NOR stderr."""
    import yaml

    from scripts.deploy_verify import main

    spec_path = tmp_path / f"{_POISON}.yaml"
    spec_path.write_text(yaml.safe_dump(_poisoned_spec()))
    rc = main([str(spec_path)])
    out = capsys.readouterr()
    assert rc == 1
    assert _POISON not in out.out
    assert _POISON not in out.err
    assert str(spec_path) not in out.out and str(spec_path) not in out.err
    for code in ("SOURCE_COMMIT_BINDING", "SECRET_VALUE_PRESENT", "DUPLICATE_ENV"):
        assert f"deploy_verify FAIL [{code}]:" in out.err, out.err
        assert "failure(s)" in out.err


def test_cli_validate_counts_match_validators(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The per-category counts the CLI prints are the validators' actual counts."""
    import yaml

    from scripts.deploy_verify import (
        main,
        validate_no_duplicate_envs,
        validate_no_secret_values,
        validate_source_commit_bindings,
    )

    spec = _poisoned_spec()
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(spec))
    assert main([str(spec_path)]) == 1
    err = capsys.readouterr().err
    expected = {
        "SOURCE_COMMIT_BINDING": len(validate_source_commit_bindings(spec)),
        "SECRET_VALUE_PRESENT": len(validate_no_secret_values(spec)),
        "DUPLICATE_ENV": len(validate_no_duplicate_envs(spec)),
    }
    for code, count in expected.items():
        assert f"deploy_verify FAIL [{code}]: {count} failure(s)" in err, err


def test_cli_validate_real_specs_exit_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """Valid (real, committed) specs still exit 0 — and print no caller filename."""
    from scripts.deploy_verify import main

    for spec_name in ("app.yaml", "staging.app.yaml"):
        path = os.path.join(_DO_DIR, spec_name)
        assert main([path]) == 0
        out = capsys.readouterr()
        assert "deploy_verify OK" in out.out
        assert out.err == ""
        assert path not in out.out


def _minimal_cutover_pair() -> tuple[dict[str, Any], dict[str, Any]]:
    """(candidate, built) that PASS validate_cutover against an empty live spec."""
    spec = {
        "name": "reorderos",
        "services": [
            {
                "name": "api",
                "envs": [{"key": "SOURCE_COMMIT", "value": "${_self.COMMIT_HASH}"}],
            }
        ],
    }
    import copy

    return spec, copy.deepcopy(spec)


def test_cli_cutover_never_leaks_poison(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Poisoned cutover input (unexpected env key, drifted component name, poisoned
    filenames) → exit 1, fixed CUTOVER_CONTRACT code + count on stderr, poison in
    NEITHER stream. Also proves the poison IS in the internal cutover errors first."""
    import yaml

    from scripts.deploy_verify import main, validate_cutover

    candidate, built = _minimal_cutover_pair()
    built["services"][0]["envs"].append({"key": _POISON, "value": _POISON})
    built["workers"] = [{"name": _POISON, "envs": []}]
    internal = validate_cutover(built, candidate, {})
    assert internal and _POISON in " ".join(internal)

    built_path = tmp_path / f"built-{_POISON}.yaml"
    cand_path = tmp_path / "candidate.yaml"
    live_path = tmp_path / "live.yaml"
    built_path.write_text(yaml.safe_dump(built))
    cand_path.write_text(yaml.safe_dump(candidate))
    live_path.write_text(yaml.safe_dump({}))
    rc = main(["--cutover", str(built_path), str(cand_path), str(live_path)])
    out = capsys.readouterr()
    assert rc == 1
    assert _POISON not in out.out
    assert _POISON not in out.err
    assert f"deploy_verify FAIL [CUTOVER_CONTRACT]: {len(internal)} failure(s)" in out.err
    assert "do NOT apply" in out.err


def test_cli_cutover_never_leaks_poisoned_role_argument(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A poisoned SERVICE_ROLE argument lands in the internal error text but must not
    reach either stream."""
    import yaml

    from scripts.deploy_verify import main, validate_cutover

    candidate, built = _minimal_cutover_pair()
    assert _POISON in " ".join(validate_cutover(built, candidate, {}, _POISON))
    for name, doc in (("built.yaml", built), ("candidate.yaml", candidate), ("live.yaml", {})):
        (tmp_path / name).write_text(yaml.safe_dump(doc))
    rc = main(
        [
            "--cutover",
            str(tmp_path / "built.yaml"),
            str(tmp_path / "candidate.yaml"),
            str(tmp_path / "live.yaml"),
            _POISON,
        ]
    )
    out = capsys.readouterr()
    assert rc == 1
    assert _POISON not in out.out
    assert _POISON not in out.err
    assert "deploy_verify FAIL [CUTOVER_CONTRACT]: 1 failure(s)" in out.err


def test_cli_cutover_valid_exits_zero(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clean built/candidate/live triple still exits 0 with the fixed OK line."""
    import yaml

    from scripts.deploy_verify import main

    candidate, built = _minimal_cutover_pair()
    for name, doc in (("built.yaml", built), ("candidate.yaml", candidate), ("live.yaml", {})):
        (tmp_path / name).write_text(yaml.safe_dump(doc))
    rc = main(
        [
            "--cutover",
            str(tmp_path / "built.yaml"),
            str(tmp_path / "candidate.yaml"),
            str(tmp_path / "live.yaml"),
        ]
    )
    out = capsys.readouterr()
    assert rc == 0
    assert "deploy_verify --cutover OK" in out.out
    assert str(tmp_path) not in out.out
    assert out.err == ""
