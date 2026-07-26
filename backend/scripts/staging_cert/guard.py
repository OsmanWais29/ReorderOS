"""Pure target-identity guard for the staging cert harness.

No app or DB imports — importable and unit-testable in isolation. The harness
uses these to prove it is pointed at the KNOWN staging database (an allowlist),
so production and any unknown database are refused regardless of flags.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlparse

# Non-secret SHA-256(host:port/dbname)[:16] of the KNOWN staging database. This is
# an ALLOWLIST: the run proceeds ONLY when the runtime DB matches. Production and
# any unknown DB therefore refuse — the guard proves the actual target, not a flag.
STAGING_DB_FINGERPRINT = "c3fd8611b8ac25ec"
ALLOWED_FINGERPRINTS = frozenset({STAGING_DB_FINGERPRINT})


def db_fingerprint(url: str) -> str:
    """Non-secret, stable identity of a DB from its URL (host:port/dbname). Pure —
    contains no credentials, so it is safe to commit and to print."""
    p = urlparse(url.replace("postgresql+asyncpg://", "postgresql://"))
    ident = f"{p.hostname}:{p.port}/{(p.path or '').lstrip('/')}"
    return hashlib.sha256(ident.encode()).hexdigest()[:16]


def guard_decision(
    *,
    allow_flag: str | None,
    fingerprint: str,
    allowed: frozenset[str] = ALLOWED_FINGERPRINTS,
) -> tuple[bool, str]:
    """PURE target decision. Proceeds ONLY when the human-intent flag is set AND the
    fingerprint is on the staging allowlist. Production or unknown → refused."""
    if allow_flag != "1":
        return False, "ALLOW_STAGING_CERT is not set to 1 (human-intent gate)"
    if fingerprint not in allowed:
        return False, f"DB fingerprint {fingerprint} is not the allowed staging identity"
    return True, "ok"
