"""scripts.retire_verify — the execution-bound, deployment-bound retirement gate.

These tests run the gate against a FAKE `doctl` executable (a real subprocess — the
same invocation path production uses) driven by a scripted state machine, so they prove
the operational transition, not markdown. The fake serves each query from a per-key
SEQUENCE, so tests can move the world mid-verification (a new deployment appears, the
active id changes, the spec drifts) and prove the gate refuses. It also logs every
call: queries that are not deployment-bound (`spec get` / `logs` without
`--deployment <id>`) have no state key and fail — an unbound fetch cannot even
succeed against the fake.

Round-7 mutation targets these tests must catch:
  - removing any post-evidence RE-CHECK (in-progress / active id / phase / digest);
  - unbinding the spec or log fetches from the proven deployment id;
  - any path to a "passed" component without that component's own execution evidence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.retire_verify import main as retire_main
from scripts.retire_verify import verify

SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
ROLE = "service_worker_v2"

if sys.platform == "win32":  # pragma: no cover
    pytest.skip("POSIX executables required for the fake doctl", allow_module_level=True)


# ── the fake doctl ────────────────────────────────────────────────────────────
_FAKE_DOCTL = f"""#!{sys.executable}
import json, pathlib, sys
state_dir = pathlib.Path(__file__).resolve().parent
state = json.loads((state_dir / "state.json").read_text())
args = sys.argv[1:]
with open(state_dir / "calls.log", "a") as f:
    f.write(" ".join(args) + chr(10))

def key_for(a):
    if a[:2] == ["apps", "get"] and "--format" in a:
        fmt = a[a.index("--format") + 1]
        if fmt == "InProgressDeployment.ID":
            return "in_progress"
        if fmt == "ActiveDeployment.ID":
            return "active"
    if a[:2] == ["apps", "get-deployment"]:
        return "phase:" + a[3]
    if a[:3] == ["apps", "spec", "get"]:
        # NO fallback: a spec fetch without --deployment has no state key -> hard fail.
        if "--deployment" in a:
            return "spec:" + a[a.index("--deployment") + 1]
        return "spec:UNBOUND"
    if a[:2] == ["apps", "logs"]:
        dep = a[a.index("--deployment") + 1] if "--deployment" in a else "UNBOUND"
        return "logs:" + dep + ":" + a[3]
    return None

key = key_for(args)
if key is None or key not in state:
    sys.stderr.write("fake-doctl: no scripted response for: " + " ".join(args) + chr(10))
    sys.exit(2)
seq = state[key]
counter = state_dir / ("count." + key.replace(":", "_").replace("/", "_"))
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
item = seq[min(n, len(seq) - 1)]
if item == "<<FAIL>>":
    sys.exit(1)
