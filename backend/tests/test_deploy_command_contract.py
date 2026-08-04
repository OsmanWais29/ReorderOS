"""Deploy command-contract + schema-gate regression tests.

The startup/migration contract is asymmetric BY DESIGN:

  PRODUCTION (unchanged, current behavior):
    - The live production app has NO PRE_DEPLOY migrate job, and no automation applies the
      repository spec (.github/workflows/deploy.yml only VALIDATES it) — so the committed
      production spec must not depend on one.
    - The api uses the Dockerfile DEFAULT command: `alembic upgrade head && exec uvicorn …`
      (migrate-then-serve on the bound admin DATABASE_URL). Removing the alembic step from
      the default would leave production's schema behind and the api would refuse to boot.
    - `RESTRICTED_RUNTIME_ROLES_ENABLED` is declared false.

  STAGING (restricted-role cutover candidate):
    - The api OVERRIDES the Dockerfile default with a Uvicorn-only `run_command` — the
      restricted `reorderos_app` role has no CREATE, so the alembic step must not run there.
    - Migrations run in exactly ONE PRE_DEPLOY `migrate` job, as the admin role.
    - `RESTRICTED_RUNTIME_ROLES_ENABLED` is declared true.

Plus the behavioral schema-head gate (pass at head / fail closed behind) and the runbook
execution contract (documented commands actually run from their documented directory).
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest
import yaml

_BACKEND = os.path.dirname(os.path.dirname(__file__))
_DO_DIR = os.path.join(_BACKEND, "..", ".do")
_RUNBOOK = pathlib.Path(_BACKEND, "docs", "security", "restricted-runtime-role-runbook.md")


def _spec(name: str) -> dict:
    return yaml.safe_load(pathlib.Path(_DO_DIR, name).read_text())


def _app_env_value(spec: dict, key: str) -> str | None:
    for e in spec.get("envs") or []:
        if e.get("key") == key:
            return str(e.get("value"))
    return None


def _dockerfile_cmd() -> str:
    src = pathlib.Path(_BACKEND, "Dockerfile").read_text()
    # last CMD instruction (ignore HEALTHCHECK … CMD)
    cmds = re.findall(r"^CMD\s+(.+)$", src, flags=re.MULTILINE)
    assert cmds, "no CMD in Dockerfile"
    return cmds[-1]


# ── production contract ───────────────────────────────────────────────────────
def test_dockerfile_default_cmd_is_migrate_then_uvicorn() -> None:
    """PRODUCTION SAFETY: the Dockerfile default MUST keep migrate-then-serve. The live
    production app has no PRE_DEPLOY migrate job and nothing installs one automatically —
    a Uvicorn-only default would leave prod's schema behind and _assert_schema_at_head
    would refuse startup. Fails against a Uvicorn-only CMD."""
    cmd = _dockerfile_cmd()
    assert "alembic upgrade head" in cmd, (
        "Dockerfile default CMD no longer migrates — production relies on the api container "
        "running `alembic upgrade head` before uvicorn (no live PRE_DEPLOY job exists)."
    )
    assert "uvicorn" in cmd, cmd
    assert cmd.index("alembic") < cmd.index("uvicorn"), "migration must run BEFORE uvicorn"


def test_prod_spec_does_not_depend_on_predeploy_job() -> None:
    """The committed production spec must NOT declare a PRE_DEPLOY migration architecture:
    the deploy workflow never applies the repository spec (`doctl apps spec validate` is not
    `doctl apps update`), so a jobs: block here would be dead configuration that invites the
    false belief that merging installs it. Production migrates via the Dockerfile default."""
    spec = _spec("app.yaml")
    assert not spec.get("jobs"), (
        "production spec declares jobs — the live app has none and no automation applies "
        "this file; production's migration contract is the Dockerfile default CMD"
    )
    for svc in spec.get("services") or []:
        assert "run_command" not in svc, (
            f"production service {svc.get('name')} overrides run_command — production must "
            f"use the Dockerfile default (migrate-then-serve)"
        )


def test_prod_spec_declares_restricted_roles_false() -> None:
    """Production must keep booting on its admin-bound DATABASE_URL: the committed prod
    spec explicitly declares the cutover flag false (and thereby documents that the role
    assertions do NOT run there)."""
    assert _app_env_value(_spec("app.yaml"), "RESTRICTED_RUNTIME_ROLES_ENABLED") == "false"


def test_prod_spec_declares_flags_matching_worker_set() -> None:
    """The committed prod spec declares CLOVER_ENABLED=true and carries BOTH Clover workers
    (a present worker with its flag off exits '.disabled' and fails the whole DO deploy)."""
    spec = _spec("app.yaml")
    assert _app_env_value(spec, "CLOVER_ENABLED") == "true"
    workers = {w.get("name") for w in spec.get("workers") or []}
    assert {"inbox-worker", "reconciliation-worker"} <= workers


# ── staging contract ──────────────────────────────────────────────────────────
def test_staging_api_run_command_is_uvicorn_only() -> None:
    """STAGING CUTOVER: the api must override the Dockerfile default with Uvicorn-only —
    under the non-CREATE reorderos_app role the default's alembic step would fail the
    DDL-capability preflight and the container would never boot."""
    spec = _spec("staging.app.yaml")
    api = next(s for s in spec["services"] if s["name"] == "api")
    rc = api.get("run_command") or ""
    assert "uvicorn" in rc, "staging api must declare a run_command starting uvicorn"
    assert "alembic" not in rc, (
        "staging api run_command runs alembic — reorderos_app has no CREATE; migrations "
        "belong to the PRE_DEPLOY migrate job"
    )


def test_staging_spec_migration_only_in_predeploy_job() -> None:
    """Staging runs `alembic upgrade head` in exactly one PRE_DEPLOY job and nowhere else."""
    spec = _spec("staging.app.yaml")
    migrate = [
        j
        for j in spec.get("jobs") or []
        if j.get("kind") == "PRE_DEPLOY" and "alembic upgrade head" in (j.get("run_command") or "")
    ]
    assert len(migrate) == 1, (
        f"staging: expected exactly one PRE_DEPLOY job running alembic, got {len(migrate)}"
    )
    for svc in spec.get("services") or []:
        assert "alembic" not in (svc.get("run_command") or "")
    for wrk in spec.get("workers") or []:
        assert "alembic" not in (wrk.get("run_command") or "")


def test_staging_spec_declares_restricted_roles_true() -> None:
    """The staging candidate is the cutover: role assertions must be armed (fail-closed)."""
    assert _app_env_value(_spec("staging.app.yaml"), "RESTRICTED_RUNTIME_ROLES_ENABLED") == "true"


# ── behavioral schema-head gate ───────────────────────────────────────────────
async def test_assert_schema_at_head_passes_at_head() -> None:
    """At head, the API's schema gate passes. It reads alembic_version via a plain SELECT
    (granted to app_user by 0035) — i.e. it needs NO CREATE, so it works under reorderos_app
    (membership read itself is covered by
    test_restricted_role_characterization.test_schema_head_readable_under_reorderos_app)."""
    from app.main import _assert_schema_at_head

    await _assert_schema_at_head()  # DB is at head in the test env → no raise


async def test_assert_schema_at_head_fails_closed_when_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behind head → the API refuses to serve. In production this guarantees the migration
    (Dockerfile default in prod, PRE_DEPLOY job in staging) actually reached head."""
    from alembic.script import ScriptDirectory

    from app.main import _assert_schema_at_head

    monkeypatch.setattr(ScriptDirectory, "get_current_head", lambda self: "9999_not_a_real_head")
    with pytest.raises(RuntimeError, match="Schema at"):
        await _assert_schema_at_head()


