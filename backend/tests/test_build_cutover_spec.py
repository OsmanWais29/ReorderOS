"""scripts.build_cutover_spec + deploy_verify --cutover — fail-closed merge contract.

The builder produces the ONLY spec the runbook may apply: candidate shape + live EV refs
(identity-unchanged ONLY) + mode-0600 file-injected values for everything moved/new, and
mandatory preservation of an in-progress TOKEN_ENCRYPTION_KEY_PREVIOUS rotation. These
tests prove: strict EV identity, per-destination injection, abort-before-apply on ANY
unresolved value, rotation-key preservation-or-abort, GENERAL carry conflict rejection,
0600 hygiene in BOTH directions (inputs and output, including pre-existing files and
symlinks), no value ever printed, and that --cutover validation is a normalized DEEP
comparison — a mutation in ANY spec category fails it.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from scripts.build_cutover_spec import _write_0600, build
from scripts.build_cutover_spec import main as builder_main
from scripts.deploy_verify import validate_cutover, validate_live_role_declarations

_CANDIDATE_PATH = Path(__file__).resolve().parents[2] / ".do" / "staging.app.yaml"

if sys.platform == "win32":  # pragma: no cover
    pytest.skip("POSIX file modes required", allow_module_level=True)


def _candidate() -> dict:
    return yaml.safe_load(_CANDIDATE_PATH.read_text())


def _secret_file(dirpath: Path, name: str, value: str, mode: int = 0o600) -> Path:
    path = dirpath / name
    path.write_text(value + "\n")
    path.chmod(mode)
    return path


def _mini_candidate() -> dict:
    return {
        "name": "x",
        "envs": [
            {"key": "FLAG", "scope": "RUN_TIME", "value": "true"},
            # required home for the selected service role (see _apply_selected_service_role)
            {"key": "SERVICE_ROLE_NAME", "scope": "RUN_TIME", "value": "service_worker"},
        ],
        "services": [
            {
                "name": "api",
                "envs": [
                    {"key": "KEPT", "scope": "RUN_TIME", "value": "kept-value"},
                    {"key": "CARRY_CFG", "scope": "RUN_TIME", "value": ""},
                    {"key": "SAME_IDENTITY", "scope": "RUN_TIME", "type": "SECRET"},
                    {"key": "MOVED", "scope": "RUN_TIME", "type": "SECRET"},
                ],
            }
        ],
        "workers": [
            {
                "name": "w1",
                "envs": [{"key": "MOVED", "scope": "RUN_TIME", "type": "SECRET"}],
            }
        ],
    }


def _mini_live() -> dict:
    return {
        "name": "x",
        "envs": [
            # app-level secret: candidate declares it component-level → MOVED (no carry)
            {"key": "MOVED", "scope": "RUN_TIME", "type": "SECRET", "value": "EV[1:aa:bb]"},
            {"key": "CARRY_CFG", "scope": "RUN_TIME", "value": "cfg-from-live"},
        ],
        "services": [
            {
                "name": "api",
                "envs": [
                    {
                        "key": "SAME_IDENTITY",
                        "scope": "RUN_TIME",
                        "type": "SECRET",
                        "value": "EV[1:cc:dd]",
                    }
                ],
            }
        ],
    }


# ── EV identity rule ──────────────────────────────────────────────────────────
def test_identity_unchanged_ev_is_carried(tmp_path: Path) -> None:
    _secret_file(tmp_path, "MOVED", "moved-secret-value")
    result = build(_mini_candidate(), _mini_live(), tmp_path)
    assert result.errors == [] and result.spec is not None
    api_envs = {e["key"]: e for e in result.spec["services"][0]["envs"]}
    assert api_envs["SAME_IDENTITY"]["value"] == "EV[1:cc:dd]"
    assert ("services/api", "SAME_IDENTITY", "carried-ev") in result.report


def test_moved_secret_never_carries_the_ev_ref(tmp_path: Path) -> None:
    """App-level live EV + component-level candidate declaration = identity change →
    the EV ref must NOT be copied; the mode-0600 file value is injected instead —
    into EVERY destination (api and w1 both declare MOVED)."""
    _secret_file(tmp_path, "MOVED", "moved-secret-value")
    result = build(_mini_candidate(), _mini_live(), tmp_path)
    assert result.spec is not None
    api_envs = {e["key"]: e for e in result.spec["services"][0]["envs"]}
    w1_envs = {e["key"]: e for e in result.spec["workers"][0]["envs"]}
    assert api_envs["MOVED"]["value"] == "moved-secret-value"
    assert w1_envs["MOVED"]["value"] == "moved-secret-value"
    assert "EV[1:aa:bb]" not in yaml.safe_dump(result.spec)


def test_component_specific_file_beats_shared(tmp_path: Path) -> None:
    _secret_file(tmp_path, "MOVED", "shared-value")
    _secret_file(tmp_path, "api.MOVED", "api-specific-value")
    result = build(_mini_candidate(), _mini_live(), tmp_path)
    assert result.spec is not None
    api_envs = {e["key"]: e for e in result.spec["services"][0]["envs"]}
    w1_envs = {e["key"]: e for e in result.spec["workers"][0]["envs"]}
    assert api_envs["MOVED"]["value"] == "api-specific-value"
    assert w1_envs["MOVED"]["value"] == "shared-value"


# ── abort-before-apply ────────────────────────────────────────────────────────
def test_missing_file_aborts_with_no_spec(tmp_path: Path) -> None:
    result = build(_mini_candidate(), _mini_live(), tmp_path)  # no MOVED file
    assert result.spec is None
    assert any("MOVED" in e and "mode-0600" in e for e in result.errors)


def test_empty_and_group_readable_files_are_rejected(tmp_path: Path) -> None:
    _secret_file(tmp_path, "MOVED", "")  # empty
    assert build(_mini_candidate(), _mini_live(), tmp_path).spec is None
    _secret_file(tmp_path, "MOVED", "value", mode=0o644)  # world-readable
    result = build(_mini_candidate(), _mini_live(), tmp_path)
    assert result.spec is None
    assert any("0600" in e for e in result.errors)


def test_candidate_carrying_a_secret_value_is_rejected(tmp_path: Path) -> None:
    cand = _mini_candidate()
    cand["services"][0]["envs"][2]["value"] = "leaked"
    result = build(cand, _mini_live(), tmp_path)
    assert result.spec is None
    assert any("CANDIDATE" in e for e in result.errors)


def test_general_carry_from_live_and_missing_live_value_errors(tmp_path: Path) -> None:
    _secret_file(tmp_path, "MOVED", "v")
    ok = build(_mini_candidate(), _mini_live(), tmp_path)
    assert ok.spec is not None
    api_envs = {e["key"]: e for e in ok.spec["services"][0]["envs"]}
    assert api_envs["CARRY_CFG"]["value"] == "cfg-from-live"
    live = _mini_live()
    live["envs"] = [e for e in live["envs"] if e["key"] != "CARRY_CFG"]
    bad = build(_mini_candidate(), live, tmp_path)
    assert bad.spec is None and any("CARRY_CFG" in e for e in bad.errors)


def test_general_carry_rejects_conflicting_live_values(tmp_path: Path) -> None:
    """Two DIFFERENT non-secret live values for a carry-from-live key must ABORT —
    never silently pick the first. The error names locations, never values."""
    _secret_file(tmp_path, "MOVED", "v")
    live = _mini_live()
    live["services"][0]["envs"].append(
        {"key": "CARRY_CFG", "scope": "RUN_TIME", "value": "different-value"}
    )
    result = build(_mini_candidate(), live, tmp_path)
    assert result.spec is None
    err = next(e for e in result.errors if "CARRY_CFG" in e)
    assert "CONFLICTING" in err and "cfg-from-live" not in err and "different-value" not in err


# ── real-candidate fixtures ───────────────────────────────────────────────────
def _real_live_fixture() -> dict:
    """Live-staging-shaped fixture (structure from the 2026-07-27 read-only inventory)
    with opaque fake EV refs — exercises the REAL candidate end-to-end."""
    ev = "EV[1:fake:fake]"
    return {
        "name": "reorderos-staging",
        "envs": [
            {"key": "POSTMARK_INBOUND_ADDRESS", "scope": "RUN_TIME", "value": "inbound-addr-cfg"},
            {"key": "SERVICE_DATABASE_URL", "scope": "RUN_TIME", "type": "SECRET", "value": ev},
        ],
        "services": [
            {
                "name": "api",
                "envs": [
                    {
                        "key": "WORKOS_SECRET_KEY",
                        "scope": "RUN_TIME",
                        "type": "SECRET",
                        "value": ev,
                    },
                    {
                        "key": "ANTHROPIC_API_KEY",
                        "scope": "RUN_TIME",
                        "type": "SECRET",
                        "value": ev,
                    },
                ],
            }
        ],
    }


# DSN-shaped fakes: the builder/validator now verify USERNAMES (request pool must be
# reorderos_app; every service DSN must match the selected role), so the fixtures carry
# realistic-but-fake DSNs at a fake host.
_NEEDED_FILES = {
    "api.DATABASE_URL": "postgresql://reorderos_app:fake-pw-a1@db.internal:25060/app",
    "migrate.DATABASE_URL": "postgresql://doadmin:fake-pw-m1@db.internal:25060/app",
    # moved app-level -> components; username must equal the SELECTED service role
    "SERVICE_DATABASE_URL": "postgresql://service_worker:fake-pw-s1@db.internal:25060/app",
    "TOKEN_ENCRYPTION_KEY": "sv-fernet",
    "CLOVER_APP_SECRET": "sv-clover-secret",
    "CLOVER_WEBHOOK_AUTH_CODE": "sv-webhook-code",
    "DO_SPACES_KEY": "sv-spaces-key",
    "DO_SPACES_SECRET": "sv-spaces-secret",
    "POSTMARK_WEBHOOK_USER": "sv-pm-user",
    "POSTMARK_WEBHOOK_PASSWORD": "sv-pm-pass",
    "ANTHROPIC_API_KEY": "sv-anthropic-worker",  # worker copy (api copy carried)
    "WORKOS_SECRET_KEY": "sv-workos",  # unused when carried; harmless
}
_V2_SERVICE_DSN = "postgresql://service_worker_v2:fake-pw-s2@db.internal:25060/app"


def _full_secrets_dir(tmp_path: Path) -> Path:
    secrets = tmp_path / "secrets"
    secrets.mkdir(exist_ok=True)
    for name, value in _NEEDED_FILES.items():
        _secret_file(secrets, name, value)
    return secrets


# ── TOKEN_ENCRYPTION_KEY_PREVIOUS: preserve-or-abort (never a note) ───────────
def _live_with_previous_key() -> dict:
    live = _real_live_fixture()
    live["envs"].append(
        {
            "key": "TOKEN_ENCRYPTION_KEY_PREVIOUS",
            "scope": "RUN_TIME",
            "type": "SECRET",
            "value": "EV[1:prev:prev]",
        }
    )
    return live


def _token_components(spec: dict) -> set[str]:
    out = set()
    for kind in ("services", "workers", "jobs"):
        for comp in spec.get(kind) or []:
            if any(e.get("key") == "TOKEN_ENCRYPTION_KEY" for e in comp.get("envs") or []):
                out.add(comp["name"])
    return out


def test_live_previous_key_without_file_aborts(tmp_path: Path) -> None:
    """BITING (P1 regression): a live rotation key can NEVER be dropped with only a
    note. App-level live EV = moved identity → file required → without it the build
    ABORTS naming the key and every token-decrypting destination."""
    secrets = _full_secrets_dir(tmp_path)
    result = build(_candidate(), _live_with_previous_key(), secrets)
    assert result.spec is None, (
        "builder must ABORT, not note, when the live rotation key is unresolved"
    )
    errs = [e for e in result.errors if "TOKEN_ENCRYPTION_KEY_PREVIOUS" in e]
    assert errs and len(errs) == len(_token_components(_candidate())), errs
    assert all("retire" in e for e in errs)


def test_live_previous_key_is_preserved_in_every_token_component(tmp_path: Path) -> None:
    secrets = _full_secrets_dir(tmp_path)
    _secret_file(secrets, "TOKEN_ENCRYPTION_KEY_PREVIOUS", "prev-key-material")
    result = build(_candidate(), _live_with_previous_key(), secrets)
    assert result.errors == [] and result.spec is not None
    expected = _token_components(_candidate())
    assert expected == {"api", "inbox-worker", "reconciliation-worker"}
    for kind in ("services", "workers", "jobs"):
        for comp in result.spec.get(kind) or []:
            envs = {e["key"]: e for e in comp["envs"]}
            if comp["name"] in expected:
                entry = envs["TOKEN_ENCRYPTION_KEY_PREVIOUS"]
                assert entry["type"] == "SECRET" and entry["value"] == "prev-key-material"
            else:
                assert "TOKEN_ENCRYPTION_KEY_PREVIOUS" not in envs
    # …and the built spec still passes --cutover validation (documented substitution 2):
    assert validate_cutover(result.spec, _candidate(), _live_with_previous_key()) == []


def test_no_live_previous_key_adds_nothing_and_requires_nothing(tmp_path: Path) -> None:
    """Fresh environment: absence of the rotation key must not fail the build and must
    not fabricate entries."""
    secrets = _full_secrets_dir(tmp_path)
    result = build(_candidate(), _real_live_fixture(), secrets)
    assert result.errors == [] and result.spec is not None
    assert "TOKEN_ENCRYPTION_KEY_PREVIOUS" not in yaml.safe_dump(result.spec)


def test_cutover_rejects_partial_previous_key_distribution(tmp_path: Path) -> None:
    """BITING (review finding 4): the validator gets the FRESH LIVE spec and derives the
    required destinations from the CANDIDATE independently — a rotation distribution
    missing from ONE token-decrypting component (its ciphertext would become
    undecryptable) must fail --cutover validation, not slip through as 'optional'."""
    secrets = _full_secrets_dir(tmp_path)
    _secret_file(secrets, "TOKEN_ENCRYPTION_KEY_PREVIOUS", "prev-key-material")
    live = _live_with_previous_key()
    result = build(_candidate(), live, secrets)
    assert result.spec is not None
    inbox = next(w for w in result.spec["workers"] if w["name"] == "inbox-worker")
    inbox["envs"] = [e for e in inbox["envs"] if e["key"] != "TOKEN_ENCRYPTION_KEY_PREVIOUS"]
    errs = validate_cutover(result.spec, _candidate(), live)
    assert any("inbox-worker" in e and "all-or-none" in e for e in errs), errs
    # …and stripping it EVERYWHERE fails on every destination:
    for kind in ("services", "workers"):
        for comp in result.spec.get(kind) or []:
            comp["envs"] = [e for e in comp["envs"] if e["key"] != "TOKEN_ENCRYPTION_KEY_PREVIOUS"]
    errs = validate_cutover(result.spec, _candidate(), live)
    assert len([e for e in errs if "all-or-none" in e]) == 3, errs


def test_cutover_rejects_previous_key_when_live_has_none(tmp_path: Path) -> None:
    """The inverse direction: live carries NO previous key → no component may carry one
    (there is nothing to preserve; a stray entry is drift, not a substitution)."""
    built = _built_ok(tmp_path)
    api = next(s for s in built["services"] if s["name"] == "api")
    api["envs"].append(
        {
            "key": "TOKEN_ENCRYPTION_KEY_PREVIOUS",
            "scope": "RUN_TIME",
            "type": "SECRET",
            "value": "stray",
        }
    )
    errs = validate_cutover(built, _candidate(), _real_live_fixture())
    assert any("nothing to preserve" in e for e in errs), errs


# ── secret output writer: 0600 in every direction ─────────────────────────────
def test_writer_rejects_existing_file_and_preserves_it(tmp_path: Path) -> None:
    """O_CREAT's mode applies only on creation — overwriting a 0644 file would leave
    secrets 0644. The writer must REFUSE an existing path outright."""
    out = tmp_path / "o.yaml"
    out.write_text("pre-existing")
    out.chmod(0o644)
    with pytest.raises(FileExistsError):
        _write_0600(out, "secret: x\n")
    assert out.read_text() == "pre-existing"  # untouched
    assert stat.S_IMODE(out.stat().st_mode) == 0o644  # and not silently re-moded


def test_writer_rejects_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target.yaml"
    target.write_text("innocent")
    link = tmp_path / "link.yaml"
    link.symlink_to(target)
    with pytest.raises(FileExistsError, match="symlink"):
        _write_0600(link, "secret: x\n")
    assert target.read_text() == "innocent"
    dangling = tmp_path / "dangling.yaml"
    dangling.symlink_to(tmp_path / "nope")
    with pytest.raises(FileExistsError):
        _write_0600(dangling, "secret: x\n")


def test_writer_partial_failure_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "o.yaml"

    def boom(fd: int, data: bytes) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", boom)
    with pytest.raises(OSError, match="disk full"):
        _write_0600(out, "secret: x\n")
    assert not out.exists(), "a partial secret-bearing file must not survive"


def test_writer_output_exactly_0600_even_under_permissive_umask(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        out = tmp_path / "o.yaml"
        _write_0600(out, "a: b\n")
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
    finally:
        os.umask(old)


def test_writer_completes_short_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BITING (review finding 3): os.write may return a SHORT COUNT without raising —
    a single un-looped write would 'succeed' having written a prefix. Force 3-byte
    writes and prove the FULL payload lands."""
    real_write = os.write
    calls = {"n": 0}

    def short_write(fd: int, data: bytes) -> int:
        calls["n"] += 1
        return real_write(fd, bytes(data)[:3])

    monkeypatch.setattr(os, "write", short_write)
    out = tmp_path / "o.yaml"
    payload = "secret_key: some-longer-value-that-needs-many-writes\n"
    _write_0600(out, payload)
    assert out.read_text() == payload, "short write survived — writer must loop on the count"
    assert calls["n"] > 1  # the loop actually engaged


