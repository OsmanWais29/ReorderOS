"""Environment readiness — make dangling-secret failures impossible to miss.

Born from the 2026-07-15 prod incident: DigitalOcean SECRET env vars silently
decrypted to EMPTY STRINGS, every main deploy failed fail-closed and
auto-rolled back for TEN DAYS, and nothing surfaced it. Readiness is reported
by KEY NAME ONLY — a secret value never appears in any output, log, or exception.

WHAT each component requires is defined in ONE place —
``app.core.component_requirements`` (code-backed, trace-documented) — shared with
Settings' production fail-closed check so the two can never drift. This module adds
the HOW: env inspection, rejection rules, CLI, and the predeploy union profile.

Used four ways (same validation everywhere):
  - `python -m app.ops.env_check --profile <name>`  → predeploy job / CI gate
    (exit 0 ready / exit 1 unsafe — chain as `env_check && alembic upgrade head`)
  - API startup (production): app.main lifespan fails loudly before serving
  - Worker startup (production): runners exit(1) before touching the queue
  - Tests: profiles are data, so coverage is assertable

Rejection rules per key:
  - missing            → fail (all environments)
  - empty / whitespace → fail (all environments) — the dangling-secret signature
  - placeholder tokens → fail for SECRET keys when APP_ENV=production
    ("false", "null", "none", "undefined", "changeme", "placeholder",
     "redacted", "dummy", "test", "example", "xxx", "todo", "client_test",
     "sk_test") — non-secret keys are exempt (WORKOS_VERIFY_AUDIENCE="false"
    is legitimate configuration).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass

from app.core.component_requirements import COMPONENTS, Var, dedupe

__all__ = ["PROFILES", "EnvReport", "Var", "check_env", "main"]

# Values that mean "someone never set this for real". Compared case-insensitively
# against the FULL value, so real secrets containing these substrings still pass.
_PLACEHOLDERS = frozenset(
    {
        "false",
        "null",
        "none",
        "undefined",
        "changeme",
        "change-me",
        "placeholder",
        "redacted",
        "dummy",
        "test",
        "example",
        "xxx",
        "todo",
        "client_test",
        "sk_test",
    }
)

# Profiles ARE the component requirement sets (single source of truth). Every runtime
# component — including reconciliation_worker (previously missing) — has a profile and
# a boot-time check_env gate in its runner.
PROFILES: dict[str, tuple[Var, ...]] = dict(COMPONENTS)
# Everything any prod component needs — the predeploy gate. ("legacy" adds nothing:
# it is a subset of the api profile's key set, kept for completeness.)
PROFILES["predeploy_env_check"] = dedupe(*COMPONENTS.values())
PROFILES["production_deploy"] = PROFILES["predeploy_env_check"]  # alias


@dataclass(frozen=True)
class EnvReport:
    profile: str
    ready: bool
    # key -> True (ready) / False (failed); never values.
    results: dict[str, bool]
    # key -> failure reason ('missing' | 'empty' | 'placeholder'); ready keys absent.
    failures: dict[str, str]


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _check_one(var: Var, environ: Mapping[str, str], production: bool) -> str | None:
    if var.when is not None:
        gate = (environ.get(var.when) or "").strip().lower()
        if gate not in _TRUTHY:
            return None  # feature off → var not required
    raw = environ.get(var.name)
    if raw is None:
        return "missing"
    if not raw.strip():
        return "empty"
    if production and var.secret and raw.strip().lower() in _PLACEHOLDERS:
        return "placeholder"
    return None


def check_env(
    profile: str,
    environ: Mapping[str, str] | None = None,
) -> EnvReport:
    """Validate `profile`'s required env. Output contains KEY NAMES ONLY."""
    if profile not in PROFILES:
        raise KeyError(f"unknown env profile {profile!r} (have: {sorted(PROFILES)})")
    env = environ if environ is not None else os.environ
    production = (env.get("APP_ENV") or "").strip().lower() == "production"
    results: dict[str, bool] = {}
    failures: dict[str, str] = {}
    for var in PROFILES[profile]:
        reason = _check_one(var, env, production)
        results[var.name] = reason is None
        if reason is not None:
            failures[var.name] = reason
    return EnvReport(profile=profile, ready=not failures, results=results, failures=failures)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Environment readiness check (names only)")
    parser.add_argument("--profile", required=True, choices=sorted(PROFILES))
    args = parser.parse_args(argv)
    report = check_env(args.profile)
    for key in sorted(report.results):
        status = "ok" if report.results[key] else f"FAIL({report.failures[key]})"
        print(f"env_check {report.profile} {key}={status}")
    if not report.ready:
        print(
            f"env_check {report.profile} NOT READY — "
            f"{len(report.failures)} key(s) unsafe: {', '.join(sorted(report.failures))}",
            file=sys.stderr,
        )
        return 1
    print(f"env_check {report.profile} READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