# ── runbook execution contract ────────────────────────────────────────────────
def test_runbook_establishes_backend_cwd_and_interpreter() -> None:
    """The runbook must establish REPO / cd backend / $PY before any module invocation, and
    every checked-in-module command must be runnable from that directory: `scripts.…` is NOT
    importable from the repository root."""
    text = _RUNBOOK.read_text()
    assert "REPO=/Users/bigvito/Documents/ReorderOS" in text
    assert 'cd "$REPO/backend"' in text
    assert "PY=.venv/bin/python" in text
    # No spec path may be repo-root-relative (.do/...) — from backend/ it must be ../.do/...
    for line in text.splitlines():
        if "scripts.deploy_verify" in line and ".yaml" in line:
            assert "../.do/" in line, f"runbook deploy_verify line not backend-relative: {line!r}"
            assert '"$PY" -m scripts.deploy_verify' in line, (
                f'runbook must invoke the verifier via "$PY" -m from backend/: {line!r}'
            )


def test_runbook_waits_are_bounded_and_gates_exit_nonzero() -> None:
    """The runbook must use the ONE bounded waiter (ACTIVE / ERROR / CANCELED /
    SUPERSEDED / doctl failure / timeout), never an unbounded poll, run under
    `set -euo pipefail`, and route EVERY failed gate through `abort` (logs captured,
    secret-bearing built specs unlinked, non-zero exit)."""
    text = _RUNBOOK.read_text()
    assert "set -euo pipefail" in text
    # the single bounded waiter exists and handles every terminal state + timeout:
    assert "wait_active()" in text
    assert "ERROR|CANCELED|SUPERSEDED" in text
    assert "not ACTIVE after" in text  # timeout branch
    # no unbounded polling anywhere:
    assert "until [" not in text, "unbounded `until` poll re-introduced in the runbook"
    # every deployment wait goes through the helper:
    assert text.count('wait_active "$DEP_ID"') >= 3  # A0, A1, C (+rollback references)
    # THE abort path exists, unlinks the aggregated secret-bearing artifacts, and is
    # used pervasively (every `|| abort <tag>` failure branch):
    assert "abort() {" in text
    assert text.count("|| abort ") + text.count("|| abort\n") >= 20, (
        "gates must route through abort — found too few `|| abort` branches"
    )
    abort_body = text.split("abort() {", 1)[1][:900]  # the function body region
    assert "cutover_spec.yaml" in abort_body, "abort() must unlink the populated cutover spec"
    assert "post_a0_spec.yaml" in abort_body, "abort() must unlink the post-A0 fetched spec"
    # ROLLBACK ARTIFACTS ARE SACRED (round-4 P1): abort must never delete them —
    # only cleanup_secrets (completion / verified rollback) may.
    assert "rollback_" not in abort_body.split("rm -f", 1)[1].split("echo", 1)[0], (
        "abort() deletes rollback artifacts — the only safe path back would be destroyed"
    )
    cleanup_body = text.split("cleanup_secrets() {", 1)[1][:1100]
    assert "post_a0_spec.yaml" in cleanup_body
    # ROUND-7 FINDING 4: sensitive-material cleanup is SPLIT from rollback-artifact
    # retirement. cleanup_secrets' rm list must never touch rollback artifacts; deletion
    # happens ONLY via cleanup_rollback_artifacts, per explicitly-named file.
    cleanup_rm = cleanup_body.split("rm -f", 1)[1].split("echo", 1)[0]
    assert "rollback_" not in cleanup_rm, (
        "cleanup_secrets deletes rollback artifacts — retention must be a separate, "
        "founder-approved decision (cleanup_rollback_artifacts)"
    )
    assert "cleanup_rollback_artifacts() {" in text
    rb_cleanup = text.split("cleanup_rollback_artifacts() {", 1)[1][:900]
    # ROUND-8 FINDING 5: the function accepts artifact TAGS, never paths — the shell
    # expands globs before any function sees its argv, so path-based "no wildcard"
    # rules are an operator contract only; the tag check makes glob expansions of
    # artifact FILES fail structurally ("/" and "." are not tag characters).
    assert "name each artifact TAG explicitly" in rb_cleanup  # no argless invocation
    assert "not a bare artifact tag" in rb_cleanup  # tag allow-list, paths/globs rejected
    assert "*[!a-z0-9_]*" in rb_cleanup  # the structural tag pattern itself
    assert "rollback_${tag}_spec.yaml" in rb_cleanup  # function builds the path itself
    assert "OPERATOR CONTRACT" in text  # the shell-expansion caveat is documented
    # no wildcard rm of rollback artifacts anywhere in the runbook:
    assert "rm -f ~/.reorderos-cutover/rollback_" not in text
    assert 'rm -f "$HOME"/.reorderos-cutover/rollback_' not in text
    # rollback_retire_spec.yaml retention is documented with explicit invalidation
    # conditions and a founder-approval requirement:
    assert "rollback_retire_spec.yaml" in text and "MUST be preserved" in text
    assert "founder confirms one of those conditions in writing" in text
    # STOP always means a non-zero path — no bare STOP echo without exit/return/abort:
    for line in text.splitlines():
        if "— STOP" in line and "echo" in line and "wait_active" not in line:
            assert (
                "exit 1" in line
                or "return 1" in line
                or "abort" in line
                or ("capture_rollback" in line)
            ), f"STOP without exit: {line!r}"
    # forensics + secret hygiene:
    assert "capture_logs" in text and "cleanup_secrets" in text