def test_writer_zero_byte_write_aborts_and_unlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    out = tmp_path / "o.yaml"
    with pytest.raises(OSError, match="short write"):
        _write_0600(out, "secret: x\n")
    assert not out.exists()


def test_cli_refuses_existing_output(tmp_path: Path) -> None:
    secrets = _full_secrets_dir(tmp_path)
    live_path = tmp_path / "live.yaml"
    live_path.write_text(yaml.safe_dump(_real_live_fixture()))
    out = tmp_path / "cutover.yaml"
    out.write_text("stale")
    rc = builder_main(
        [
            "--candidate",
            str(_CANDIDATE_PATH),
            "--live",
            str(live_path),
            "--secrets",
            str(secrets),
            "--out",
            str(out),
        ]
    )
    assert rc == 1
    assert out.read_text() == "stale"  # never overwritten in place


# ── CLI end-to-end: real candidate, no values printed, unlink ─────────────────
def test_cli_end_to_end_real_candidate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Full run against the REAL committed candidate: identity-unchanged api EV refs
    carried, every moved/new secret injected from files, output written 0600, and NO
    secret value in stdout/stderr."""
    secrets = _full_secrets_dir(tmp_path)
    live_path = tmp_path / "live.yaml"
    live_path.write_text(yaml.safe_dump(_real_live_fixture()))
    live_path.chmod(0o600)

    out = tmp_path / "cutover.yaml"
    rc = builder_main(
        [
            "--candidate",
            str(_CANDIDATE_PATH),
            "--live",
            str(live_path),
            "--secrets",
            str(secrets),
            "--out",
            str(out),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    for value in [*_NEEDED_FILES.values(), "EV[1:fake:fake]", "inbound-addr-cfg"]:
        assert value not in captured.out and value not in captured.err
    built = yaml.safe_load(out.read_text())
    api = next(s for s in built["services"] if s["name"] == "api")
    api_envs = {e["key"]: e for e in api["envs"]}
    assert api_envs["WORKOS_SECRET_KEY"]["value"] == "EV[1:fake:fake]"
    assert api_envs["ANTHROPIC_API_KEY"]["value"] == "EV[1:fake:fake]"
    assert api_envs["POSTMARK_INBOUND_ADDRESS"]["value"] == "inbound-addr-cfg"
    assert validate_cutover(built, _candidate(), _real_live_fixture()) == []
    out.unlink()  # the runbook's cleanup contract: unlink after use (no shred claims)
    assert not out.exists()


def test_cli_aborts_without_writing_output(tmp_path: Path) -> None:
    live_path = tmp_path / "live.yaml"
    live_path.write_text(yaml.safe_dump(_real_live_fixture()))
    (tmp_path / "secrets").mkdir()
    out = tmp_path / "cutover.yaml"
    rc = builder_main(
        [
            "--candidate",
            str(_CANDIDATE_PATH),
            "--live",
            str(live_path),
            "--secrets",
            str(tmp_path / "secrets"),
            "--out",
            str(out),
        ]
    )
    assert rc == 1
    assert not out.exists(), "abort must not leave a (partial) secret-bearing spec behind"


# ── --cutover validation: normalized DEEP comparison bites in EVERY category ──
def _built_ok(tmp_path: Path) -> dict:
    result = build(_candidate(), _real_live_fixture(), _full_secrets_dir(tmp_path))
    assert result.spec is not None, result.errors
    return result.spec


def test_cutover_accepts_clean_build(tmp_path: Path) -> None:
    assert validate_cutover(_built_ok(tmp_path), _candidate(), _real_live_fixture()) == []


def test_cutover_rejects_valueless_entry(tmp_path: Path) -> None:
    built = _built_ok(tmp_path)
    api = next(s for s in built["services"] if s["name"] == "api")
    next(e for e in api["envs"] if e["key"] == "TOKEN_ENCRYPTION_KEY")["value"] = ""
    errs = validate_cutover(built, _candidate(), _real_live_fixture())
    assert any("TOKEN_ENCRYPTION_KEY" in e and "no value" in e for e in errs)


def _api(spec: dict) -> dict:
    return next(s for s in spec["services"] if s["name"] == "api")


_MUTATIONS: dict[str, Callable[[dict], None]] = {
    "dockerfile_path": lambda s: _api(s).__setitem__("dockerfile_path", "backend/Dockerfile.evil"),
    "source_dir": lambda s: _api(s).__setitem__("source_dir", "backend-evil"),
    "health_check_path": lambda s: _api(s)["health_check"].__setitem__("http_path", "/health/live"),
    "routes": lambda s: _api(s).__setitem__("routes", [{"path": "/evil"}]),
    "instance_count": lambda s: _api(s).__setitem__("instance_count", 3),
    "instance_size": lambda s: _api(s).__setitem__("instance_size_slug", "professional-xs"),
    "http_port": lambda s: _api(s).__setitem__("http_port", 9090),
    "run_command": lambda s: _api(s).__setitem__(
        "run_command", "sh -c 'alembic upgrade head && uvicorn app.main:app'"
    ),
    "github_repo": lambda s: _api(s)["github"].__setitem__("repo", "Evil/Repo"),
    "github_branch": lambda s: _api(s)["github"].__setitem__("branch", "main"),
    "deploy_on_push": lambda s: _api(s)["github"].__setitem__("deploy_on_push", True),
    "job_kind": lambda s: s["jobs"][0].__setitem__("kind", "POST_DEPLOY"),
    "database_cluster": lambda s: s["databases"][0].__setitem__("cluster_name", "evil-pg"),
    "app_name": lambda s: s.__setitem__("name", "reorderos-evil"),
    "region": lambda s: s.__setitem__("region", "nyc1"),
    "env_scope_change": lambda s: next(
        e for e in _api(s)["envs"] if e["key"] == "DATABASE_URL"
    ).__setitem__("scope", "RUN_AND_BUILD_TIME"),
    "env_type_change": lambda s: next(
        e for e in _api(s)["envs"] if e["key"] == "CORS_ORIGINS"
    ).__setitem__("type", "SECRET"),
    "extra_env": lambda s: _api(s)["envs"].append(
        {"key": "SNEAKY", "scope": "RUN_TIME", "value": "x"}
    ),
    "missing_env": lambda s: _api(s).__setitem__(
        "envs", [e for e in _api(s)["envs"] if e["key"] != "WORKOS_SECRET_KEY"]
    ),
    "pinned_value_change": lambda s: next(
        e for e in _api(s)["envs"] if e["key"] == "CORS_ORIGINS"
    ).__setitem__("value", '["https://evil.example"]'),
    "component_removed": lambda s: s.__setitem__(
        "workers", [w for w in s["workers"] if w["name"] != "receipt-extraction-worker"]
    ),
    "new_top_level_block": lambda s: s.__setitem__("alerts", [{"rule": "DEPLOYMENT_FAILED"}]),
    "previous_key_on_non_token_component": lambda s: next(
        w for w in s["workers"] if w["name"] == "inbound-email-worker"
    )["envs"].append(
        {
            "key": "TOKEN_ENCRYPTION_KEY_PREVIOUS",
            "scope": "RUN_TIME",
            "type": "SECRET",
            "value": "x",
        }
    ),
}


@pytest.mark.parametrize("category", sorted(_MUTATIONS))
def test_cutover_rejects_every_mutation_category(category: str, tmp_path: Path) -> None:
    """DEEP comparison: mutate the built spec in each material category — Dockerfile,
    source_dir, health checks, routes, instances, ports, commands, GitHub identity,
    job kind, databases, name/region, env identities/values, components, unknown
    top-level blocks, and a mis-placed rotation key — and prove validation fails."""
    built = _built_ok(tmp_path)
    _MUTATIONS[category](built)
    errs = validate_cutover(built, _candidate(), _real_live_fixture())
    assert errs, f"mutation {category!r} passed cutover validation — validator is blind to it"


def test_cutover_multi_category_mutation_reports_both(tmp_path: Path) -> None:
    """The review's exact probe: Dockerfile + health-check mutated together must fail
    (the previous validator returned no errors for this pair)."""
    built = _built_ok(tmp_path)
    _MUTATIONS["dockerfile_path"](built)
    _MUTATIONS["health_check_path"](built)
    errs = validate_cutover(built, _candidate(), _real_live_fixture())
    assert any("dockerfile_path" in e for e in errs)
    assert any("health_check" in e for e in errs)


def test_cutover_error_messages_never_contain_values(tmp_path: Path) -> None:
    built = _built_ok(tmp_path)
    _MUTATIONS["pinned_value_change"](built)
    _MUTATIONS["extra_env"](built)
    errs = validate_cutover(built, _candidate(), _real_live_fixture())
    joined = "\n".join(errs)
    for secret in _NEEDED_FILES.values():
        assert secret not in joined
    assert "evil.example" not in joined


# ── selected-service-role contract, end to end (round 5, findings 2+4) ────────
def _role_name_values(spec: dict) -> set[str]:
    out = set()
    for holder in [
        spec,
        *spec.get("services", []),
        *spec.get("workers", []),
        *spec.get("jobs", []),
    ]:
        for e in holder.get("envs") or []:
            if e.get("key") == "SERVICE_ROLE_NAME":
                out.add(str(e.get("value")))
    return out


def _independent_username(value: str) -> str | None:
    """TEST ORACLE: derives the DSN username with urllib.parse directly — deliberately
    NOT the production dsn_username(), so a bug there cannot self-certify."""
    from urllib.parse import urlparse as _up

    if not value or value.startswith("EV["):
        return None
    return _up(value.replace("postgresql+asyncpg://", "postgresql://")).username


def _service_dsn_usernames(spec: dict) -> set[str | None]:
    out: set[str | None] = set()
    for kind in ("services", "workers", "jobs"):
        for comp in spec.get(kind) or []:
            for e in comp.get("envs") or []:
                if e.get("key") == "SERVICE_DATABASE_URL":
                    out.add(_independent_username(str(e.get("value") or "")))
    for e in spec.get("envs") or []:
        if e.get("key") == "SERVICE_DATABASE_URL":
            out.add(_independent_username(str(e.get("value") or "")))
    return out


def test_balt_end_to_end_contract(tmp_path: Path) -> None:
    """The FULL selected-role contract (review round 5): one source of truth flows from
    `--service-role` through the effective spec, the validator, and role.ok parsing —
    standard and versioned paths both machine-validated, every rejection biting."""
    from scripts.deploy_verify import parse_role_ok

    secrets_dir = _full_secrets_dir(tmp_path)
    live = _real_live_fixture()

    # 1. STANDARD effective spec: service_worker throughout.
    std = build(_candidate(), live, secrets_dir)
    assert std.spec is not None, std.errors
    assert _role_name_values(std.spec) == {"service_worker"}
    assert _service_dsn_usernames(std.spec) == {"service_worker"}
    assert validate_cutover(std.spec, _candidate(), live, "service_worker") == []

    # 2-5. VERSIONED effective spec for service_worker_v2.
    _secret_file(secrets_dir, "SERVICE_DATABASE_URL", _V2_SERVICE_DSN)
    v2 = build(_candidate(), live, secrets_dir, service_role="service_worker_v2")
    assert v2.spec is not None, v2.errors
    assert _service_dsn_usernames(v2.spec) == {"service_worker_v2"}  # 3.
    api = next(s for s in v2.spec["services"] if s["name"] == "api")
    api_db = next(e for e in api["envs"] if e["key"] == "DATABASE_URL")
    assert _independent_username(str(api_db["value"])) == "reorderos_app"  # 4.
    assert _role_name_values(v2.spec) == {"service_worker_v2"}  # 5.

    # 6. deploy_verify passes the versioned effective spec.
    assert validate_cutover(v2.spec, _candidate(), live, "service_worker_v2") == []

    # 7. representative role.ok JSON accepts v2 (dynamic expectation, no hardcode).
    logs = (
        '{"request_user": "reorderos_app", "service_user": "service_worker_v2", '
        '"event": "role.ok", "level": "info"}'
    )
    assert parse_role_ok(logs) == ("reorderos_app", "service_worker_v2")

    # 8. rejections: unapproved names, mismatches, missing binding, malformed DSN.
    evil = build(_candidate(), live, secrets_dir, service_role="service_worker_evil")
    assert evil.spec is None and any("not allowed" in e for e in evil.errors)
    arbitrary = validate_cutover(v2.spec, _candidate(), live, "svc")
    assert arbitrary == [next(e for e in arbitrary if "not allowed" in e)]
    mismatch = validate_cutover(v2.spec, _candidate(), live, "service_worker")
    assert any("SERVICE_DATABASE_URL username disagrees" in e for e in mismatch)
    assert any("SERVICE_ROLE_NAME" in e for e in mismatch)
    # builder-side mismatch: selected v2 but the credential file is still service_worker
    _secret_file(secrets_dir, "SERVICE_DATABASE_URL", _NEEDED_FILES["SERVICE_DATABASE_URL"])
    cross = build(_candidate(), live, secrets_dir, service_role="service_worker_v2")
    assert cross.spec is None
    assert any("does not match the selected service role" in e for e in cross.errors)
    _secret_file(secrets_dir, "SERVICE_DATABASE_URL", _V2_SERVICE_DSN)
    # missing binding: strip SERVICE_ROLE_NAME from the built spec
    import copy

    stripped = copy.deepcopy(v2.spec)
    stripped["envs"] = [e for e in stripped["envs"] if e["key"] != "SERVICE_ROLE_NAME"]
    missing = validate_cutover(stripped, _candidate(), live, "service_worker_v2")
    assert any("SERVICE_ROLE_NAME" in e and "missing" in e for e in missing)
    # malformed DSN: unverifiable username must be rejected
    malformed = copy.deepcopy(v2.spec)
    api_m = next(s for s in malformed["services"] if s["name"] == "api")
    next(e for e in api_m["envs"] if e["key"] == "SERVICE_DATABASE_URL")["value"] = "EV[1:x:y]"
    bad = validate_cutover(malformed, _candidate(), live, "service_worker_v2")
    assert any("unverifiable" in e or "disagrees" in e for e in bad)

    # 9. Phase C after B-alt cannot silently revert. NOTE (round-6 correction): the
    # earlier version of this step used a PLAINTEXT deepcopy of the built spec as
    # "live" — an invalid fixture (real DO live specs carry EV[...] refs); the
    # DO-shaped repeatability proof now lives in
    # test_rebuild_against_do_shaped_ev_live_spec. Role-bound DSNs never carry, so
    # the revert refusal is the VALIDATOR's monotonic rule, driven by the live
    # SERVICE_ROLE_NAME declaration alone:
    live_role_only = _ev_shaped_live(v2.spec)  # DO-shaped: every SECRET is an EV ref
    _secret_file(secrets_dir, "SERVICE_DATABASE_URL", _NEEDED_FILES["SERVICE_DATABASE_URL"])
    std_spec = build(_candidate(), live_role_only, secrets_dir)  # sw DSNs from files
    assert std_spec.spec is not None, std_spec.errors
    revert = validate_cutover(std_spec.spec, _candidate(), live_role_only, "service_worker")
    assert any("DOWNGRADE" in e for e in revert), revert
    # …while re-selecting the live versioned role is accepted (monotonic: equal).
    _secret_file(secrets_dir, "SERVICE_DATABASE_URL", _V2_SERVICE_DSN)
    v2_again = build(_candidate(), live_role_only, secrets_dir, service_role="service_worker_v2")
    assert v2_again.spec is not None, v2_again.errors
    assert validate_cutover(v2_again.spec, _candidate(), live_role_only, "service_worker_v2") == []


# ── round 6: DigitalOcean-shaped live specs (EV[...] secrets) ─────────────────
def _ev_shaped_live(spec: dict) -> dict:
    """Transform a deployable spec into the shape DigitalOcean RETURNS for a live app:
    every SECRET value becomes a realistic opaque EV[...] reference. This is what
    `doctl apps spec get` actually yields — the round-5 fixture that kept plaintext
    DSNs was NOT a valid live spec."""
    import copy

    live = copy.deepcopy(spec)
    counter = 0
    for holder in [
        live,
        *live.get("services", []),
        *live.get("workers", []),
        *live.get("jobs", []),
    ]:
        for e in holder.get("envs") or []:
            if e.get("type") == "SECRET" and str(e.get("value") or "").strip():
                counter += 1
                e["value"] = f"EV[1:b64opaquesalt{counter:02d}:ciphertextb64payload{counter:02d}]"
    return live


def test_rebuild_against_do_shaped_ev_live_spec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ROUND-6 P1 (independently confirmed by review): a rebuild against a REAL
    DigitalOcean live spec — where every deployed SECRET is an opaque EV[...] ref —
    must succeed with fresh local role-bound DSN files, because role-bound DSNs are
    NEVER carried from live. The round-5 plaintext-deepcopy 'live' fixture masked
    this; this test is the corrected, platform-shaped proof."""
    secrets_dir = _full_secrets_dir(tmp_path)
    live0 = _real_live_fixture()

    # 1. build + validate a plaintext service_worker_v2 cutover.
    _secret_file(secrets_dir, "SERVICE_DATABASE_URL", _V2_SERVICE_DSN)
    first = build(_candidate(), live0, secrets_dir, service_role="service_worker_v2")
    assert first.spec is not None, first.errors
    assert validate_cutover(first.spec, _candidate(), live0, "service_worker_v2") == []

    # 2-4. deep-copy into a live fixture; EV-ify every SECRET; keep SERVICE_ROLE_NAME=v2.
    live_v2 = _ev_shaped_live(first.spec)
    assert _role_name_values(live_v2) == {"service_worker_v2"}
    ev_values = [
        str(e.get("value"))
        for holder in [
            live_v2,
            *live_v2.get("services", []),
            *live_v2.get("workers", []),
            *live_v2.get("jobs", []),
        ]
        for e in holder.get("envs") or []
        if e.get("type") == "SECRET"
    ]
    assert ev_values and all(v.startswith("EV[") for v in ev_values)

    # 5-6. rebuild with fresh local role-bound files → build AND validation succeed.
    second = build(_candidate(), live_v2, secrets_dir, service_role="service_worker_v2")
    assert second.spec is not None, (
        "rebuild against a DO-shaped (EV) live spec must succeed with fresh role-bound "
        f"files — errors: {second.errors}"
    )
    assert validate_cutover(second.spec, _candidate(), live_v2, "service_worker_v2") == []

    # 7. no role-bound DSN in the built spec is EV-backed; usernames independently derived.
    for kind in ("services", "workers", "jobs"):
        for comp in second.spec.get(kind) or []:
            for e in comp.get("envs") or []:
                if e.get("key") in ("DATABASE_URL", "SERVICE_DATABASE_URL"):
                    value = str(e.get("value"))
                    assert not value.startswith("EV["), (
                        f"{comp['name']}: role-bound {e['key']} is EV-backed"
                    )
                    expected = {
                        ("services", "DATABASE_URL"): "reorderos_app",
                        ("jobs", "DATABASE_URL"): "doadmin",
                    }.get((kind, e["key"]), "service_worker_v2")
                    assert _independent_username(value) == expected
    # …while UNRELATED identity-preserved secrets remain EV-carried (9.):
    api = next(s for s in second.spec["services"] if s["name"] == "api")
    api_envs = {e["key"]: str(e.get("value")) for e in api["envs"]}
    for unrelated in ("WORKOS_SECRET_KEY", "ANTHROPIC_API_KEY", "TOKEN_ENCRYPTION_KEY"):
        assert api_envs[unrelated].startswith("EV["), f"{unrelated} should be EV-carried"

    # 8. remove each required role-bound file individually → fail-closed, no output.
    for missing in ("api.DATABASE_URL", "migrate.DATABASE_URL", "SERVICE_DATABASE_URL"):
        content = (secrets_dir / missing).read_text()
        (secrets_dir / missing).unlink()
        broken = build(_candidate(), live_v2, secrets_dir, service_role="service_worker_v2")
        assert broken.spec is None, f"missing {missing} must abort"
        assert any("role-bound DSN" in e and missing.split(".")[-1] in e for e in broken.errors)
        _secret_file(secrets_dir, missing, content.strip())
    # CLI path for one case: abort leaves no output artifact.
    (secrets_dir / "SERVICE_DATABASE_URL").unlink()
    live_path = tmp_path / "live_v2.yaml"
    live_path.write_text(yaml.safe_dump(live_v2))
    out = tmp_path / "rebuild.yaml"
    rc = builder_main(
        [
            "--candidate",
            str(_CANDIDATE_PATH),
            "--live",
            str(live_path),
            "--secrets",
            str(secrets_dir),
            "--service-role",
            "service_worker_v2",
            "--out",
            str(out),
        ]
    )
    assert rc == 1 and not out.exists()
    _secret_file(secrets_dir, "SERVICE_DATABASE_URL", _V2_SERVICE_DSN)

    # 10. no error, report line, or captured output contains a DSN, password, EV body,
    # hostname, or token.
    captured = capsys.readouterr()
    corpus = "\n".join(broken.errors) + captured.out + captured.err
    for forbidden in ("db.internal", "fake-pw", "ciphertextb64payload", "postgresql://"):
        assert forbidden not in corpus, f"leak: {forbidden!r} appeared in output/errors"