sys.stdout.write(item)
"""


_PLACEHOLDER = "${_self.COMMIT_HASH}"  # the ONLY SOURCE_COMMIT value that is evidence
_OMIT = object()  # sentinel: leave the field out of the record entirely


def _spec_text(
    role: str | None = ROLE,
    flag: str | None = "true",
    workers: tuple[str, ...] = ("inbox-worker", "reconciliation-worker"),
    name: str | None = "reorderos-staging",
    services: tuple[str, ...] = ("api",),
    source_commit: str | None = _PLACEHOLDER,  # per-component value; None = omit
    app_level_source_commit: str | None = None,
) -> str:
    envs = []
    if role is not None:
        envs.append({"key": "SERVICE_ROLE_NAME", "scope": "RUN_TIME", "value": role})
    if flag is not None:
        envs.append({"key": "RESTRICTED_RUNTIME_ROLES_ENABLED", "scope": "RUN_TIME", "value": flag})
    if app_level_source_commit is not None:
        envs.append({"key": "SOURCE_COMMIT", "scope": "RUN_TIME", "value": app_level_source_commit})

    def _component(comp_name: str) -> dict:
        comp_envs = []
        if source_commit is not None:
            comp_envs.append({"key": "SOURCE_COMMIT", "scope": "RUN_TIME", "value": source_commit})
        return {"name": comp_name, "envs": comp_envs}

    spec: dict = {
        "envs": envs,
        "services": [_component(s) for s in services],
        "workers": [_component(w) for w in workers],
    }
    if name is not None:
        spec["name"] = name
    return yaml.safe_dump(spec)


def _api_log(service_user: str = ROLE, request_user: str = "reorderos_app") -> str:
    return (
        json.dumps({"event": "role.ok", "request_user": request_user, "service_user": service_user})
        + "\n"
    )


def _worker_log(worker: str, sha: str = SHA, service_user: object = ROLE) -> str:
    """A worker `.starting` record. service_user=_OMIT drops the field (a worker that
    never logs its assertion result); None serializes as null (assertion disabled)."""
    rec: dict = {"event": f"{worker.replace('-', '_')}.starting", "source_commit": sha}
    if service_user is not _OMIT:
        rec["service_user"] = service_user
    return json.dumps(rec) + "\n"


def _default_state(
    active: str = "dep-1",
    spec: str | list[str] | None = None,
    in_progress: str | list[str] = "",
    phase: str | list[str] = "ACTIVE",
    workers: tuple[str, ...] = ("inbox-worker", "reconciliation-worker"),
    api_log: str | None = None,
    worker_logs: dict[str, str] | None = None,
) -> dict:
    def seq(v: str | list[str]) -> list[str]:
        return v if isinstance(v, list) else [v]

    first = seq(active)[0]  # per-deployment keys belong to the INITIALLY-active id
    state = {
        "in_progress": seq(in_progress),
        "active": seq(active),
        f"phase:{first}": seq(phase),
        f"spec:{first}": seq(spec if spec is not None else _spec_text(workers=workers)),
        f"logs:{first}:api": [api_log if api_log is not None else _api_log()],
    }
    for w in workers:
        state[f"logs:{first}:{w}"] = [(worker_logs or {}).get(w, _worker_log(w))]
    return state


@pytest.fixture
def fake_doctl(tmp_path: Path):
    """Returns (doctl_path, set_state, calls) — calls() re-reads the call log."""
    doctl = tmp_path / "doctl"
    doctl.write_text(_FAKE_DOCTL)
    doctl.chmod(0o755)

    def set_state(state: dict) -> None:
        (tmp_path / "state.json").write_text(json.dumps(state))
        for stale in tmp_path.glob("count.*"):
            stale.unlink()
        (tmp_path / "calls.log").write_text("")

    def calls() -> list[str]:
        return (tmp_path / "calls.log").read_text().splitlines()

    return str(doctl), set_state, calls


def _verify(doctl: str) -> tuple[list[str], str | None]:
    return verify("app-x", ROLE, SHA, doctl=doctl)


# ── happy path: everything bound to ONE deployment, rechecked ─────────────────
def test_happy_path_returns_the_proven_deployment_id(fake_doctl) -> None:
    doctl, set_state, calls = fake_doctl
    set_state(_default_state())
    assert _verify(doctl) == ([], "dep-1")
    log = calls()
    # every spec/log fetch was DEPLOYMENT-BOUND (the fake would have failed otherwise —
    # assert anyway so a fake regression can't mask an unbinding mutation):
    assert any("spec get app-x --deployment dep-1" in line for line in log)
    for comp in ("api", "inbox-worker", "reconciliation-worker"):
        assert any(f"logs app-x {comp}" in line and "--deployment dep-1" in line for line in log)
    # the RE-CHECK exists: in-progress, active id, phase, and spec each queried TWICE.
    assert sum("InProgressDeployment.ID" in line for line in log) == 2
    assert sum("ActiveDeployment.ID" in line for line in log) == 2
    assert sum("get-deployment app-x dep-1" in line for line in log) == 2
    assert sum("spec get app-x --deployment dep-1" in line for line in log) == 2


# ── movement mid-proof: every recheck refuses ─────────────────────────────────
def test_in_progress_deployment_at_start_refuses(fake_doctl) -> None:
    doctl, set_state, _ = fake_doctl
    set_state(_default_state(in_progress="dep-9"))
    errors, active = _verify(doctl)
    assert active is None and any("in-progress deployment exists" in e for e in errors)


def test_in_progress_deployment_appearing_during_proof_refuses(fake_doctl) -> None:
    doctl, set_state, _ = fake_doctl
    set_state(_default_state(in_progress=["", "dep-9"]))
    errors, active = _verify(doctl)
    assert active is None
    assert any("appeared during verification" in e for e in errors)


def test_active_id_changing_during_proof_refuses(fake_doctl) -> None:
    doctl, set_state, _ = fake_doctl
    set_state(_default_state(active=["dep-1", "dep-2"]))
    errors, active = _verify(doctl)
    assert active is None
    assert any("active deployment changed during verification" in e for e in errors)


def test_phase_leaving_active_during_proof_refuses(fake_doctl) -> None:
    doctl, set_state, _ = fake_doctl
    set_state(_default_state(phase=["ACTIVE", "SUPERSEDED"]))
    errors, active = _verify(doctl)
    assert active is None and any("no longer ACTIVE" in e for e in errors)


def test_spec_digest_changing_during_proof_refuses(fake_doctl) -> None:
    doctl, set_state, _ = fake_doctl
    drifted = _spec_text() + "# drift\n"
    set_state(_default_state(spec=[_spec_text(), drifted]))
    errors, active = _verify(doctl)
    assert active is None and any("spec digest changed" in e for e in errors)


def test_not_active_at_start_refuses(fake_doctl) -> None:
    doctl, set_state, _ = fake_doctl
    set_state(_default_state(phase="PENDING"))
    errors, active = _verify(doctl)
    assert active is None and any("not ACTIVE" in e for e in errors)


# ── the deployment-bound spec must authorize retirement ───────────────────────
def test_wrong_bound_role_refuses(fake_doctl) -> None:
    doctl, set_state, _ = fake_doctl
    set_state(_default_state(spec=_spec_text(role="service_worker_v3")))
    errors, active = _verify(doctl)
    assert active is None
    assert any("retirement authorization refused" in e for e in errors)


def test_empty_role_declaration_in_bound_spec_refuses(fake_doctl) -> None:
    doctl, set_state, _ = fake_doctl
    set_state(_default_state(spec=_spec_text(role="")))
    errors, active = _verify(doctl)
    assert active is None and any("EMPTY" in e for e in errors)


def test_missing_role_declaration_refuses(fake_doctl) -> None:
    """A spec with NO declaration is legacy — it cannot authorize retiring anything."""
    doctl, set_state, _ = fake_doctl
    set_state(_default_state(spec=_spec_text(role=None)))
    errors, active = _verify(doctl)
    assert active is None
    assert any("binds service role None" in e for e in errors)


def test_flag_not_true_refuses(fake_doctl) -> None:
    """Without RESTRICTED_RUNTIME_ROLES_ENABLED=true the startup lines prove nothing."""
    doctl, set_state, _ = fake_doctl
    for flag in ("false", None):
        set_state(_default_state(spec=_spec_text(flag=flag)))
        errors, active = _verify(doctl)
        assert active is None
        assert any("RESTRICTED_RUNTIME_ROLES_ENABLED=true" in e for e in errors), flag


def test_empty_or_invalid_bound_spec_refuses(fake_doctl) -> None:
    doctl, set_state, _ = fake_doctl
    set_state(_default_state(spec=_spec_text(name=None)))
    errors, active = _verify(doctl)
    assert active is None and any("empty/invalid" in e for e in errors)


# ── execution evidence: no component passes without its own proof ─────────────
def test_worker_without_starting_evidence_refuses_and_names_it(fake_doctl) -> None:
    doctl, set_state, _ = fake_doctl
    set_state(_default_state(worker_logs={"reconciliation-worker": "no json here\n"}))
    errors, active = _verify(doctl)
    assert active is None
    assert any("reconciliation-worker" in e and "unproven" in e for e in errors)


def test_worker_with_wrong_sha_refuses(fake_doctl) -> None:
    doctl, set_state, _ = fake_doctl
    set_state(
        _default_state(worker_logs={"inbox-worker": _worker_log("inbox-worker", sha="b" * 40)})
    )
    errors, active = _verify(doctl)
    assert active is None and any("inbox-worker" in e for e in errors)


def test_empty_component_logs_cannot_pass(fake_doctl) -> None:
    """The manual-attestation replacement in one line: NO evidence, NO pass — there is
    no side channel (typed list, env var, file) that can mark a component passed."""
    doctl, set_state, _ = fake_doctl
    set_state(
        _default_state(api_log="", worker_logs={"inbox-worker": "", "reconciliation-worker": ""})
    )
    errors, active = _verify(doctl)
    assert active is None
    joined = "\n".join(errors)
    assert "api role.ok is None" in joined
    assert "inbox-worker" in joined and "reconciliation-worker" in joined


def test_api_role_ok_with_old_service_worker_refuses(fake_doctl) -> None:
    doctl, set_state, _ = fake_doctl
    set_state(_default_state(api_log=_api_log(service_user="service_worker")))
    errors, active = _verify(doctl)
    assert active is None and any("api role.ok" in e for e in errors)


def test_every_worker_in_inventory_needs_evidence_not_just_known_names(fake_doctl) -> None:
    """A NEW worker added to the spec is automatically part of the passed-set contract:
    with no scripted logs for it, the gate must fail (fail-closed on the unknown)."""
    doctl, set_state, _ = fake_doctl
    state = _default_state(
        spec=_spec_text(workers=("inbox-worker", "brand-new-worker")),
        workers=("inbox-worker", "brand-new-worker"),
    )
    del state["logs:dep-1:brand-new-worker"]  # doctl logs will exit nonzero for it
    set_state(state)
    errors, active = _verify(doctl)
    assert active is None
    assert any("brand-new-worker" in e and "failed" in e for e in errors)


# ── input gates: no doctl call before the request itself is valid ─────────────
def test_unversioned_or_invalid_role_is_rejected_before_any_doctl_call(fake_doctl) -> None:
    doctl, set_state, calls = fake_doctl
    set_state(_default_state())
    for bad_role in ("service_worker", "doadmin", "service_worker_v0", ""):
        errors, active = verify("app-x", bad_role, SHA, doctl=doctl)
        assert active is None and errors, bad_role
    errors, active = verify("app-x", ROLE, "unknown", doctl=doctl)
    assert active is None and any("SHA is empty/unknown" in e for e in errors)
    assert calls() == []  # every rejection above happened before ANY doctl call


def test_doctl_failure_is_an_error_not_a_pass(fake_doctl) -> None:
    doctl, set_state, _ = fake_doctl
    set_state({"in_progress": ["<<FAIL>>"]})
    errors, active = _verify(doctl)
    assert active is None and any("failed" in e for e in errors)


# ── CLI contract: stdout carries ONLY the proven id ───────────────────────────
def _run_cli(doctl: str, extra: list[str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.retire_verify",
            "--app",
            "app-x",
            "--role",
            ROLE,
            "--sha",
            SHA,
            "--doctl",
            doctl,
            *(extra or []),
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
    )


def test_cli_success_prints_only_the_deployment_id(fake_doctl) -> None:
    doctl, set_state, _ = fake_doctl
    set_state(_default_state())
    proc = _run_cli(doctl)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "dep-1\n"  # exactly consumable by ACTIVE_ID=$(...)
    assert "evidence complete" in proc.stderr
    # even a PASS is an audit record only — the CLI itself restates that retirement
    # (NOLOGIN) remains unsupported (round-8 finding 4):
    assert "UNSUPPORTED" in proc.stderr


def test_cli_failure_exits_nonzero_with_empty_stdout(fake_doctl) -> None:
    doctl, set_state, _ = fake_doctl
    set_state(_default_state(active=["dep-1", "dep-2"]))
    proc = _run_cli(doctl)
    assert proc.returncode == 1
    assert proc.stdout == ""  # a failed gate must never emit a consumable id
    assert "evidence INCOMPLETE" in proc.stderr


def test_cli_main_direct_failure_path(fake_doctl, capsys: pytest.CaptureFixture[str]) -> None:
    doctl, set_state, _ = fake_doctl
    set_state(_default_state(in_progress="dep-9"))
    rc = retire_main(["--app", "app-x", "--role", ROLE, "--sha", SHA, "--doctl", doctl])
    captured = capsys.readouterr()
    assert rc == 1 and captured.out == "" and "retire_verify FAIL" in captured.err


# ── /version binding (network stubbed at the module seam) ─────────────────────
def test_version_mismatch_refuses(fake_doctl, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.retire_verify as rv

    doctl, set_state, _ = fake_doctl
    set_state(_default_state())
    monkeypatch.setattr(rv, "_fetch_version_commit", lambda base_url: "f" * 40)
    errors, active = rv.verify("app-x", ROLE, SHA, doctl=doctl, base_url="http://x")
    assert active is None
    assert any("/version commit does not equal" in e for e in errors)
    monkeypatch.setattr(rv, "_fetch_version_commit", lambda base_url: SHA)
    assert rv.verify("app-x", ROLE, SHA, doctl=doctl, base_url="http://x") == ([], "dep-1")


# ── round 8 finding 1: SHA evidence must be bound to the platform placeholder ──
def test_literal_source_commit_cannot_certify_even_when_logs_echo_it(fake_doctl) -> None:
    """The spoof scenario verbatim: the bound spec pins SOURCE_COMMIT to the LITERAL
    expected SHA, so /version and every start line echo it perfectly — and the gate
    must still refuse, because an echoed literal proves nothing about the running
    code. Only ${_self.COMMIT_HASH} is evidence."""
    doctl, set_state, _ = fake_doctl
    set_state(_default_state(spec=_spec_text(source_commit=SHA)))
    errors, active = _verify(doctl)
    assert active is None
    assert any("SOURCE_COMMIT" in e and "self-referential" in e for e in errors), errors


def test_missing_component_source_commit_refuses(fake_doctl) -> None:
    doctl, set_state, _ = fake_doctl
    set_state(_default_state(spec=_spec_text(source_commit=None)))
    errors, active = _verify(doctl)
    assert active is None
    assert any("missing component-level SOURCE_COMMIT" in e for e in errors), errors


def test_app_level_source_commit_refuses(fake_doctl) -> None:
    """An app-level declaration is forbidden even alongside correct component-level
    placeholders — ${_self...} resolves per-component only."""
    doctl, set_state, _ = fake_doctl
    set_state(_default_state(spec=_spec_text(app_level_source_commit=_PLACEHOLDER)))
    errors, active = _verify(doctl)
    assert active is None
    assert any("app-level" in e and "SOURCE_COMMIT" in e for e in errors), errors


# ── round 8 finding 2: the starting record must carry the ASSERTED role ───────
def test_worker_starting_without_service_user_refuses(fake_doctl) -> None:
    """A NEW worker with a plausible `.starting` record (right event, right SHA) but no
    service_user field must fail — it never logged its assertion result."""
    doctl, set_state, _ = fake_doctl
    set_state(
        _default_state(
            worker_logs={"inbox-worker": _worker_log("inbox-worker", service_user=_OMIT)}
        )
    )
    errors, active = _verify(doctl)
    assert active is None
    assert any("inbox-worker" in e and "service_user" in e for e in errors), errors


def test_worker_starting_with_null_service_user_refuses(fake_doctl) -> None:
    """service_user null == the assertion was disabled when the worker started."""
    doctl, set_state, _ = fake_doctl
    set_state(
        _default_state(
            worker_logs={
                "reconciliation-worker": _worker_log("reconciliation-worker", service_user=None)
            }
        )
    )
    errors, active = _verify(doctl)
    assert active is None
    assert any("reconciliation-worker" in e and "service_user" in e for e in errors), errors


def test_worker_starting_with_wrong_service_user_refuses(fake_doctl) -> None:
    """A worker that authenticated as the OLD role (or any other role) is exactly what
    retirement evidence must catch."""
    doctl, set_state, _ = fake_doctl
    set_state(
        _default_state(
            worker_logs={"inbox-worker": _worker_log("inbox-worker", service_user="service_worker")}
        )
    )
    errors, active = _verify(doctl)
    assert active is None
    assert any("inbox-worker" in e for e in errors), errors


# ── round 8 finding 3: unproven services fail closed ──────────────────────────
@pytest.mark.parametrize(
    "services",
    [("api", "admin-dashboard"), ("",), ("api", "api"), ("admin-dashboard",)],
    ids=["additional", "unnamed", "duplicate-api", "no-api"],
)
def test_service_inventory_must_be_exactly_api(fake_doctl, services) -> None:
    doctl, set_state, _ = fake_doctl
    set_state(_default_state(spec=_spec_text(services=services)))
    errors, active = _verify(doctl)
    assert active is None
    assert any("exactly ['api']" in e for e in errors), (services, errors)


def test_unnamed_or_duplicate_workers_refuse(fake_doctl) -> None:
    doctl, set_state, _ = fake_doctl
    for workers in (("inbox-worker", "inbox-worker"), ("inbox-worker", "")):
        state = _default_state(
            spec=_spec_text(workers=workers), workers=tuple(dict.fromkeys(workers))
        )
        set_state(state)
        errors, active = _verify(doctl)
        assert active is None
        assert any("unnamed or" in e and "duplicate" in e.replace("\n", " ") for e in errors), (
            workers,
            errors,
        )


# ── round 8 finding 2: production-path (AST) contract for all four workers ────
_WORKER_MODULES = (
    "inbox_worker",
    "reconciliation_worker",
    "receipt_extraction_worker",
    "inbound_email_worker",
)


@pytest.mark.parametrize("module_name", _WORKER_MODULES)
def test_worker_source_assigns_assertion_result_and_logs_it(module_name: str) -> None:
    """AST contract: each worker AWAITS assert_service_pool_role_if_enabled(), ASSIGNS
    the result to service_user, and passes service_user=service_user into its
    `<module>.starting` log call, with the assignment PRECEDING the log. This pins the
    production path that makes the retire_verify evidence meaningful — remove the
    assignment, the kwarg, or reorder them and this fails."""
    import ast

    src_path = Path(__file__).resolve().parents[1] / "app" / "workers" / f"{module_name}.py"
    tree = ast.parse(src_path.read_text())

    assign_line: int | None = None
    log_line: int | None = None
    logs_result = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Await):
            call = node.value.value
            func = call.func if isinstance(call, ast.Call) else None
            fname = getattr(func, "id", None) or getattr(func, "attr", None)
            if fname == "assert_service_pool_role_if_enabled" and any(
                isinstance(t, ast.Name) and t.id == "service_user" for t in node.targets
            ):
                assign_line = node.lineno
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "info"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and str(node.args[0].value) == f"{module_name}.starting"
        ):
            log_line = node.lineno
            logs_result = any(
                kw.arg == "service_user"
                and isinstance(kw.value, ast.Name)
                and kw.value.id == "service_user"
                for kw in node.keywords
            )
    assert assign_line is not None, f"{module_name}: assertion result is not assigned"
    assert log_line is not None, f"{module_name}: no `.starting` log call found"
    assert assign_line < log_line, f"{module_name}: `.starting` logged BEFORE the role assertion"
    assert logs_result, f"{module_name}: `.starting` does not log service_user=service_user"