def test_runbook_rollback_uses_pre_active_deployment_artifact() -> None:
    """Round-4 P1: rollback must come from the PRE-ACTIVE deployment's spec fetched via
    `apps spec get --deployment` (immutable artifact), never from a re-fetch of the
    app's latest spec (which after a failed cutover IS the failed cutover spec)."""
    text = _RUNBOOK.read_text()
    assert "capture_rollback() {" in text
    assert 'spec get "$APP" --deployment' in text  # per-deployment fetch
    assert "--format ActiveDeployment.ID --no-header" in text
    capture = text.split("capture_rollback() {", 1)[1].split("capture_logs() {", 1)[0]
    assert 'tmp="${out}.tmp.$$"' in capture
    assert "yaml.safe_load" in capture, "rollback capture must parse/validate before publish"
    assert 'test "$current" = "$id"' in capture, "active deployment must be stable during capture"
    assert 'ln "$tmp" "$out"' in capture, "final rollback artifact must publish atomically"
    assert '> "$out"' not in capture, "never stream directly into the sacred rollback path"
    assert "capture_rollback a0" in text and "capture_rollback c" in text
    # rollback commands reference the immutable artifacts:
    assert "rollback_a0_spec.yaml" in text and "rollback_c_spec.yaml" in text
    # …and no rollback path applies live_spec.yaml (the contaminated re-fetch):
    rollback_section = text.split("## Rollback", 1)[1]
    assert "live_spec.yaml" not in rollback_section
    a0_rollback = text.split("**Rollback A0:**", 1)[1].split("---", 1)[0]
    assert "rollback_a0_spec.yaml" in a0_rollback and "live_spec.yaml" not in a0_rollback


