"""Unit tests for scripts.deploy_verify — the deploy-spec + log verifier the runbook calls.

Covers SOURCE_COMMIT bindings (platform placeholder, component-level, no literal/app-level),
COMPLETE worker presence (Clover pair, Postmark, receipt-extraction — both directions), the
committed-spec secret/duplicate rules, JSON worker-log parsing (last VALID record wins), and
deterministic deployment-id selection. Also asserts the REAL repo specs pass the COMPLETE
validator — both of them, production included (no partial validation)."""

from __future__ import annotations

import os

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