# ── round 6: monotonic version rotation ───────────────────────────────────────
@pytest.mark.parametrize(
    ("live_role", "selected", "ok"),
    [
        ("service_worker_v1", "service_worker_v2", True),
        ("service_worker_v2", "service_worker_v2", True),
        ("service_worker_v2", "service_worker_v1", False),
        ("service_worker_v2", "service_worker", False),
        (None, "service_worker", True),
        (None, "service_worker_v3", True),
        ("service_worker", "service_worker_v1", True),
    ],
)
def test_monotonic_version_rotation(
    live_role: str | None, selected: str, ok: bool, tmp_path: Path
) -> None:
    """Rotations are monotonic: same version or forward only. A downgrade — including
    back to plain service_worker — needs its own approved rollback procedure and is
    rejected by the normal cutover selector."""
    secrets_dir = _full_secrets_dir(tmp_path)
    dsn_user = selected
    _secret_file(
        secrets_dir,
        "SERVICE_DATABASE_URL",
        f"postgresql://{dsn_user}:fake-pw-mono@db.internal:25060/app",
    )
    live = _real_live_fixture()
    if live_role is not None:
        live["envs"].append({"key": "SERVICE_ROLE_NAME", "scope": "RUN_TIME", "value": live_role})
    built = build(_candidate(), live, secrets_dir, service_role=selected)
    assert built.spec is not None, built.errors
    errors = validate_cutover(built.spec, _candidate(), live, selected)
    if ok:
        assert errors == [], errors
    else:
        assert any("DOWNGRADE" in e for e in errors), errors