def test_runbook_a0_source_preservation_is_structural_not_vacuous() -> None:
    """Review finding: pre-A1 code reports no /version commit, so an `unknown == unknown`
    SHA comparison at A0 would pass vacuously. A0 must instead prove preservation
    STRUCTURALLY: post-apply live spec fetched and byte-compared to the intended A0
    spec, with no /version-equality gate at A0."""
    text = _RUNBOOK.read_text()
    a0_section = text.split("## Phase A0", 1)[1].split("## Phase A1", 1)[0]
    assert "post_a0_spec" in a0_section  # post-apply fetch...
    assert "post == a0" in a0_section  # ...byte-compared to the intended spec
    assert "vacuous" in a0_section  # the why is documented
    assert 'test "$POST_SHA" = "$PRE_SHA"' not in text  # the vacuous gate is gone
    assert "a0_pre_sha" not in text


def test_runbook_phase_b_is_preflighted_against_the_running_deployment() -> None:
    """Review finding (P1): Phase B must never invalidate a credential the RUNNING
    deployment uses. The runbook must run `role_admin preflight-rotate` for BOTH managed
    roles BEFORE any credential change, and provide the coordinated Phase B-alt (with a
    retained rollback credential) for the in-use case."""
    text = _RUNBOOK.read_text()
    assert text.count("preflight-rotate") >= 3  # B0 runs it for both roles (+tooling ref)
    b_section = text.split("## Phase B", 1)[1]
    assert b_section.index("preflight-rotate") < b_section.index("provision-app"), (
        "the preflight must come BEFORE any provisioning/rotation"
    )
    b0 = b_section.split("**B1/B2", 1)[0]
    assert 'for kind in ("services","workers")' in b0
    assert 'for kind in ("services","workers","jobs")' not in b0
    assert "repeat for api AND every currently-running worker" in b0
    assert 'There is deliberately no "the API represents everything"' in b0
    assert "shortcut: each running service/worker is checked in its own container" in b0
    assert "b0_components.expected" in b0
    assert "b0_components.passed" in b0
    assert "cmp -s ~/.reorderos-cutover/b0_components.expected" in b0
    assert "|| abort b0-incomplete-component-proof" in b0
    assert "Phase B-alt" in text
    assert "Phase B-alt supports an in-use" in text
    assert "`service_worker` only; an unexpected in-use `reorderos_app`" in text
    balt = text.split("### Phase B-alt", 1)[1].split("## Phase C", 1)[0]
    # B-alt is EXECUTABLE (round-5 finding 3): exact labeled commands, secret-safe.
    assert "old `service_worker` login stays valid" in balt
    assert "openssl rand -hex 32 > ~/.reorderos-cutover/service_worker_v2.pw" in balt
    assert "pbcopy < ~/.reorderos-cutover/service_worker_v2.pw" in balt
    assert balt.count("read -s PW; export PW") >= 1
    assert balt.count("unset PW") >= 1
    assert "provision-worker service_worker_v2 \\" in balt or (
        "provision-worker service_worker_v2" in balt
    )
    assert "prove service_worker_v2" in balt
    assert "export SERVICE_ROLE=service_worker_v2" in balt  # THE single selection
    assert "rm -f ~/.reorderos-cutover/secrets/SERVICE_DATABASE_URL" in balt
    # SELF-CONTAINED sequence (round-6 finding 5): eight ordered steps starting from C1
    # file verification — never a jump from B-alt.2 straight into a deployment.
    assert "never jump from B-alt.2 straight into a deployment" in balt
    assert "Finish C1 and verify every required mode-0600 file" in balt
    for step in ("Execute C2", "Execute C3", "Execute C4", "observation window"):
        assert step in balt, f"B-alt.3 missing ordered step {step!r}"
    assert balt.index("Finish C1") < balt.index("export SERVICE_ROLE=service_worker_v2")
    assert balt.index("Execute C2") < balt.index("Execute C3") < balt.index("Execute C4")
    # ROUND-8 FINDING 4: retirement (NOLOGIN) is UNSUPPORTED — no executable admin-DSN
    # console session for it may exist in B-alt:
    assert "read -s ADMIN_DATABASE_URL" not in balt
    assert "B-alt.8" in balt and "UNSUPPORTED" in balt


