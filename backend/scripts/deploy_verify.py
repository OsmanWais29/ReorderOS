"""Deterministic deploy-spec + log verification for the restricted-runtime-role cutover.

Replaces fragile Markdown grep pipelines with a checked-in, unit-tested tool the runbook
calls. Concerns:

  1. validate_source_commit_bindings(spec)  — every service/worker binds SOURCE_COMMIT to
     the PLATFORM value ${_self.COMMIT_HASH} (component-level, never a literal SHA, never
     app-level). Jobs are exempt (may not expose the placeholder).
  2. validate_worker_presence(spec)         — the COMPLETE feature set:
       * CLOVER_ENABLED=true  → inbox-worker AND reconciliation-worker present;
         false/unset → BOTH forbidden (a present-but-disabled worker exits '.disabled'
         and DO marks the whole deploy failed).
       * POSTMARK_INBOUND_ENABLED=true → inbound-email-worker AND
         receipt-extraction-worker present (inbound emails fan out into extraction jobs
         that would otherwise rot at 'pending'); false/unset → inbound-email-worker
         forbidden.
       * ANTHROPIC_API_KEY declared anywhere ↔ receipt-extraction-worker present
         (credential without consumer = stranded uploads; worker without credential
         exits(1) at boot and fails the deploy).
  3. validate_no_secret_values(spec)        — committed specs must NEVER carry a value on
     a `type: SECRET` env (neither plaintext nor an EV[...] reference).
  4. validate_no_duplicate_envs(spec)       — no key declared both app-level and on a
     component (shadowing), and no key declared twice in one env list.
  5. parse_starting_commit(log_text, worker)— extract source_commit from structlog JSON
     `<worker>.starting` lines; the LAST VALID record wins. Caller must bound `log_text`
     to the target deployment so an old line cannot certify a new deploy.
  6. select_new_deployment(before, after)   — deterministic deployment-id capture: the id
     set difference must contain EXACTLY ONE new id; zero or several is a hard error
     (concurrent deployment activity must stop the runbook, not race it).

CLI (run from backend/: `"$PY" -m scripts.deploy_verify …`):
  "$PY" -m scripts.deploy_verify <spec.yaml>                      → exit 0 clean / 1 errors
  "$PY" -m scripts.deploy_verify --select-deployment BEFORE AFTER → print the single new id
      (BEFORE/AFTER are files of deployment ids, one per line) / exit 1 unless exactly one
  "$PY" -m scripts.deploy_verify --cutover BUILT CANDIDATE LIVE [ROLE] → exit 0/1

SAFE DIAGNOSTIC BOUNDARY: the validator functions return detailed error strings for
programmatic use (tests, runbook tooling reading return values), and those strings echo
input-derived text — component names, env keys, role values. The CLI therefore NEVER
prints them: spec-validation failures are reported only as fixed diagnostic codes
(SOURCE_COMMIT_BINDING, WORKER_PRESENCE, SECRET_VALUE_PRESENT, DUPLICATE_ENV,
CUTOVER_CONTRACT) with a failure count and fixed remediation text. No spec value, env
key, component name, role, caller-supplied filename, or exception text ever reaches
stdout/stderr. For detail, call the validators from Python and inspect the returned
lists."""

from __future__ import annotations

import json
import sys
from typing import Any

_COMMIT_PLACEHOLDER = "${_self.COMMIT_HASH}"

# Flag-gated workers: present iff their feature flag is truthy (the worker exits cleanly
# as ".disabled" when its flag is off, and DO treats that clean exit as a failed deploy).
_FLAG_WORKERS = {
    "inbox-worker": "CLOVER_ENABLED",
    "reconciliation-worker": "CLOVER_ENABLED",
    "inbound-email-worker": "POSTMARK_INBOUND_ENABLED",
}
_EXTRACTION_WORKER = "receipt-extraction-worker"
_TRUTHY = {"1", "true", "yes", "on"}


def load_spec(path: str) -> dict[str, Any]:
    import yaml  # type: ignore[import-untyped]

    with open(path) as f:
        return dict(yaml.safe_load(f))


