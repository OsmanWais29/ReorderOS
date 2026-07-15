"""Environment readiness — make dangling-secret failures impossible to miss.

Born from the 2026-07-15 prod incident: DigitalOcean SECRET env vars silently
decrypted to EMPTY STRINGS, every main deploy failed fail-closed and
auto-rolled back for TEN DAYS, and nothing surfaced it. This module is the one
place that knows what every component needs, and it reports readiness by KEY
NAME ONLY — a secret value never appears in any output, log, or exception.

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


@dataclass(frozen=True)
class Var:
    name: str
    secret: bool = False  # secret keys get the production placeholder check
    # Required only when this env var is truthy ("1"/"true"/"yes"/"on") —
    # e.g. Clover secrets only matter when CLOVER_ENABLED=true.
    when: str | None = None


_DB = (Var("DATABASE_URL", secret=True),)
_SERVICE_DB = (Var("SERVICE_DATABASE_URL", secret=True),)
_SPACES = (
    Var("DO_SPACES_ENDPOINT"),
    Var("DO_SPACES_REGION"),
    Var("DO_SPACES_BUCKET"),
    Var("DO_SPACES_KEY", secret=True),
    Var("DO_SPACES_SECRET", secret=True),
)
_CLOVER = (
    Var("CLOVER_APP_ID", when="CLOVER_ENABLED"),
    Var("CLOVER_APP_SECRET", secret=True, when="CLOVER_ENABLED"),
    Var("CLOVER_WEBHOOK_AUTH_CODE", secret=True, when="CLOVER_ENABLED"),
)
_WORKOS = (
    Var("WORKOS_CLIENT_ID"),
    Var("WORKOS_JWKS_URL"),
    Var("WORKOS_ISSUER"),
    Var("WORKOS_SECRET_KEY", secret=True),
)
_TOKENS = (Var("TOKEN_ENCRYPTION_KEY", secret=True),)
_POSTMARK = (
    Var("POSTMARK_WEBHOOK_USER", secret=True, when="POSTMARK_INBOUND_ENABLED"),
    Var("POSTMARK_WEBHOOK_PASSWORD", secret=True, when="POSTMARK_INBOUND_ENABLED"),
)

# alembic/env.py loads full Settings, whose production fail-closed check demands
# these four — so the migrate job needs them even though alembic itself only
# consumes DATABASE_URL. Checking here means the job dies with a NAMED report
# instead of a pydantic traceback.
_SETTINGS_FAIL_CLOSED = (
    Var("TOKEN_ENCRYPTION_KEY", secret=True),
    Var("SERVICE_DATABASE_URL", secret=True),
    Var("CLOVER_APP_SECRET", secret=True, when="CLOVER_ENABLED"),
    Var("CLOVER_WEBHOOK_AUTH_CODE", secret=True, when="CLOVER_ENABLED"),
)


def _dedupe(*groups: tuple[Var, ...]) -> tuple[Var, ...]:
    seen: dict[str, Var] = {}
    for g in groups:
        for v in g:
            # secret=True wins if the same key appears in multiple groups
            if v.name not in seen or v.secret:
                seen[v.name] = v
    return tuple(seen.values())


PROFILES: dict[str, tuple[Var, ...]] = {
    "api": _dedupe(_DB, _SERVICE_DB, _TOKENS, _WORKOS, _CLOVER, _SPACES, _POSTMARK),
    "receipt_extraction_worker": _dedupe(
        _SERVICE_DB, _SPACES, (Var("ANTHROPIC_API_KEY", secret=True),)
    ),
    # Fan-out only: reads inbox rows, creates drafts + jobs. No Spaces (the
    # webhook uploaded the bytes), no Anthropic (extraction is a separate worker).
    "inbound_email_worker": _dedupe(_SERVICE_DB),
    "inbox_worker": _dedupe(
        _SERVICE_DB,
        _TOKENS,
        (
            Var("CLOVER_APP_ID", when="CLOVER_ENABLED"),
            Var("CLOVER_APP_SECRET", secret=True, when="CLOVER_ENABLED"),
        ),
    ),
    "migrate_job": _dedupe(_DB, _SETTINGS_FAIL_CLOSED),
}
# Everything any prod component needs — the predeploy gate.
PROFILES["predeploy_env_check"] = _dedupe(*PROFILES.values())
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