def _fenced_blocks(text: str) -> list[str]:
    """All fenced code blocks (the runbook's executable surface)."""
    parts = text.split("```")
    return [parts[i] for i in range(1, len(parts), 2)]


def test_runbook_retirement_gate_is_execution_bound_and_nologin_is_unsupported() -> None:
    """Round-7 findings 2+3 + round-8 finding 4: the B-alt.7 gate must be
    machine-verified (scripts.retire_verify: deployment-bound spec fetch, per-component
    execution evidence, movement rechecks) with the retire rollback capture bound to
    the PROVEN deployment id — and the retirement mutation itself must be UNSUPPORTED:
    no fenced (executable) block may disable service_worker's login, because doctl
    offers no lock/exec mechanism that binds the control-plane checks to the DDL.
    Manual per-component attestation must remain GONE from retirement."""
    text = _RUNBOOK.read_text()
    balt = text.split("### Phase B-alt", 1)[1].split("## Phase C", 1)[0]
    retire = balt.split("B-alt.7", 1)[1]
    for marker in (
        "scripts.retire_verify",  # THE gate
        '--role "$RETIRE_ROLE"',  # expected role, machine-sourced…
        "cat ~/.reorderos-cutover/service_role",  # …from the C2-recorded file
        "cat ~/.reorderos-cutover/expected_sha",  # approved SHA
        "|| abort retire-verify",  # gate failure aborts
        '--deployment "$ACTIVE_ID"',  # spec fetched BOUND to the proven deployment
        'capture_rollback retire "$ACTIVE_ID"',  # capture bound to the SAME id
    ):
        assert marker in retire, f"retirement gate missing {marker!r}"
    # MANUAL ATTESTATION MUST NOT EXIST anywhere for retirement (round-7 finding 3):
    # a typed component list proves only that names were typed.
    assert "retire_components" not in text
    assert "RETIRE COMPONENT" not in text
    # the only printf-attestation checklist left is B0's (pre-cutover, documented why):
    assert text.count("printf '%s\\n' '<component-name>'") == 1
    assert "Why B0 keeps a manual console sweep while retirement (B-alt.7) does not" in text
    # the gate's movement rechecks are documented (id/phase/digest + in-progress):
    for phrase in ("no in-progress deployment", "spec digest unchanged"):
        assert phrase in retire, f"retirement gate prose missing {phrase!r}"
    # ROUND-8 FINDING 4 — NOLOGIN for service_worker is UNSUPPORTED and NOT executable:
    # no fenced code block may contain the mutation (R1's reorderos_app rollback NOLOGIN
    # is a different, still-supported path and is allowed).
    for block in _fenced_blocks(text):
        assert not ("service_worker" in block and "NOLOGIN" in block), (
            "an executable block disables service_worker login — B-alt.8 is UNSUPPORTED: "
            "no doctl mechanism binds the control-plane checks to the DDL"
        )
    assert "Keep `service_worker` LOGIN enabled" in retire
    # the race and the honesty requirement are documented, not papered over:
    assert "no deployment lock" in retire
    assert "not zero and it is not atomic" in retire
    assert "Documented residual" in retire