@pytest.mark.parametrize(
    "bad", ["service_worker_v0", "service_worker_v01", "service_worker_v", "svc"]
)
def test_non_canonical_role_names_rejected(bad: str, tmp_path: Path) -> None:
    """v0, leading zeros, and arbitrary names are not canonical — rejected by the
    builder AND the validator."""
    secrets_dir = _full_secrets_dir(tmp_path)
    result = build(_candidate(), _real_live_fixture(), secrets_dir, service_role=bad)
    assert result.spec is None and any("not allowed" in e for e in result.errors)
    built_ok = build(_candidate(), _real_live_fixture(), secrets_dir)
    assert built_ok.spec is not None
    errs = validate_cutover(built_ok.spec, _candidate(), _real_live_fixture(), bad)
    assert any("not allowed" in e for e in errs)


# ── round 6: conflicting live role declarations fail closed ───────────────────
def test_conflicting_live_role_declarations_abort(tmp_path: Path) -> None:
    """App-level vs component-level DISTINCT SERVICE_ROLE_NAME values must abort —
    never resolved by traversal order."""
    secrets_dir = _full_secrets_dir(tmp_path)
    built = build(_candidate(), _real_live_fixture(), secrets_dir)
    assert built.spec is not None
    live = _real_live_fixture()
    live["envs"].append(
        {"key": "SERVICE_ROLE_NAME", "scope": "RUN_TIME", "value": "service_worker_v2"}
    )
    live["services"][0]["envs"].append(
        {"key": "SERVICE_ROLE_NAME", "scope": "RUN_TIME", "value": "service_worker_v3"}
    )
    errs = validate_cutover(built.spec, _candidate(), live, "service_worker_v3")
    assert any("CONFLICTING" in e and "SERVICE_ROLE_NAME" in e for e in errs), errs


