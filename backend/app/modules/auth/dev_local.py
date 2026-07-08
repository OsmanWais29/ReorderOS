"""LOCAL-ONLY dev sign-in — a WorkOS-free auth path for laptop smoke tests.

DOUBLE-GATED, fail-closed:
  1. `LOCAL_DEV_AUTH=true` must be set explicitly (default false), AND
  2. `APP_ENV` must be `local` or `ci`.
Staging and production NEVER qualify regardless of the flag — the gate is
re-checked on EVERY request (both at the sign-in endpoint and at token
verification), so an accidentally-set flag in a production deploy changes
nothing: the endpoint 404s and dev tokens are never even attempted.

The WorkOS path is untouched: when the gate is closed, `get_identity` is
byte-for-byte the pre-existing code path; when open, non-dev tokens still fall
through to the WorkOS verifier unchanged.

Token: HS256 JWT with issuer `reorderos-dev-local`, signed with a key DERIVED
from TOKEN_ENCRYPTION_KEY (an env-provided secret that already exists in every
environment — nothing new lands in the repo). Without that env secret a dev
token cannot be minted or verified.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import jwt

from app.core.config import Settings

DEV_ISSUER = "reorderos-dev-local"
DEV_WORKOS_ID = "devlocal_user_01"
DEV_EMAIL = "dev@local.test"
DEV_TENANT_SLUG = "dev-local"
DEV_TENANT_NAME = "Dev Local Kitchen"
_TOKEN_TTL_SECONDS = 12 * 3600

# APP_ENV values allowed to use dev auth. Staging/production are structurally
# absent — adding them would require editing this line, not flipping an env var.
_ALLOWED_ENVS = frozenset({"local", "ci"})


def dev_local_auth_enabled(settings: Settings) -> bool:
    """The double gate. Checked on EVERY request that could touch dev auth."""
    return (
        bool(settings.local_dev_auth)
        and settings.app_env in _ALLOWED_ENVS
        and bool(settings.token_encryption_key)  # signing material must exist
    )


def _signing_key(settings: Settings) -> bytes:
    """Derive the HS256 key from TOKEN_ENCRYPTION_KEY — a per-environment secret
    that is never in the repo. Domain-separated so the derived key is useless
    for anything but this issuer."""
    assert settings.token_encryption_key is not None  # guarded by the gate
    return hmac.new(
        settings.token_encryption_key.encode(),
        b"reorderos-dev-local-auth-v1",
        hashlib.sha256,
    ).digest()


def mint_dev_token(settings: Settings) -> str:
    """Mint the dev user's token. Caller MUST have checked the gate."""
    now = int(time.time())
    return jwt.encode(
        {
            "iss": DEV_ISSUER,
            "sub": DEV_WORKOS_ID,
            "email": DEV_EMAIL,
            "email_verified": True,
            "first_name": "Dev",
            "last_name": "Manager",
            "iat": now,
            "exp": now + _TOKEN_TTL_SECONDS,
        },
        _signing_key(settings),
        algorithm="HS256",
    )


def verify_dev_token(settings: Settings, token: str) -> dict[str, Any] | None:
    """Return claims when `token` is a valid dev token, else None (fall through
    to the WorkOS verifier). Caller MUST have checked the gate — this function
    is never reached when the gate is closed."""
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "HS256":
            return None  # WorkOS tokens are RS256 — not ours, fall through
        claims: dict[str, Any] = jwt.decode(
            token,
            _signing_key(settings),
            algorithms=["HS256"],  # pinned: an RS256 token can never match
            issuer=DEV_ISSUER,
            options={"require": ["exp", "iss", "sub"]},
        )
        return claims
    except jwt.PyJWTError:
        return None