def test_runbook_capture_rollback_accepts_expected_deployment_id() -> None:
    """Round-7 finding 2: when a prior gate proved a specific deployment,
    capture_rollback must bind to it — a moved active id means the proof is stale and
    the capture must refuse."""
    text = _RUNBOOK.read_text()
    capture = text.split("capture_rollback() {", 1)[1].split("capture_logs() {", 1)[0]
    assert "capture_rollback TAG [EXPECTED_ID]" in capture
    assert 'expected="${2:-}"' in capture
    assert '[ "$id" != "$expected" ]' in capture
    assert "STALE" in capture and "return 1" in capture


def test_runbook_c3_gates_are_asserted_not_displayed() -> None:
    """Review finding: role.ok / RuntimeError / tenant smokes were display-only. C3 must
    assert them: role.ok parsed from JSON against the SELECTED role (round-5: never a
    hardcoded tuple), startup errors rejected, membership count and foreign-tenant 403
    asserted."""
    text = _RUNBOOK.read_text()
    c3 = text.split("### C3.", 1)[1].split("### C4.", 1)[0]
    assert "parse_role_ok" in c3
    assert "|| abort c-role-ok" in c3
    # ROLE-DRIVEN, not hardcoded (round-5 finding 4): the expectation is the RECORDED
    # selection, passed as an argument — no literal service_worker tuple anywhere in C3.
    assert 'EXPECT_ROLE="$(cat ~/.reorderos-cutover/service_role)"' in c3
    assert "sys.argv[1]" in c3 and '"$EXPECT_ROLE"' in c3
    assert "('reorderos_app','service_worker')" not in c3
    assert "('reorderos_app', 'service_worker')" not in c3
    assert 'grep -q "request_user=reorderos_app"' not in text
    assert 'grep -q "service_user=service_worker"' not in text
    assert "RuntimeError|env.not_ready" in text and "abort c-startup-error" in text
    assert '[ "$TENANTS" -ge 1 ] || abort' in text
    assert '[ "$CODE" = "403" ] || abort' in text
    # Hidden input alone is insufficient: never expand the bearer token into curl argv.
    assert "smoke_auth.header" in c3
    assert c3.count('-H @"$AUTH_HEADER"') == 2
    assert "Authorization: Bearer $SMOKE_TOKEN" not in c3
    assert c3.index("unset SMOKE_TOKEN") < c3.index("TENANTS=$(curl")