def test_duplicate_identical_live_role_declarations_are_structural_drift(tmp_path: Path) -> None:
    secrets_dir = _full_secrets_dir(tmp_path)
    _secret_file(secrets_dir, "SERVICE_DATABASE_URL", _V2_SERVICE_DSN)
    built = build(_candidate(), _real_live_fixture(), secrets_dir, service_role="service_worker_v2")
    assert built.spec is not None
    live = _real_live_fixture()
    live["envs"].append(
        {"key": "SERVICE_ROLE_NAME", "scope": "RUN_TIME", "value": "service_worker_v2"}
    )
    live["services"][0]["envs"].append(
        {"key": "SERVICE_ROLE_NAME", "scope": "RUN_TIME", "value": "service_worker_v2"}
    )
    errs = validate_cutover(built.spec, _candidate(), live, "service_worker_v2")
    assert any("structural drift" in e for e in errs), errs


def test_malformed_live_role_declaration_aborts(tmp_path: Path) -> None:
    secrets_dir = _full_secrets_dir(tmp_path)
    built = build(_candidate(), _real_live_fixture(), secrets_dir)
    assert built.spec is not None
    live = _real_live_fixture()
    live["envs"].append({"key": "SERVICE_ROLE_NAME", "scope": "RUN_TIME", "value": "sw-evil"})
    errs = validate_cutover(built.spec, _candidate(), live, "service_worker")
    assert any("malformed" in e for e in errs), errs


