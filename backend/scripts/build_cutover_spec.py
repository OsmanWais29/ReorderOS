"""Build the fully-populated, deployable cutover spec from the committed candidate.

The committed candidate (.do/staging.app.yaml) is the SHAPE contract and carries no
secret values. The live app holds the current values (SECRETs as encrypted EV[...]
references). This tool merges them, fail-closed, into a mode-0600 temporary spec that
the runbook applies exactly once and then unlinks. It never prints a value.

Resolution rules, per candidate env entry:

  SECRET with empty value (the only legal committed form):
    0. ROLE-BOUND DSNs (DATABASE_URL, SERVICE_DATABASE_URL) NEVER carry from live:
       DigitalOcean returns deployed secrets as opaque EV[...] references whose
       usernames cannot be inspected, so carrying one would make every rebuild against
       a real live deployment unverifiable. They resolve EXCLUSIVELY from fresh local
       mode-0600 files on EVERY build — including a re-run against an already-versioned
       live deployment. Missing/empty/insecure/unparseable → abort, no output.
    1. Any OTHER secret: if the LIVE spec has an entry whose FULL IDENTITY is unchanged
       — component kind, component name, key, and env scope — its value (EV ref or
       plaintext) is CARRIED. An EV[...] reference is never moved, copied, or
       re-scoped: any identity change means the entry is treated as NEW.
    2. Otherwise the value is INJECTED from a local secrets directory: the file
       `<component>.<KEY>` (component-specific, e.g. `api.DATABASE_URL` vs
       `migrate.DATABASE_URL`) or, as a fallback, `<KEY>` (shared across components).
       The file must exist, be non-empty, and not be group/other-readable.
    3. Neither available → collected as an error. ANY error → NO output file is
       written and the exit code is 1 (abort BEFORE apply; post-deployment readiness
       is verification, never the fallback).

  SECRET with a value in the candidate → error (the committed contract must not carry
  values; scripts.deploy_verify enforces the same for the repo file).

  GENERAL with empty value → carry-from-live BY KEY (configuration, not credentials —
  e.g. POSTMARK_INBOUND_ADDRESS): the live value is copied; MORE THAN ONE distinct
  non-empty live value is a CONFLICT → error (never silently pick one); missing live
  value → error.

  GENERAL with a value → kept verbatim from the candidate.

TOKEN_ENCRYPTION_KEY_PREVIOUS (encryption-key rotation): if the LIVE app carries it,
it is PRESERVED in every token-decrypting destination (every output component that
declares TOKEN_ENCRYPTION_KEY) — identity-unchanged live entry carried, else the
mode-0600 secrets file is REQUIRED — or the build ABORTS. Losing an in-progress
rotation key could make existing OAuth ciphertext undecryptable, so a note is never
enough; deliberate retirement means removing it from the LIVE spec first with
independent proof that no ciphertext still needs it. When the live app does not carry
it (fresh environment), nothing is required and nothing is added.

Output: written fail-closed — the path must NOT already exist (O_EXCL; O_CREAT's mode
applies only on creation), symlinks are rejected (O_NOFOLLOW + explicit check),
fchmod(0600) pins the mode, and a partial write is unlinked. The report printed to
stdout contains component names, key names, and the resolution kind (carried-ev /
carried-live / injected-file / kept) — NEVER values.

CLI (from backend/):
  "$PY" -m scripts.build_cutover_spec \
      --candidate ../.do/staging.app.yaml \
      --live  ~/.reorderos-cutover/live_spec.yaml \
      --secrets ~/.reorderos-cutover/secrets \
      --out   ~/.reorderos-cutover/cutover.yaml
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_KINDS = ("services", "workers", "jobs")

# THE selected-service-role contract (shared shape with scripts.deploy_verify):
#   - standard cutover resolves to exactly "service_worker";
#   - Phase B-alt accepts only versioned replacements service_worker_vN, N an integer
#     >= 1 with NO leading zero (canonical, monotonic — see deploy_verify);
#   - no other role name is ever accepted (fail-closed, no arbitrary override).
SERVICE_ROLE_PATTERN = re.compile(r"^service_worker(_v[1-9][0-9]*)?$")
_SERVICE_ROLE_KEY = "SERVICE_ROLE_NAME"
_SERVICE_DSN_KEY = "SERVICE_DATABASE_URL"
_REQUEST_DSN_KEY = "DATABASE_URL"
_REQUEST_ROLE = "reorderos_app"

# ROLE-BOUND DSN keys: the credentials whose USERNAMES the cutover verifies. These are
# NEVER carried from the live spec — DigitalOcean returns deployed SECRET values as
# opaque EV[...] references, whose usernames cannot be inspected, so a carried value
# would make every rebuild against a real live deployment unverifiable (the round-6
# P1). They resolve EXCLUSIVELY from fresh local mode-0600 files on EVERY build,
# including a re-run against an already-versioned live deployment.
_ROLE_BOUND_DSN_KEYS = frozenset({_REQUEST_DSN_KEY, _SERVICE_DSN_KEY})


def dsn_username(value: str) -> str | None:
    """Username of a plaintext DSN; None for EV refs / unparseable values (username
    checks then FAIL — an unverifiable credential is never accepted)."""
    if not value or value.startswith("EV["):
        return None
    try:
        return urlparse(
            value.replace("postgresql+asyncpg://", "postgresql://").replace(
                "postgres+asyncpg://", "postgres://"
            )
        ).username
    except ValueError:
        return None


@dataclass
class BuildResult:
    spec: dict[str, Any] | None
    # (where, key, resolution) — resolution ∈ carried-ev / carried-live / injected-file / kept
    report: list[tuple[str, str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _iter_scopes(spec: dict[str, Any]) -> list[tuple[str, str, str, list[dict[str, Any]]]]:
    """(kind, component_name, where_label, envs) for app-level + every component."""
    out: list[tuple[str, str, str, list[dict[str, Any]]]] = [
        ("app", "app", "app-level", spec.get("envs") or [])
    ]
    for kind in _KINDS:
        for comp in spec.get(kind) or []:
            name = str(comp.get("name", "?"))
            out.append((kind, name, f"{kind}/{name}", comp.setdefault("envs", [])))
    return out


def _live_index(live: dict[str, Any]) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    """(kind, component_name, key, env_scope) -> live env entry. Full-identity index —
    the ONLY lookup allowed for SECRET carry (EV refs must not change identity)."""
    idx: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for kind, name, _, envs in _iter_scopes(live):
        for e in envs:
            idx[(kind, name, str(e.get("key")), str(e.get("scope", "RUN_AND_BUILD_TIME")))] = e
    return idx


def _live_values_by_key(live: dict[str, Any], key: str) -> dict[str, list[str]]:
    """Key-level lookup for GENERAL (non-credential) carry-from-live: every distinct
    non-empty live value -> the locations carrying it. More than one distinct value is
    a CONFLICT the caller must reject (never silently pick the first)."""
    found: dict[str, list[str]] = {}
    for _, _, where, envs in _iter_scopes(live):
        for e in envs:
            if str(e.get("key")) == key and e.get("type") != "SECRET":
                val = str(e.get("value") or "")
                if val.strip():
                    found.setdefault(val, []).append(where)
    return found


def _read_secret_file(secrets_dir: Path, component: str, key: str) -> tuple[str | None, str]:
    """Return (value, source_label). Component-specific file wins; shared file falls
    back. Rejects empty and group/other-readable files."""
    for candidate in (secrets_dir / f"{component}.{key}", secrets_dir / key):
        if not candidate.is_file():
            continue
        mode = stat.S_IMODE(candidate.stat().st_mode)
        if mode & 0o077:
            return None, f"{candidate.name} is not mode 0600 (found {oct(mode)})"
        value = candidate.read_text().strip()
        if not value:
            return None, f"{candidate.name} is empty"
        return value, candidate.name
    return None, "no secret file"


def build(
    candidate: dict[str, Any],
    live: dict[str, Any],
    secrets_dir: Path,
    service_role: str = "service_worker",
) -> BuildResult:
    import copy

    result = BuildResult(spec=copy.deepcopy(candidate))
    assert result.spec is not None
    if not SERVICE_ROLE_PATTERN.match(service_role):
        result.errors.append(
            f"selected service role {service_role!r} is not allowed — must be "
            f"'service_worker' or service_worker_vN (N >= 1, no leading zero; no arbitrary roles)"
        )
        result.spec = None
        return result
    live_idx = _live_index(live)

    for kind, name, where, envs in _iter_scopes(result.spec):
        for e in envs:
            key = str(e.get("key"))
            scope = str(e.get("scope", "RUN_AND_BUILD_TIME"))
            is_secret = e.get("type") == "SECRET"
            value = str(e.get("value") or "")
            if is_secret:
                if value.strip():
                    result.errors.append(
                        f"{where}: SECRET {key} carries a value in the CANDIDATE — the "
                        f"committed contract must declare secrets valueless"
                    )
                    continue
                # ROLE-BOUND DSNs never carry from live (EV refs are username-opaque —
                # a rebuild against a real DO live spec would be unverifiable). Fresh
                # local mode-0600 files are REQUIRED on every build.
                if key not in _ROLE_BOUND_DSN_KEYS:
                    live_entry = live_idx.get((kind, name, key, scope))
                    if live_entry is not None and str(live_entry.get("value") or "").strip():
                        if live_entry.get("type") != "SECRET":
                            # live plaintext under an identity now declared SECRET: same
                            # identity, same material — carry (DO encrypts on apply).
                            e["value"] = str(live_entry["value"])
                            result.report.append((where, key, "carried-live"))
                        else:
                            e["value"] = str(live_entry["value"])
                            result.report.append((where, key, "carried-ev"))
                        continue
                secret_value, source = _read_secret_file(secrets_dir, name, key)
                if secret_value is None:
                    if key in _ROLE_BOUND_DSN_KEYS:
                        result.errors.append(
                            f"{where}: role-bound DSN {key} requires a FRESH mode-0600 "
                            f"file ({name}.{key} or {key}) on EVERY build — live values "
                            f"are never carried for role verification; found {source}"
                        )
                    else:
                        result.errors.append(
                            f"{where}: SECRET {key} has no identity-unchanged live value "
                            f"and {source} in the secrets dir — provide "
                            f"{name}.{key} (or {key}) as a non-empty mode-0600 file"
                        )
                    continue
                e["value"] = secret_value
                result.report.append((where, key, f"injected-file:{source}"))
            else:
                if value.strip():
                    result.report.append((where, key, "kept"))
                    continue
                live_vals = _live_values_by_key(live, key)
                if not live_vals:
                    result.errors.append(
                        f"{where}: {key} is carry-from-live (empty value) but the live "
                        f"spec has no non-secret value for it"
                    )
                    continue
                if len(live_vals) > 1:
                    locations = sorted(loc for locs in live_vals.values() for loc in locs)
                    result.errors.append(
                        f"{where}: {key} has CONFLICTING non-secret values in the live "
                        f"spec at {locations} — resolve live first; refusing to pick one"
                    )
                    continue
                e["value"] = next(iter(live_vals))
                result.report.append((where, key, "carried-live"))

    _preserve_rotation_previous_key(result, live, live_idx, secrets_dir)
    _apply_selected_service_role(result, service_role)

    if result.errors:
        result.spec = None
    return result


_ROTATION_CURRENT = "TOKEN_ENCRYPTION_KEY"
_ROTATION_PREVIOUS = "TOKEN_ENCRYPTION_KEY_PREVIOUS"


def _apply_selected_service_role(result: BuildResult, service_role: str) -> None:
    """Bind the SELECTED service role into the effective spec — the one place the role
    name enters the deployable artifact:

      - every SERVICE_ROLE_NAME env's value becomes `service_role` (the candidate pins
        'service_worker'; Phase B-alt selects a versioned name via --service-role — the
        substitution is machine-made here, never hand-edited);
      - every resolved SERVICE_DATABASE_URL must be a parseable plaintext DSN whose
        USERNAME equals the selected role (an EV ref or unparseable value is
        unverifiable → error);
      - the api service's DATABASE_URL username must be `reorderos_app` (the request
        pool never changes role; the migrate job's admin DSN is deliberately exempt).

    Errors carry role/env NAMES only — never DSNs."""
    assert result.spec is not None
    seen_role_key = False
    for kind, _name, where, envs in _iter_scopes(result.spec):
        for e in envs:
            key = str(e.get("key"))
            value = str(e.get("value") or "")
            if key == _SERVICE_ROLE_KEY:
                seen_role_key = True
                if value != service_role:
                    e["value"] = service_role
                    result.report.append((where, key, f"selected-role:{service_role}"))
                else:
                    result.report.append((where, key, f"selected-role:{service_role}"))
            elif key == _SERVICE_DSN_KEY:
                if dsn_username(value) != service_role:
                    result.errors.append(
                        f"{where}: {_SERVICE_DSN_KEY} username does not match the selected "
                        f"service role {service_role!r} (or the DSN is unverifiable) — "
                        f"provide a plaintext DSN for that exact role"
                    )
            elif key == _REQUEST_DSN_KEY and kind == "services":
                if dsn_username(value) != _REQUEST_ROLE:
                    result.errors.append(
                        f"{where}: {_REQUEST_DSN_KEY} username must be {_REQUEST_ROLE!r} "
                        f"(the request pool never changes role) — got an unverifiable or "
                        f"different username"
                    )
    if not seen_role_key:
        result.errors.append(
            f"candidate declares no {_SERVICE_ROLE_KEY} env — the selected service role "
            f"has nowhere to bind; declare it app-level in the candidate"
        )


def _preserve_rotation_previous_key(
    result: BuildResult,
    live: dict[str, Any],
    live_idx: dict[tuple[str, str, str, str], dict[str, Any]],
    secrets_dir: Path,
) -> None:
    """If the LIVE app carries TOKEN_ENCRYPTION_KEY_PREVIOUS (a rotation in progress),
    losing it could make existing OAuth-token ciphertext UNDECRYPTABLE. The builder
    therefore PRESERVES it in every token-decrypting destination — every output
    component that declares TOKEN_ENCRYPTION_KEY — or ABORTS. A note is never enough.

    Resolution per destination: identity-unchanged live entry (kind, name, key, scope)
    is carried; otherwise the mode-0600 secrets file (`<component>.{key}` / `{key}`) is
    required — missing/invalid file is an ERROR (no output is written).

    Deliberate retirement instead requires editing the LIVE spec first (removing the
    key there, after independently proving no ciphertext still needs it — see
    core/encryption.py rotation semantics); this builder never drops it silently.
    When the live app does not carry the key (fresh environment / no rotation), nothing
    is required and nothing is added."""
    assert result.spec is not None
    live_has_previous = any(
        str(e.get("key")) == _ROTATION_PREVIOUS and str(e.get("value") or "").strip()
        for _, _, _, envs in _iter_scopes(live)
        for e in envs
    )
    if not live_has_previous:
        return
    for kind, name, where, envs in _iter_scopes(result.spec):
        keys = {str(e.get("key")) for e in envs}
        if _ROTATION_CURRENT not in keys or _ROTATION_PREVIOUS in keys:
            continue  # not a token-decrypting component / already resolved by main loop
        scope = next(
            str(e.get("scope", "RUN_TIME")) for e in envs if str(e.get("key")) == _ROTATION_CURRENT
        )
        entry: dict[str, Any] = {
            "key": _ROTATION_PREVIOUS,
            "scope": scope,
            "type": "SECRET",
        }
        live_entry = live_idx.get((kind, name, _ROTATION_PREVIOUS, scope))
        if live_entry is not None and str(live_entry.get("value") or "").strip():
            entry["value"] = str(live_entry["value"])
            result.report.append((where, _ROTATION_PREVIOUS, "carried-ev"))
        else:
            secret_value, source = _read_secret_file(secrets_dir, name, _ROTATION_PREVIOUS)
            if secret_value is None:
                result.errors.append(
                    f"{where}: the LIVE app carries {_ROTATION_PREVIOUS} (rotation in "
                    f"progress) — it must be preserved in every token-decrypting "
                    f"component, and {source} in the secrets dir. Provide "
                    f"{name}.{_ROTATION_PREVIOUS} (or {_ROTATION_PREVIOUS}) as a "
                    f"non-empty mode-0600 file, or retire the key on the LIVE spec "
                    f"first with independent proof no ciphertext needs it"
                )
                continue
            entry["value"] = secret_value
            result.report.append((where, _ROTATION_PREVIOUS, f"injected-file:{source}"))
        envs.append(entry)


def _write_0600(path: Path, text: str) -> None:
    """Write the secret-bearing spec with hard guarantees, not best-effort:
    - the output path must NOT exist (O_EXCL): O_CREAT's mode applies only on
      creation, so writing over a pre-existing 0644 file would leave it 0644;
    - symlinks are rejected (explicit check + O_NOFOLLOW where supported) so the
      write cannot be redirected outside the 0700 working directory;
    - fchmod(0600) after open pins the mode regardless of umask or platform;
    - EVERY byte lands: os.write may return a SHORT COUNT without raising, so the
      write loops on the returned count until the buffer is exhausted, then the
      on-disk size is verified against the payload;
    - a partial write never survives: any failure unlinks the file and re-raises."""
    if path.is_symlink():
        raise FileExistsError(f"{path} is a symlink — refusing to write secrets through it")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    data = text.encode()
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(f"os.write returned {written} — refusing a silent short write")
            view = view[written:]
        if os.fstat(fd).st_size != len(data):
            raise OSError("on-disk size mismatch after write — refusing a short write")
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    os.close(fd)


def main(argv: list[str] | None = None) -> int:
    import yaml  # type: ignore[import-untyped]

    p = argparse.ArgumentParser(description="Build the populated cutover spec (fail-closed).")
    p.add_argument("--candidate", required=True, help="committed shape contract (valueless)")
    p.add_argument("--live", required=True, help="freshly fetched live spec (mode 0600)")
    p.add_argument(
        "--secrets", required=True, help="dir of mode-0600 <component>.<KEY>/<KEY> files"
    )
    p.add_argument("--out", required=True, help="output path (written mode 0600)")
    p.add_argument(
        "--service-role",
        default="service_worker",
        help="the SELECTED service role: 'service_worker' (standard cutover) or "
        "'service_worker_vN' (Phase B-alt). Bound into SERVICE_ROLE_NAME and enforced "
        "against every SERVICE_DATABASE_URL username; no other value is accepted.",
    )
    args = p.parse_args(argv)

    with open(args.candidate) as f:
        candidate = dict(yaml.safe_load(f))
    with open(args.live) as f:
        live = dict(yaml.safe_load(f))

    result = build(candidate, live, Path(args.secrets).expanduser(), args.service_role)

    for where, key, resolution in result.report:
        print(f"build_cutover_spec {where}: {key} <- {resolution}")
    for note in result.notes:
        print(f"build_cutover_spec NOTE: {note}")
    if result.errors:
        for err in result.errors:
            print(f"build_cutover_spec FAIL: {err}", file=sys.stderr)
        print(
            f"build_cutover_spec: {len(result.errors)} unresolved item(s) — NO spec "
            f"written; nothing may be applied",
            file=sys.stderr,
        )
        return 1

    assert result.spec is not None
    try:
        _write_0600(Path(args.out).expanduser(), yaml.safe_dump(result.spec, sort_keys=False))
    except FileExistsError as exc:
        print(
            f"build_cutover_spec FAIL: {exc} — the output path must not exist "
            f"(rm the stale file and re-run; never overwrite in place)",
            file=sys.stderr,
        )
        return 1
    print(
        f"build_cutover_spec OK: wrote {args.out} (mode 0600) — validate with "
        f'`doctl apps spec validate` and `"$PY" -m scripts.deploy_verify --cutover` '
        f"before the single apply; unlink after use"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