def test_runbook_c2_selects_the_service_role_exactly_once() -> None:
    """Round-5 finding 2: ONE selection point ($SERVICE_ROLE) flows machine-checked into
    the builder, the validator (4th --cutover input), and the recorded service_role file
    that C3 consumes — never retyped, and B-alt only exports the variable."""
    text = _RUNBOOK.read_text()
    c2 = text.split("### C2.", 1)[1].split("### C3.", 1)[0]
    assert 'SERVICE_ROLE="${SERVICE_ROLE:-service_worker}"' in c2
    assert '--service-role "$SERVICE_ROLE"' in c2
    assert (
        "--cutover ~/.reorderos-cutover/cutover_spec.yaml ../.do/staging.app.yaml "
        '~/.reorderos-cutover/live_spec.yaml "$SERVICE_ROLE"' in c2
    )
    # the recorded role comes FROM THE BUILT ARTIFACT, cross-checked vs the selection:
    assert "SERVICE_ROLE_NAME" in c2 and "service_role || abort c-role-record" in c2
    # the candidate itself pins the standard role (single committed source):
    import yaml as _yaml

    spec = _yaml.safe_load(pathlib.Path(_DO_DIR, "staging.app.yaml").read_text())
    role_entries = [e for e in spec.get("envs") or [] if e.get("key") == "SERVICE_ROLE_NAME"]
    assert [str(e.get("value")) for e in role_entries] == ["service_worker"]


def test_runbook_prove_gates_assert_all_dangerous_attributes_and_memberships() -> None:
    """The role-proof gates must assert EVERY dangerous attribute AND the exact
    membership set (review finding 5)."""
    text = _RUNBOOK.read_text()
    for attribute in ("rolcreatedb=False", "rolcreaterole=False", "rolreplication=False"):
        assert text.count(attribute) >= 2, f"runbook EXPECT lines missing {attribute}"
    assert text.count("member_of=app_user admin_option=False") >= 2  # reorderos_app gates
    assert "member_of=(none) admin_option=False" in text  # service_worker gate
    c4 = text.split("### C4.", 1)[1].split("### C5.", 1)[0]
    assert "prove reorderos_app" in c4
    assert "prove service_worker" in c4
    assert c4.count("CONTRACT VIOLATION") >= 2


def test_documented_deploy_verify_commands_run_from_backend() -> None:
    """EXECUTE the documented verifier command exactly as the runbook states (interpreter
    `-m scripts.deploy_verify`, cwd backend, backend-relative spec path). Must exit 0 with
    no ModuleNotFoundError — this fails against the previous repo-root instructions."""
    for rel_spec in ("../.do/staging.app.yaml", "../.do/app.yaml"):
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.deploy_verify", rel_spec],
            cwd=_BACKEND,
            capture_output=True,
            text=True,
        )
        assert "ModuleNotFoundError" not in proc.stderr, proc.stderr
        assert proc.returncode == 0, f"{rel_spec}: {proc.stdout}\n{proc.stderr}"


def test_select_deployment_cli_contract(tmp_path: pathlib.Path) -> None:
    """The runbook's deployment-id capture: exactly one new id passes; zero and multiple
    are hard failures (exit 1) so concurrent deployment activity stops the runbook."""

    def run(before: str, after: str) -> subprocess.CompletedProcess[str]:
        b = tmp_path / "before.txt"
        a = tmp_path / "after.txt"
        b.write_text(before)
        a.write_text(after)
        return subprocess.run(
            [sys.executable, "-m", "scripts.deploy_verify", "--select-deployment", str(b), str(a)],
            cwd=_BACKEND,
            capture_output=True,
            text=True,
        )

    ok = run("dep-1\ndep-2\n", "dep-1\ndep-2\ndep-3\n")
    assert ok.returncode == 0 and ok.stdout.strip() == "dep-3"

    zero = run("dep-1\n", "dep-1\n")
    assert zero.returncode == 1 and "exactly one" in zero.stderr

    many = run("dep-1\n", "dep-1\ndep-2\ndep-3\n")
    assert many.returncode == 1 and "exactly one" in many.stderr