# ── round 7 finding 1: EMPTY live role declarations fail CLOSED, in every scope ──
# A present-but-empty SERVICE_ROLE_NAME is drift/misconfiguration, never "legacy":
# treating it as absent would let the anti-revert version comparison silently skip.
def _live_with_declaration_at(scope: str, value: object) -> dict:
    live = _real_live_fixture()
    entry: dict = {"key": "SERVICE_ROLE_NAME", "scope": "RUN_TIME"}
    if value is not ...:  # ... means "declare the key with NO value field at all"
        entry["value"] = value
    if scope == "app-level":
        live["envs"].append(entry)
    elif scope == "services":
        live["services"][0]["envs"].append(entry)
    else:  # workers / jobs — absent from the fixture; add a minimal component
        live[scope] = [{"name": f"fake-{scope[:-1]}", "envs": [entry]}]
    return live


@pytest.mark.parametrize("scope", ["app-level", "services", "workers", "jobs"])
@pytest.mark.parametrize("value", ["", "   ", None, ...], ids=["empty", "ws", "null", "no-field"])
def test_empty_live_role_declaration_fails_closed_in_every_scope(scope: str, value: object) -> None:
    errs, role = validate_live_role_declarations(_live_with_declaration_at(scope, value))
    assert role is None
    assert any("EMPTY" in e and scope in e for e in errs), (scope, value, errs)


