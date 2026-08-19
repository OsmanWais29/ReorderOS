"""Per-component production configuration requirements — the single source of truth.

Imported by BOTH ``app.core.config`` (Settings' production fail-closed check) and
``app.ops.env_check`` (profiles for the CLI / predeploy / boot gates). This module is
deliberately neutral: it imports neither of them and holds no I/O — just data.

Every requirement below is CODE-BACKED by the dependency trace in
docs/security/restricted-runtime-role-matrix.md (§ "Component dependency matrix"),
which records the consuming file:line for each secret, and is executable-proven by
tests/test_component_requirements.py (boot-with-exactly-declared-config matrix) plus
the feature-path fake-provider suites cited in the matrix.

Key trace facts (do not widen a set without new evidence):
  - CLOVER_APP_SECRET   → pos/router.py OAuth exchange ONLY (api). No worker consumes it.
  - CLOVER_WEBHOOK_AUTH_CODE → pos/webhook.py ONLY (api).
  - CLOVER_APP_ID       → pos/router.py (api) + pos/token_refresh.py (reconciliation_worker).
  - TOKEN_ENCRYPTION_KEY → pos/router+catalog_sync (api), pos/worker (inbox_worker),
    reconciliation+token_refresh (reconciliation_worker). NOT extraction / inbound-email.
  - ANTHROPIC_API_KEY   → recipes/router.py (api suggestions) + receipt_extraction_worker.
  - Spaces              → receipts/{services,router,inbound_webhook} + observability (api),
    extraction_worker (worker).
  - WORKOS_SECRET_KEY / Postmark Basic Auth pair → api only.
  - POSTMARK_INBOUND_ADDRESS is CONFIGURATION, not a credential (inbound_admin.py reports
    configured:false without it) — deliberately NOT a required secret here.

``TOKEN_ENCRYPTION_KEY_PREVIOUS`` is OPTIONAL everywhere: preserved when present
(rotation support, core/encryption.py), but its absence must never fail a fresh
environment — so it appears in OPTIONAL_VARS, never in a component's required set.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Var:
    name: str
    secret: bool = False  # secret keys get the production placeholder check in env_check
    # Required only when this feature-flag env var is truthy ("1"/"true"/"yes"/"on") —
    # e.g. Clover secrets only matter when CLOVER_ENABLED=true.
    when: str | None = None


_DB = (Var("DATABASE_URL", secret=True),)
_SERVICE_DB = (Var("SERVICE_DATABASE_URL", secret=True),)
_TOKENS = (Var("TOKEN_ENCRYPTION_KEY", secret=True),)
_SPACES = (
    Var("DO_SPACES_ENDPOINT"),
    Var("DO_SPACES_REGION"),
    Var("DO_SPACES_BUCKET"),
    Var("DO_SPACES_KEY", secret=True),
    Var("DO_SPACES_SECRET", secret=True),
)
_WORKOS = (
    Var("WORKOS_CLIENT_ID"),
    Var("WORKOS_JWKS_URL"),
    Var("WORKOS_ISSUER"),
    Var("WORKOS_SECRET_KEY", secret=True),
)
_ANTHROPIC = (Var("ANTHROPIC_API_KEY", secret=True),)
_CLOVER_FULL = (
    Var("CLOVER_APP_ID", when="CLOVER_ENABLED"),
    Var("CLOVER_APP_SECRET", secret=True, when="CLOVER_ENABLED"),
    Var("CLOVER_WEBHOOK_AUTH_CODE", secret=True, when="CLOVER_ENABLED"),
)
_POSTMARK = (
    Var("POSTMARK_WEBHOOK_USER", secret=True, when="POSTMARK_INBOUND_ENABLED"),
    Var("POSTMARK_WEBHOOK_PASSWORD", secret=True, when="POSTMARK_INBOUND_ENABLED"),
)


def dedupe(*groups: tuple[Var, ...]) -> tuple[Var, ...]:
    seen: dict[str, Var] = {}
    for g in groups:
        for v in g:
            # secret=True wins if the same key appears in multiple groups
            if v.name not in seen or v.secret:
                seen[v.name] = v
    return tuple(seen.values())


_dedupe = dedupe  # internal alias used below


# ── The components ────────────────────────────────────────────────────────────
COMPONENTS: dict[str, tuple[Var, ...]] = {
    # API request path: everything it genuinely consumes (see module docstring trace).
    "api": _dedupe(
        _DB, _SERVICE_DB, _TOKENS, _WORKOS, _ANTHROPIC, _CLOVER_FULL, _SPACES, _POSTMARK
    ),
    # POS event inbox drain: decrypts merchant tokens, calls Clover with them (Bearer) —
    # needs NO Clover app credentials (trace: CLOVER_APP_SECRET has no worker consumer).
    "inbox_worker": _dedupe(_SERVICE_DB, _TOKENS),
    # POS reconciliation + token refresh: refresh grant sends client_id
    # (pos/token_refresh.py) but NOT the app secret.
    "reconciliation_worker": _dedupe(
        _SERVICE_DB, _TOKENS, (Var("CLOVER_APP_ID", when="CLOVER_ENABLED"),)
    ),
    # Receipt photo/PDF -> line extraction: downloads bytes from Spaces, calls Anthropic.
    "receipt_extraction_worker": _dedupe(_SERVICE_DB, _SPACES, _ANTHROPIC),
    # Inbound email fan-out only: reads inbox rows, creates drafts + jobs. No Spaces
    # (the webhook uploaded the bytes), no Anthropic, no token decryption.
    "inbound_email_worker": _dedupe(_SERVICE_DB),
    # DDL only: alembic/env.py reads DATABASE_URL directly (no Settings construction).
    "migrate_job": _dedupe(_DB),
    # LEGACY compatibility profile — byte-for-byte the pre-APP_COMPONENT global
    # fail-closed set (Settings F1.3). Selected ONLY when APP_COMPONENT is unset AND
    # RESTRICTED_RUNTIME_ROLES_ENABLED is false; pins production's current behavior.
    "legacy": _dedupe(
        _TOKENS,
        _SERVICE_DB,
        (Var("WORKOS_CLIENT_ID"), Var("WORKOS_JWKS_URL")),
        _CLOVER_FULL,
        _POSTMARK,
    ),
}

# Valid APP_COMPONENT values when the restricted-role flag is ON. "legacy" is
# deliberately excluded: under the cutover flag every component must self-identify.
RESTRICTED_COMPONENTS: frozenset[str] = frozenset(COMPONENTS) - {"legacy"}

# Optional configuration: preserved when present, never required.
OPTIONAL_VARS: tuple[Var, ...] = (
    Var("TOKEN_ENCRYPTION_KEY_PREVIOUS", secret=True),
    Var("POSTMARK_INBOUND_ADDRESS"),  # configuration, not a credential (see docstring)
)


def required_vars(component: str, flags: Mapping[str, bool]) -> tuple[Var, ...]:
    """The component's required Vars, honoring `when=` feature gates via `flags`
    (a mapping of flag env-name -> enabled). Unknown component -> KeyError."""
    return tuple(v for v in COMPONENTS[component] if v.when is None or flags.get(v.when, False))


def required_env_names(component: str, flags: Mapping[str, bool]) -> tuple[str, ...]:
    return tuple(v.name for v in required_vars(component, flags))
