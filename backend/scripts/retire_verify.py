"""Execution-bound retirement EVIDENCE gate for the versioned-role cutover (B-alt.7).

SCOPE (round-8): a PASS here is an audit record that no running component consumes the
old role. It does NOT authorize executing `ALTER ROLE service_worker` to NOLOGIN —
retirement is UNSUPPORTED (runbook B-alt.8): doctl/App Platform offer no deployment
lock and no noninteractive exec, so nothing can bind these control-plane checks to the
database mutation; the verify-to-DDL window is small but neither zero nor atomic.

WHY THIS EXISTS (round-7 P1s): `doctl apps console` is interactive-only (no command
flag as of doctl 1.155 — verified via `doctl apps console --help`), so per-component
in-container preflights cannot be automated, and a manually-typed "passed" list proves
only that names were typed. This gate replaces manual attestation with EVIDENCE the
local machine can verify, all bound to ONE active deployment:

  1. No in-progress deployment may exist (before AND after evidence collection).
  2. The ACTIVE deployment id is resolved first; its phase must be ACTIVE.
  3. The spec is fetched WITH `--deployment <ACTIVE_ID>` — never the unbound app spec —
     and its SHA-256 digest is recorded.
  4. That spec must bind RESTRICTED_RUNTIME_ROLES_ENABLED=true and EXACTLY the expected
     versioned service role (shared strict declaration validator: present-but-empty,
     malformed, conflicting, or duplicated declarations all fail), AND bind
     SOURCE_COMMIT to the platform placeholder (${_self.COMMIT_HASH}) component-level
     on every service and worker — /version and start lines merely echo that env, so
     without the placeholder binding the SHA evidence would be self-referential.
  5. The component inventory is derived from that spec, and services must be EXACTLY
     ['api'] — the only service with a role-proof event today. Additional, unnamed, or
     duplicate services (or unnamed/duplicate workers) refuse rather than being
     silently inventoried without evidence.
  6. The api's deployment-bound logs must contain a role.ok JSON record equal to
     ("reorderos_app", expected_role).
  7. EVERY worker's deployment-bound logs must contain a `.starting` record carrying
     BOTH source_commit == the expected SHA AND service_user == the expected role.
     service_user is the RETURN VALUE of the worker's fail-closed service-pool role
     assertion, logged in the same record — the evidence is the logged assertion
     result, not source-code ordering. The passed-set therefore equals the inventory
     by construction; there is no separate attestation step to forget or fake.
  8. Optionally (--base-url) the live `/version` commit must equal the expected SHA.
  9. RE-CHECK immediately before returning: no in-progress deployment, same ACTIVE id,
     phase still ACTIVE, and the re-fetched deployment-bound spec digest unchanged.

On success the ONLY stdout output is the verified ACTIVE deployment id (for
`capture_rollback retire <ACTIVE_ID>`); all progress/errors go to stderr. Log contents,
DSNs, and secrets are never printed.

CLI (from backend/):
  ACTIVE_ID=$("$PY" -m scripts.retire_verify --app "$APP" \
      --role "$(cat ~/.reorderos-cutover/service_role)" \
      --sha  "$(cat ~/.reorderos-cutover/expected_sha)" \
      --base-url "$BASE")
`--doctl` overrides the doctl binary (tests inject a fake runner).
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from typing import Any


def _doctl(doctl: str, *args: str) -> str:
    # list argv, shell=False; operator-controlled binary path (tests inject a fake)
    proc = subprocess.run([doctl, *args], capture_output=True, text=True)  # noqa: S603
    if proc.returncode != 0:
        # args carry no secrets (app id / deployment id / component names only)
        raise RuntimeError(f"doctl {' '.join(args)} failed (rc={proc.returncode})")
    return proc.stdout


def _fetch_version_commit(base_url: str) -> str:
    import json
    import urllib.request

    with urllib.request.urlopen(f"{base_url}/version", timeout=10) as resp:  # noqa: S310
        return str(json.load(resp).get("commit", "unknown"))


def verify(
    app: str,
    expected_role: str,
    expected_sha: str,
    doctl: str = "doctl",
    base_url: str | None = None,
) -> tuple[list[str], str | None]:
    """Returns (errors, verified_active_id). verified_active_id is None on any error."""
    import yaml  # type: ignore[import-untyped]

    from scripts.build_cutover_spec import SERVICE_ROLE_PATTERN
    from scripts.deploy_verify import (
        parse_role_ok,
        parse_starting_evidence,
        validate_live_role_declarations,
        validate_source_commit_bindings,
    )

    errors: list[str] = []
    if not SERVICE_ROLE_PATTERN.match(expected_role) or expected_role == "service_worker":
        return (
            [
                f"expected role {expected_role!r} is not a versioned service role — "
                f"retirement applies only after a versioned cutover"
            ],
            None,
        )
    if not expected_sha.strip() or expected_sha.strip() == "unknown":
        return (["expected SHA is empty/unknown — refusing"], None)

    def _in_progress() -> str:
        return _doctl(
            doctl, "apps", "get", app, "--format", "InProgressDeployment.ID", "--no-header"
        ).strip()

    def _active_id() -> str:
        return _doctl(
            doctl, "apps", "get", app, "--format", "ActiveDeployment.ID", "--no-header"
        ).strip()

    def _phase(dep: str) -> str:
        return _doctl(
            doctl, "apps", "get-deployment", app, dep, "--format", "Phase", "--no-header"
        ).strip()

    def _spec_text(dep: str) -> str:
        return _doctl(doctl, "apps", "spec", "get", app, "--deployment", dep)

    try:
        # 1-3: single active deployment, bound spec, digest.
        if _in_progress():
            return (["an in-progress deployment exists — retirement forbidden"], None)
        active = _active_id()
        if not active:
            return (["app reports no active deployment"], None)
        if _phase(active) != "ACTIVE":
            return ([f"deployment {active} is not ACTIVE"], None)
        spec_text = _spec_text(active)
        digest = hashlib.sha256(spec_text.encode()).hexdigest()
        spec: dict[str, Any] = dict(yaml.safe_load(spec_text) or {})
        if not spec.get("name"):
            return ([f"deployment {active} spec is empty/invalid"], None)

        # 4: role binding + flag, from the DEPLOYMENT-BOUND spec only.
        decl_errors, live_role = validate_live_role_declarations(spec)
        errors += decl_errors
        if not decl_errors and live_role != expected_role:
            errors.append(
                f"deployment {active} binds service role {live_role!r}, expected "
                f"{expected_role!r} — retirement authorization refused"
            )
        flag_values = {
            str(e.get("value"))
            for holder in [
                spec,
                *(spec.get("services") or []),
                *(spec.get("workers") or []),
                *(spec.get("jobs") or []),
            ]
            for e in holder.get("envs") or []
            if str(e.get("key")) == "RESTRICTED_RUNTIME_ROLES_ENABLED"
        }
        if flag_values != {"true"}:
            errors.append(
                f"deployment {active} does not bind RESTRICTED_RUNTIME_ROLES_ENABLED=true "
                f"— worker start lines would not prove role assertions; refusing"
            )
        # Round-8 finding 1: /version and worker start lines merely ECHO the
        # SOURCE_COMMIT env — they are evidence only when the bound spec ties that env
        # to the platform placeholder (${_self.COMMIT_HASH}) on every component. A
        # literal or missing binding could let a stale expected SHA certify different
        # code, so any binding error refuses retirement.
        errors += [
            f"deployment {active}: {e} — SHA evidence would be self-referential; refusing"
            for e in validate_source_commit_bindings(spec)
        ]

        # 5: inventory from the bound spec. Round-8 finding 3: services are supported
        # ONLY when each has a role-proof event; today that is exactly ['api']
        # (role.ok). Anything else — additional, unnamed, or duplicate services —
        # would be inventoried without evidence, so it refuses instead.
        services = [str(s.get("name") or "") for s in spec.get("services") or []]
        workers = [str(w.get("name") or "") for w in spec.get("workers") or []]
        if services != ["api"]:
            errors.append(
                f"deployment {active} services are {services}; the supported proof "
                f"inventory is exactly ['api'] — a service without its own role-proof "
                f"event cannot be certified; refusing"
            )
        if not workers:
            errors.append(f"deployment {active} has no workers — inventory empty; refusing")
        elif len(set(workers)) != len(workers) or any(not w for w in workers):
            errors.append(
                f"deployment {active} worker inventory {workers} has unnamed or "
                f"duplicate entries — per-worker evidence would be ambiguous; refusing"
            )

        # 8 (early, cheap): live /version commit equals the expected SHA.
        if base_url is not None:
            live_sha = _fetch_version_commit(base_url)
            if live_sha != expected_sha:
                errors.append(
                    "live /version commit does not equal the expected SHA "
                    "(got a different or unknown commit) — refusing"
                )

        if errors:
            return (errors, None)

        # 6-7: per-component EXECUTION evidence, bound to THIS deployment. The passed
        # set equals the inventory by construction — a component passes only via its
        # own deployment-bound log evidence; there is nothing manual to type.
        api_logs = _doctl(
            doctl, "apps", "logs", app, "api", "--type", "run", "--deployment", active
        )
        role_ok = parse_role_ok(api_logs)
        if role_ok != ("reorderos_app", expected_role):
            errors.append(
                f"api role.ok is {role_ok} in deployment {active}, expected "
                f"('reorderos_app', {expected_role!r})"
            )
        for worker in workers:
            worker_logs = _doctl(
                doctl, "apps", "logs", app, worker, "--type", "run", "--deployment", active
            )
            event = worker.replace("-", "_")
            # Round-8 finding 2: the record must carry the AUTHENTICATED role itself
            # (workers log the return value of their fail-closed pool assertion as
            # service_user) — a `.starting` line without it, or with any other role,
            # is not evidence, regardless of what the source code looks like today.
            evidence = parse_starting_evidence(worker_logs, event)
            if evidence != (expected_sha, expected_role):
                errors.append(
                    f"worker {worker}: no `{event}.starting` record proving BOTH "
                    f"source_commit == expected SHA AND service_user == "
                    f"{expected_role!r} in deployment {active} — its role assertion "
                    f"is unproven; refusing"
                )

        # 9: RE-CHECK — the world must not have moved while evidence was collected.
        if _in_progress():
            errors.append("an in-progress deployment appeared during verification — refusing")
        recheck_active = _active_id()
        if recheck_active != active:
            errors.append(
                f"active deployment changed during verification ({active} -> "
                f"{recheck_active}) — refusing"
            )
        elif _phase(active) != "ACTIVE":
            errors.append(f"deployment {active} is no longer ACTIVE — refusing")
        elif hashlib.sha256(_spec_text(active).encode()).hexdigest() != digest:
            errors.append(f"deployment {active} spec digest changed during verification — refusing")
    except RuntimeError as exc:
        errors.append(str(exc))
        return (errors, None)

    if errors:
        return (errors, None)
    return ([], active)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Execution-bound, deployment-bound retirement gate (runbook B-alt.7)."
    )
    p.add_argument("--app", required=True)
    p.add_argument("--role", required=True, help="expected versioned service role")
    p.add_argument("--sha", required=True, help="expected git SHA")
    p.add_argument("--base-url", default=None, help="app base URL for the /version check")
    p.add_argument("--doctl", default="doctl", help="doctl binary (tests inject a fake)")
    args = p.parse_args(argv)

    errors, active = verify(args.app, args.role, args.sha, args.doctl, args.base_url)
    for error in errors:
        print(f"retire_verify FAIL: {error}", file=sys.stderr)
    if errors or active is None:
        print("retire_verify: retirement evidence INCOMPLETE", file=sys.stderr)
        return 1
    print(
        f"retire_verify OK: deployment {active} evidence complete "
        f"(audit record only — B-alt.8 retirement remains UNSUPPORTED)",
        file=sys.stderr,
    )
    print(active)  # the ONLY stdout output: feeds capture_rollback retire <ACTIVE_ID>
    return 0


if __name__ == "__main__":
    sys.exit(main())