@pytest.mark.parametrize("scope", ["app-level", "services", "workers", "jobs"])
def test_empty_declaration_aborts_the_full_cutover_validator(scope: str, tmp_path: Path) -> None:
    """End-to-end wiring: validate_cutover must refuse when the LIVE spec carries an
    empty declaration in ANY scope — never proceed as if the live state were legacy."""
    secrets_dir = _full_secrets_dir(tmp_path)
    built = build(_candidate(), _real_live_fixture(), secrets_dir)
    assert built.spec is not None
    errs = validate_cutover(
        built.spec, _candidate(), _live_with_declaration_at(scope, ""), "service_worker"
    )
    assert any("EMPTY" in e for e in errs), (scope, errs)


def test_valid_plus_empty_duplicate_declaration_aborts() -> None:
    """A valid app-level declaration must NOT rescue an empty component-level duplicate:
    the pair is drift and must abort, not resolve to the valid value."""
    live = _real_live_fixture()
    live["envs"].append(
        {"key": "SERVICE_ROLE_NAME", "scope": "RUN_TIME", "value": "service_worker_v2"}
    )
    live["services"][0]["envs"].append(
        {"key": "SERVICE_ROLE_NAME", "scope": "RUN_TIME", "value": ""}
    )
    errs, role = validate_live_role_declarations(live)
    assert role is None
    assert any("EMPTY" in e for e in errs), errs


def test_zero_declarations_is_legacy_and_one_valid_is_accepted() -> None:
    assert validate_live_role_declarations(_real_live_fixture()) == ([], None)
    live = _live_with_declaration_at("app-level", "service_worker_v2")
    assert validate_live_role_declarations(live) == ([], "service_worker_v2")