def _env_map(envs: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    return {e["key"]: e for e in (envs or [])}


def _components(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(kind, name, component) for services, workers, and jobs."""
    out: list[tuple[str, str, dict[str, Any]]] = []
    for kind in ("services", "workers", "jobs"):
        for comp in spec.get(kind) or []:
            out.append((kind, comp.get("name", "?"), comp))
    return out


def _flags(spec: dict[str, Any]) -> dict[str, bool]:
    app_level = _env_map(spec.get("envs"))
    out: dict[str, bool] = {}
    for flag in set(_FLAG_WORKERS.values()):
        raw = str((app_level.get(flag) or {}).get("value", "")).strip().lower()
        out[flag] = raw in _TRUTHY
    return out


def _declared_env_keys(spec: dict[str, Any]) -> set[str]:
    """Every env key declared anywhere (app-level or any component)."""
    keys = set(_env_map(spec.get("envs")))
    for _, _, comp in _components(spec):
        keys |= set(_env_map(comp.get("envs")))
    return keys


def validate_source_commit_bindings(spec: dict[str, Any]) -> list[str]:
    """Every service + worker must bind SOURCE_COMMIT to the platform placeholder,
    component-level. Reject literals, app-level, or missing. Returns a list of errors."""
    errors: list[str] = []
    # app-level SOURCE_COMMIT is forbidden — it must be component-scoped so each component
    # reports its OWN build (and ${_self...} only resolves per-component anyway).
    if "SOURCE_COMMIT" in _env_map(spec.get("envs")):
        errors.append("SOURCE_COMMIT is set app-level; it must be component-level only")
    for kind in ("services", "workers"):
        for comp in spec.get(kind) or []:
            name = comp.get("name", "?")
            env = _env_map(comp.get("envs"))
            sc = env.get("SOURCE_COMMIT")
            if sc is None:
                errors.append(f"{kind}/{name}: missing component-level SOURCE_COMMIT")
                continue
            val = str(sc.get("value", ""))
            if val != _COMMIT_PLACEHOLDER:
                errors.append(
                    f"{kind}/{name}: SOURCE_COMMIT must be {_COMMIT_PLACEHOLDER!r}, "
                    f"not a literal/other value (a manually-pasted SHA proves nothing)"
                )
    return errors


def validate_worker_presence(spec: dict[str, Any]) -> list[str]:
    """Worker components present iff the features they serve are enabled — BOTH
    directions, over the COMPLETE feature set (Clover pair, Postmark, extraction)."""
    errors: list[str] = []
    flags = _flags(spec)
    present = {w.get("name") for w in (spec.get("workers") or [])}

    for worker, flag in _FLAG_WORKERS.items():
        enabled = flags.get(flag, False)
        if enabled and worker not in present:
            errors.append(f"{flag} is true but worker {worker!r} is absent")
        if not enabled and worker in present:
            errors.append(
                f"worker {worker!r} is present but {flag} is not true "
                f"(it would exit '.disabled' and fail the DO deploy)"
            )

    # Receipt-extraction worker: tied to the declared extraction capability, and required
    # by the inbound-email pipeline (fan-out creates extraction jobs).
    has_anthropic = "ANTHROPIC_API_KEY" in _declared_env_keys(spec)
    has_extraction = _EXTRACTION_WORKER in present
    if has_anthropic and not has_extraction:
        errors.append(
            f"ANTHROPIC_API_KEY is declared but worker {_EXTRACTION_WORKER!r} is absent — "
            f"uploaded/emailed receipts would strand at 'pending' with no consumer"
        )
    if has_extraction and not has_anthropic:
        errors.append(
            f"worker {_EXTRACTION_WORKER!r} is present but ANTHROPIC_API_KEY is not "
            f"declared (the worker exits(1) without it and fails the deploy)"
        )
    if flags.get("POSTMARK_INBOUND_ENABLED", False) and not has_extraction:
        errors.append(
            f"POSTMARK_INBOUND_ENABLED is true but worker {_EXTRACTION_WORKER!r} is absent — "
            f"inbound emails fan out into extraction jobs that nothing would process"
        )
    return errors


def validate_no_secret_values(spec: dict[str, Any]) -> list[str]:
    """A committed spec must never carry a value on a SECRET env — neither plaintext nor
    an EV[...] encrypted reference (those belong to the live app / console only)."""
    errors: list[str] = []
    scopes: list[tuple[str, dict[str, dict[str, Any]]]] = [
        ("app-level", _env_map(spec.get("envs")))
    ]
    scopes += [(f"{k}/{n}", _env_map(c.get("envs"))) for k, n, c in _components(spec)]
    for where, env in scopes:
        for key, e in env.items():
            if e.get("type") == "SECRET" and str(e.get("value") or "").strip():
                errors.append(
                    f"{where}: SECRET env {key!r} carries a value in the committed spec "
                    f"(secret values are console-only; committed specs declare the key only)"
                )
    return errors


def validate_no_duplicate_envs(spec: dict[str, Any]) -> list[str]:
    """No key both app-level and component-level (shadowing/dangling hazard), and no key
    declared twice within a single env list."""
    errors: list[str] = []
    app_keys = set(_env_map(spec.get("envs")))

    def _dups(envs: list[dict[str, Any]] | None) -> set[str]:
        seen: set[str] = set()
        dup: set[str] = set()
        for e in envs or []:
            k = str(e.get("key"))
            if k in seen:
                dup.add(k)
            seen.add(k)
        return dup

    for k in sorted(_dups(spec.get("envs"))):
        errors.append(f"app-level: env {k!r} declared more than once")
    for kind, name, comp in _components(spec):
        for k in sorted(_dups(comp.get("envs"))):
            errors.append(f"{kind}/{name}: env {k!r} declared more than once")
        overlap = app_keys & set(_env_map(comp.get("envs")))
        for k in sorted(overlap):
            errors.append(
                f"{kind}/{name}: env {k!r} is declared BOTH app-level and component-level "
                f"(shadowing — declare each key in exactly one scope)"
            )
    return errors


def validate_spec(spec: dict[str, Any]) -> list[str]:
    return (
        validate_source_commit_bindings(spec)
        + validate_worker_presence(spec)
        + validate_no_secret_values(spec)
        + validate_no_duplicate_envs(spec)
    )


# ── safe diagnostic boundary (CLI) ────────────────────────────────────────────
# Validator error strings echo input-derived text (component names, env keys, role
# values) and MUST NOT reach stdout/stderr/logs/annotations. The CLI emits only these
# fixed codes + counts + fixed remediation text. Everything below is a compile-time
# literal — never derived from the spec under validation.
_SPEC_CHECKS: tuple[tuple[str, Any, str], ...] = (
    (
        "SOURCE_COMMIT_BINDING",
        validate_source_commit_bindings,
        "bind SOURCE_COMMIT to the platform placeholder ${_self.COMMIT_HASH} at "
        "component level on every service and worker (never app-level, never a literal)",
    ),
    (
        "WORKER_PRESENCE",
        validate_worker_presence,
        "align worker components with the declared feature set in both directions "
        "(Clover pair, Postmark inbound, receipt extraction)",
    ),
    (
        "SECRET_VALUE_PRESENT",
        validate_no_secret_values,
        "remove every value from 'type: SECRET' envs in the committed spec "
        "(secret values are console-only; committed specs declare the key only)",
    ),
    (
        "DUPLICATE_ENV",
        validate_no_duplicate_envs,
        "declare each env key exactly once, in exactly one scope "
        "(no app-level/component shadowing, no repeats within one env list)",
    ),
)
_CUTOVER_REMEDIATION = (
    "the built spec violates the cutover shape/role contract; re-run the builder and "
    "inspect validate_cutover() from Python for detail (its return value is not printed)"
)


def summarize_spec_failures(spec: dict[str, Any]) -> list[tuple[str, int, str]]:
    """(code, failure_count, fixed_remediation) per failing category — the CLI's only
    view of validation failures. Contains no input-derived text by construction."""
    out: list[tuple[str, int, str]] = []
    for code, validator, remediation in _SPEC_CHECKS:
        count = len(validator(spec))
        if count:
            out.append((code, count, remediation))
    return out


# The ONLY env addition the builder may make beyond the candidate shape: preserving an
# in-progress encryption-key rotation on token-decrypting components (see
# scripts.build_cutover_spec._preserve_rotation_previous_key).
_ROTATION_CURRENT = "TOKEN_ENCRYPTION_KEY"
_ROTATION_PREVIOUS = "TOKEN_ENCRYPTION_KEY_PREVIOUS"

# Selected-service-role contract — canonical pattern + DSN-username parser live in the
# builder; the validator imports them so the two tools can never disagree.
_SERVICE_ROLE_KEY = "SERVICE_ROLE_NAME"
_SERVICE_DSN_KEY = "SERVICE_DATABASE_URL"
_REQUEST_ROLE = "reorderos_app"


def _collect_live_service_roles(live: dict[str, Any]) -> list[tuple[str, str | None]]:
    """EVERY SERVICE_ROLE_NAME declaration in the LIVE spec as (where, value) —
    INCLUDING empty, whitespace-only, and null/missing values (round-7 finding: an
    empty declaration must fail closed, never be mistaken for legacy state). Never a
    first-match; never value-filtered. `value` is None when the entry has no usable
    value; callers must treat any such declaration as MALFORMED."""
    found: list[tuple[str, str | None]] = []
    scopes: list[tuple[str, list[dict[str, Any]]]] = [("app-level", live.get("envs") or [])]
    for kind in ("services", "workers", "jobs"):
        for c in live.get(kind) or []:
            scopes.append((f"{kind}/{c.get('name')}", c.get("envs") or []))
    for where, envs in scopes:
        for e in envs:
            if str(e.get("key")) == _SERVICE_ROLE_KEY:
                raw = e.get("value")
                value = str(raw).strip() if raw is not None and str(raw).strip() else None
                found.append((where, value))
    return found


def validate_live_role_declarations(
    live: dict[str, Any],
) -> tuple[list[str], str | None]:
    """Shared strict validation of the LIVE spec's SERVICE_ROLE_NAME declarations
    (used by validate_cutover AND the retirement gate). Returns (errors, role):
      - zero occurrences of the key → ([], None): legacy/unversioned live state;
      - any PRESENT-but-empty/whitespace/null declaration → malformed, error;
      - any non-pattern value → malformed, error;
      - >1 distinct valid values → conflict, error;
      - identical duplicates → structural drift, error;
      - exactly one valid declaration → ([], that_role)."""
    from scripts.build_cutover_spec import SERVICE_ROLE_PATTERN

    declarations = _collect_live_service_roles(live)
    if not declarations:
        return [], None
    errors: list[str] = []
    for where, value in declarations:
        if value is None:
            errors.append(
                f"LIVE spec declares {_SERVICE_ROLE_KEY} at {where} with an "
                f"EMPTY/missing value — present-but-empty is malformed, not legacy; "
                f"resolve the live state before proceeding"
            )
        elif not SERVICE_ROLE_PATTERN.match(value):
            errors.append(
                f"LIVE spec declares a malformed {_SERVICE_ROLE_KEY} at {where} "
                f"({value!r}) — resolve the live state before proceeding"
            )
    valid = [(w, v) for w, v in declarations if v is not None and SERVICE_ROLE_PATTERN.match(v)]
    distinct = sorted({v for _, v in valid})
    if len(distinct) > 1:
        errors.append(
            f"LIVE spec declares CONFLICTING {_SERVICE_ROLE_KEY} values {distinct} at "
            f"{sorted(w for w, _ in valid)} — resolve the live state; refusing to pick one"
        )
    elif len(declarations) > 1:
        errors.append(
            f"LIVE spec declares {_SERVICE_ROLE_KEY} {len(declarations)} times at "
            f"{sorted(w for w, _ in declarations)} — structural drift; the role must be "
            f"declared exactly once"
        )
    if errors:
        return errors, None
    return [], distinct[0]


def _role_version(role: str) -> int:
    """service_worker -> 0; service_worker_vN -> N. Caller validates the pattern first."""
    if role == "service_worker":
        return 0
    return int(role.rsplit("v", 1)[1])


def _deep_diff(a: Any, b: Any, path: str = "$") -> list[str]:
    """Paths where two YAML-shaped structures differ (values never printed)."""
    if type(a) is not type(b):
        return [f"{path} (type {type(a).__name__} vs {type(b).__name__})"]
    if isinstance(a, dict):
        diffs: list[str] = []
        for k in sorted(set(a) | set(b)):
            if k not in a:
                diffs.append(f"{path}.{k} (only in candidate)")
            elif k not in b:
                diffs.append(f"{path}.{k} (only in built)")
            else:
                diffs += _deep_diff(a[k], b[k], f"{path}.{k}")
        return diffs
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path} (length {len(a)} vs {len(b)})"]
        diffs = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            diffs += _deep_diff(x, y, f"{path}[{i}]")
        return diffs
    return [] if a == b else [f"{path}"]


def _live_has_previous_key(live: dict[str, Any]) -> bool:
    for envs in [live.get("envs") or []] + [
        c.get("envs") or []
        for kind in ("services", "workers", "jobs")
        for c in live.get(kind) or []
    ]:
        for e in envs:
            if str(e.get("key")) == _ROTATION_PREVIOUS and str(e.get("value") or "").strip():
                return True
    return False


def _token_decrypting_components(candidate: dict[str, Any]) -> set[tuple[str, str]]:
    """(kind, name) of every candidate component declaring TOKEN_ENCRYPTION_KEY —
    derived INDEPENDENTLY of the builder, from the shape contract itself."""
    out: set[tuple[str, str]] = set()
    for kind in ("services", "workers", "jobs"):
        for comp in candidate.get(kind) or []:
            if any(str(e.get("key")) == _ROTATION_CURRENT for e in comp.get("envs") or []):
                out.add((kind, str(comp.get("name"))))
    return out


def validate_cutover(
    built: dict[str, Any],
    candidate: dict[str, Any],
    live: dict[str, Any],
    service_role: str = "service_worker",
) -> list[str]:
    """Fail-closed validation of a BUILT (secret-bearing) cutover spec against the
    committed candidate shape contract AND the fresh LIVE spec: a NORMALIZED DEEP
    COMPARISON of the ENTIRE spec. The only permitted differences from the candidate
    are documented value substitutions:

      1. an env entry whose candidate value is empty/absent may (must) carry a
         non-empty value in the built spec — same key, scope, and type;
      2. TOKEN_ENCRYPTION_KEY_PREVIOUS (SECRET, non-empty) on a component whose
         candidate envs declare TOKEN_ENCRYPTION_KEY at the same scope — and ONLY
         when the LIVE spec carries the previous key (rotation in progress). The
         distribution is ALL-OR-NONE, derived from the candidate independently of the
         builder: live has the key → EVERY token-decrypting component must carry it
         (losing one destination could make its ciphertext undecryptable); live does
         not → NO component may carry it.

    After normalizing exactly those substitutions away, built must DEEP-EQUAL the
    candidate — covering Dockerfile paths, source_dir, health checks, routes, instance
    count/size, http_port, job kind, github repo/branch/deploy_on_push, run_command,
    databases, name/region, and every other field, known or future. Additionally:
    every env entry in the built spec must carry a non-empty value (an unresolved,
    moved, or valueless secret must never reach `doctl apps update`), a candidate-
    pinned value must match verbatim, and the built spec must still pass the
    worker-presence / SOURCE_COMMIT / duplicate-env validators (defense in depth;
    validate_no_secret_values is deliberately NOT run — the built spec carries values).
    No error message ever contains a value."""
    import copy

    from scripts.build_cutover_spec import SERVICE_ROLE_PATTERN, dsn_username

    errors: list[str] = []
    normalized = copy.deepcopy(built)
    live_previous = _live_has_previous_key(live)
    required_previous = _token_decrypting_components(candidate) if live_previous else set()

    # ── selected-service-role contract (rounds 5+6) ───────────────────────────
    if not SERVICE_ROLE_PATTERN.match(service_role):
        return [
            f"selected service role {service_role!r} is not allowed — must be "
            f"'service_worker' or service_worker_vN (N >= 1, no leading zero; "
            f"no arbitrary roles)"
        ]
    # Live role declarations: collected in FULL (including empty/null — round-7) and
    # validated by the shared strict validator — never first-match, never fail-open.
    live_role_errors, live_role = validate_live_role_declarations(live)
    errors += live_role_errors
    # MONOTONIC rotation (round-6 finding 3): versions only stand still or move
    # forward. service_worker = v0. A downgrade (including back to service_worker)
    # requires a separately named, explicitly approved rollback procedure — it is never
    # accepted through the normal cutover selector.
    if live_role is not None and _role_version(service_role) < _role_version(live_role):
        errors.append(
            f"the LIVE spec runs service role {live_role!r}; selecting "
            f"{service_role!r} is a VERSION DOWNGRADE — rotations are monotonic; "
            f"a rollback needs its own explicitly approved procedure, not the "
            f"normal cutover selector"
        )
    for kind in ("services", "workers", "jobs"):
        for comp in built.get(kind) or []:
            comp_where = f"{kind}/{comp.get('name')}"
            for e in comp.get("envs") or []:
                key = str(e.get("key"))
                value = str(e.get("value") or "")
                if key == _SERVICE_DSN_KEY and dsn_username(value) != service_role:
                    errors.append(
                        f"{comp_where}: {_SERVICE_DSN_KEY} username disagrees with the "
                        f"selected service role {service_role!r} (or is unverifiable) — "
                        f"role name and credential must change together"
                    )
                if (
                    key == "DATABASE_URL"
                    and kind == "services"
                    and dsn_username(value) != _REQUEST_ROLE
                ):
                    errors.append(
                        f"{comp_where}: DATABASE_URL username must be {_REQUEST_ROLE!r} "
                        f"(the request pool never changes role)"
                    )
    for e in built.get("envs") or []:
        if (
            str(e.get("key")) == _SERVICE_DSN_KEY
            and dsn_username(str(e.get("value") or "")) != service_role
        ):
            errors.append(
                f"app-level: {_SERVICE_DSN_KEY} username disagrees with the selected "
                f"service role {service_role!r} (or is unverifiable)"
            )

    def _scopes(spec: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {("app-level", ""): spec}
        for kind in ("services", "workers", "jobs"):
            for comp in spec.get(kind) or []:
                out[(kind, str(comp.get("name")))] = comp
        return out

    norm_scopes, cand_scopes = _scopes(normalized), _scopes(candidate)
    if set(norm_scopes) != set(cand_scopes):
        errors.append(
            f"component set drift: built={sorted(set(norm_scopes) - {('app-level', '')})} "
            f"candidate={sorted(set(cand_scopes) - {('app-level', '')})}"
        )

    for scope_key in sorted(set(norm_scopes) & set(cand_scopes)):
        holder, cand_holder = norm_scopes[scope_key], cand_scopes[scope_key]
        where = scope_key[0] if scope_key[1] == "" else f"{scope_key[0]}/{scope_key[1]}"
        cand_envs = {
            (str(e.get("key")), str(e.get("scope", "RUN_AND_BUILD_TIME"))): e
            for e in cand_holder.get("envs") or []
        }
        normalized_envs: list[dict[str, Any]] = []
        for e in holder.get("envs") or []:
            key = str(e.get("key"))
            scope = str(e.get("scope", "RUN_AND_BUILD_TIME"))
            etype = str(e.get("type", "GENERAL"))
            value = str(e.get("value") or "")
            if not value.strip():
                errors.append(
                    f"{where}: {key} has no value in the BUILT spec — unresolved/valueless "
                    f"entries must never be applied"
                )
            cand_entry = cand_envs.get((key, scope))
            if cand_entry is not None:
                if str(cand_entry.get("type", "GENERAL")) != etype:
                    errors.append(f"{where}: {key} type drifted from the candidate")
                cand_value = str(cand_entry.get("value") or "")
                if key == _SERVICE_ROLE_KEY:
                    # substitution 3: the selected role (builder --service-role) governs
                    # this value — NOT the candidate pin ('service_worker').
                    if value != service_role:
                        errors.append(
                            f"{where}: {key} is {value!r} but the selected service role "
                            f"is {service_role!r} — the effective spec must bind exactly "
                            f"the selected role"
                        )
                elif cand_value.strip() and value != cand_value:
                    errors.append(f"{where}: {key} pinned value drifted from the candidate")
                # substitution 1: candidate empty/absent value filled by the builder —
                # normalize to the candidate's own entry.
                normalized_envs.append(copy.deepcopy(cand_entry))
            elif (
                key == _ROTATION_PREVIOUS
                and etype == "SECRET"
                and (_ROTATION_CURRENT, scope) in cand_envs
                and scope_key in required_previous
            ):
                pass  # substitution 2: rotation preservation — normalize away entirely
            elif key == _ROTATION_PREVIOUS and not live_previous:
                errors.append(
                    f"{where}: {key} present in the BUILT spec but the LIVE spec carries "
                    f"no previous key — nothing to preserve; remove it"
                )
            else:
                errors.append(
                    f"{where}: unexpected env {key} (scope {scope}) not declared by the "
                    f"candidate and not a documented substitution"
                )
        for (key, scope), _cand_entry in cand_envs.items():
            if not any(
                str(e.get("key")) == key and str(e.get("scope", "RUN_AND_BUILD_TIME")) == scope
                for e in holder.get("envs") or []
            ):
                errors.append(f"{where}: env {key} (scope {scope}) missing from the built spec")
        # Rotation distribution is ALL-OR-NONE: live carries the previous key → THIS
        # token-decrypting component must too (a lost destination = undecryptable
        # ciphertext there).
        if scope_key in required_previous and not any(
            str(e.get("key")) == _ROTATION_PREVIOUS and str(e.get("value") or "").strip()
            for e in holder.get("envs") or []
        ):
            errors.append(
                f"{where}: the LIVE spec carries {_ROTATION_PREVIOUS} but this "
                f"token-decrypting component does not — rotation distribution must be "
                f"all-or-none"
            )
        if "envs" in holder or "envs" in cand_holder:
            holder["envs"] = normalized_envs

    # THE deep comparison: everything not normalized above must be byte-identical.
    for diff_path in _deep_diff(candidate, normalized):
        errors.append(f"shape drift vs candidate at {diff_path}")

    errors += validate_source_commit_bindings(built)
    errors += validate_worker_presence(built)
    errors += validate_no_duplicate_envs(built)
    return sorted(set(errors))


def parse_starting_commit(log_text: str, worker: str) -> str | None:
    """Return the source_commit from the LAST VALID structlog JSON `<worker>.starting`
    line, or None if no valid record exists. Malformed lines, wrong events, and records
    with a missing/empty/"unknown" source_commit are skipped (they never overwrite an
    earlier valid record). `worker` is the module event prefix, e.g. 'inbox_worker'.
    Caller must bound `log_text` to the target deployment so an old line cannot certify
    a new deploy."""
    want = f"{worker}.starting"
    found: str | None = None
    for line in log_text.splitlines():
        line = line.strip()
        if not line or "{" not in line:
            continue
        # tolerate a log prefix before the JSON object
        obj_start = line.index("{")
        try:
            rec = json.loads(line[obj_start:])
        except (json.JSONDecodeError, ValueError):
            continue
        if not (isinstance(rec, dict) and rec.get("event") == want):
            continue
        sc = rec.get("source_commit")
        if isinstance(sc, str) and sc.strip() and sc != "unknown":
            found = sc
    return found


def parse_starting_evidence(log_text: str, worker: str) -> tuple[str, str] | None:
    """Return (source_commit, service_user) from the LAST `<worker>.starting` record in
    which BOTH are valid non-empty strings (source_commit != "unknown"). A record whose
    service_user is missing, null, or empty is NOT evidence — the worker either predates
    the evidence contract or started with the role assertion disabled — and never
    overwrites an earlier valid record. Same parsing rules and deployment-bounding
    caveat as parse_starting_commit; consumed by scripts/retire_verify.py, which
    requires the pair to equal (expected_sha, expected_role)."""
    want = f"{worker}.starting"
    found: tuple[str, str] | None = None
    for line in log_text.splitlines():
        line = line.strip()
        if not line or "{" not in line:
            continue
        obj_start = line.index("{")
        try:
            rec = json.loads(line[obj_start:])
        except (json.JSONDecodeError, ValueError):
            continue
        if not (isinstance(rec, dict) and rec.get("event") == want):
            continue
        sc = rec.get("source_commit")
        su = rec.get("service_user")
        if (
            isinstance(sc, str)
            and sc.strip()
            and sc != "unknown"
            and isinstance(su, str)
            and su.strip()
        ):
            found = (sc, su)
    return found


def parse_role_ok(log_text: str) -> tuple[str, str] | None:
    """Extract (request_user, service_user) from the LAST valid `role.ok` structlog JSON
    record. The app logs via structlog's JSONRenderer, so the record is JSON —
    `{"request_user": "reorderos_app", "service_user": "service_worker",
    "event": "role.ok", ...}` — and a `request_user=...` substring grep can never match
    it. Returns None when no valid record exists (missing users, malformed lines, and
    wrong events are skipped). Caller must bound `log_text` to the target deployment."""
    found: tuple[str, str] | None = None
    for line in log_text.splitlines():
        line = line.strip()
        if not line or "{" not in line:
            continue
        try:
            rec = json.loads(line[line.index("{") :])
        except (json.JSONDecodeError, ValueError):
            continue
        if not (isinstance(rec, dict) and rec.get("event") == "role.ok"):
            continue
        request_user, service_user = rec.get("request_user"), rec.get("service_user")
        if (
            isinstance(request_user, str)
            and request_user.strip()
            and isinstance(service_user, str)
            and service_user.strip()
        ):
            found = (request_user, service_user)
    return found


def select_new_deployment(before: list[str], after: list[str]) -> str:
    """Deterministic deployment-id capture for the runbook: exactly ONE id may be new.

    Zero new ids means the update produced no deployment; more than one means concurrent
    deployment activity — either way the runbook must STOP, not guess. Raises ValueError
    unless exactly one new nonblank id exists; returns it."""
    before_set = {b.strip() for b in before if b.strip()}
    new = sorted({a.strip() for a in after if a.strip()} - before_set)
    if len(new) != 1:
        raise ValueError(
            f"expected exactly one new deployment id, found {len(new)}: {new!r} — "
            f"STOP (zero = no deployment created; several = concurrent activity)"
        )
    return new[0]


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(
            "usage: python -m scripts.deploy_verify <spec.yaml>\n"
            "       python -m scripts.deploy_verify --select-deployment BEFORE_FILE AFTER_FILE\n"
            "       python -m scripts.deploy_verify --cutover BUILT CANDIDATE LIVE [SERVICE_ROLE]",
            file=sys.stderr,
        )
        return 2
    if args[0] == "--select-deployment":
        if len(args) != 3:
            print("--select-deployment needs BEFORE_FILE AFTER_FILE", file=sys.stderr)
            return 2
        with open(args[1]) as f:
            before = f.read().splitlines()
        with open(args[2]) as f:
            after = f.read().splitlines()
        try:
            print(select_new_deployment(before, after))
        except ValueError as exc:
            print(f"deploy_verify FAIL: {exc}", file=sys.stderr)
            return 1
        return 0
    if args[0] == "--cutover":
        if len(args) not in (4, 5):
            print(
                "--cutover needs BUILT_SPEC CANDIDATE_SPEC LIVE_SPEC [SERVICE_ROLE]",
                file=sys.stderr,
            )
            return 2
        selected_role = args[4] if len(args) == 5 else "service_worker"
        errors = validate_cutover(
            load_spec(args[1]), load_spec(args[2]), load_spec(args[3]), selected_role
        )
        # Safe diagnostic boundary: raw cutover errors echo spec-derived text and are
        # never printed — only the fixed code, the count, and fixed remediation.
        if errors:
            print(
                f"deploy_verify FAIL [CUTOVER_CONTRACT]: {len(errors)} failure(s) — "
                f"{_CUTOVER_REMEDIATION}",
                file=sys.stderr,
            )
            print(
                f"deploy_verify --cutover: {len(errors)} problem(s) — do NOT apply",
                file=sys.stderr,
            )
            return 1
        print("deploy_verify --cutover OK: built spec matches the candidate and is fully resolved")
        return 0
    spec = load_spec(args[0])
    # Safe diagnostic boundary: raw validator errors echo spec-derived text and are
    # never printed — only fixed category codes, per-category counts, and fixed
    # remediation text.
    failures = summarize_spec_failures(spec)
    for code, count, remediation in failures:
        print(f"deploy_verify FAIL [{code}]: {count} failure(s) — {remediation}", file=sys.stderr)
    if failures:
        total = sum(count for _, count, _ in failures)
        print(f"deploy_verify: {total} problem(s) in the given spec", file=sys.stderr)
        return 1
    print("deploy_verify OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
