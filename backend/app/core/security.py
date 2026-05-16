"""JWT verification, identity resolution, and RBAC guards.

WorkOS issues RS256 JWTs.  The verifier caches JWKS keys with a configurable
TTL and handles outage scenarios:
  - Unknown kid inside TTL → refresh once; reject if still unknown.
  - JWKS endpoint down, cache populated → serve stale keys, log warning.
  - JWKS endpoint down, cache empty → 503 Service Unavailable.
  - Malformed JWKS response → keep existing cache, log warning.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx
import jwt
import jwt.algorithms
from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

Role = Literal["owner", "manager", "staff"]
_ROLE_RANK: dict[Role, int] = {"owner": 3, "manager": 2, "staff": 1}

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class Identity:
    """Raw JWT claims — user is authenticated but not yet bound to a tenant."""

    workos_id: str
    email: str
    email_verified: bool
    first_name: str | None
    last_name: str | None


@dataclass(frozen=True, slots=True)
class Principal:
    """Fully resolved identity + tenant + role."""

    user_id: str
    workos_id: str
    email: str
    tenant_id: str
    role: Role


# ── JWKS-backed RS256 verifier ────────────────────────────────────────────────


class JWTVerifier:
    def __init__(
        self,
        *,
        jwks_url: str,
        issuer: str,
        client_id: str | None = None,
        verify_audience: bool = False,
        ttl: float = 300.0,
    ) -> None:
        self._jwks_url = jwks_url
        self._issuer = issuer
        self._client_id = client_id
        self._verify_audience = verify_audience
        self._ttl = ttl
        self._keys: dict[str, Any] = {}
        self._fetched_at: float = 0.0
        self._lock: asyncio.Lock = asyncio.Lock()

    async def verify(self, token: str) -> dict[str, Any]:
        """Verify token and return validated claims."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise HTTPException(status_code=401, detail="Invalid token format") from exc

        alg = header.get("alg", "")
        if alg != "RS256":
            raise HTTPException(status_code=401, detail=f"Unsupported algorithm: {alg!r}")

        kid = header.get("kid")
        if not kid:
            raise HTTPException(status_code=401, detail="Missing kid in token header")

        key = await self._get_key(kid)

        decode_opts: Any = {
            "verify_exp": True,
            "verify_iss": True,
            "verify_aud": self._verify_audience,
        }
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._client_id if self._verify_audience else None,
                options=decode_opts,
            )
        except jwt.ExpiredSignatureError as exc:
            raise HTTPException(status_code=401, detail="Token expired") from exc
        except jwt.InvalidIssuerError as exc:
            raise HTTPException(status_code=401, detail="Wrong issuer") from exc
        except jwt.InvalidAudienceError as exc:
            raise HTTPException(status_code=401, detail="Wrong audience") from exc
        except jwt.InvalidTokenError as exc:
            raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc

        return claims

    async def _get_key(self, kid: str) -> Any:
        stale = time.monotonic() - self._fetched_at >= self._ttl
        if kid in self._keys and not stale:
            return self._keys[kid]

        async with self._lock:
            # Re-check after acquiring lock.
            stale = time.monotonic() - self._fetched_at >= self._ttl
            if kid in self._keys and not stale:
                return self._keys[kid]
            await self._refresh_jwks()

        if kid in self._keys:
            return self._keys[kid]
        raise HTTPException(status_code=401, detail=f"Unknown key id: {kid!r}")

    async def _refresh_jwks(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(self._jwks_url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            if self._keys:
                log.warning("jwks_refresh_failed_using_stale_cache", error=str(exc))
                return
            raise HTTPException(status_code=503, detail="Auth service unavailable") from exc

        try:
            new_keys: dict[str, Any] = {}
            for key_data in data.get("keys", []):
                kid = key_data.get("kid")
                if not kid:
                    continue
                try:
                    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)
                    new_keys[kid] = public_key
                except Exception as key_exc:
                    log.warning("jwks_bad_key_skipped", kid=kid, error=str(key_exc))
                    continue
            if new_keys:
                self._keys = new_keys
                self._fetched_at = time.monotonic()
            elif self._keys:
                log.warning("jwks_parse_returned_no_keys_keeping_stale_cache")
            else:
                raise HTTPException(status_code=503, detail="Auth service unavailable")
        except HTTPException:
            raise
        except Exception as exc:
            if self._keys:
                log.warning("jwks_parse_failed_keeping_stale_cache", error=str(exc))
                return
            raise HTTPException(status_code=503, detail="Auth service unavailable") from exc


# ── Singleton verifier ────────────────────────────────────────────────────────

_verifier: JWTVerifier | None = None


def get_jwt_verifier() -> JWTVerifier:
    global _verifier
    if _verifier is None:
        s = get_settings()
        if not s.workos_jwks_url:
            raise HTTPException(
                status_code=503,
                detail="Auth not configured (WORKOS_JWKS_URL missing)",
            )
        _verifier = JWTVerifier(
            jwks_url=s.workos_jwks_url,
            issuer=s.workos_issuer,
            client_id=s.workos_client_id,
            verify_audience=s.workos_verify_audience,
        )
    return _verifier


def reset_verifier() -> None:
    """Clear singleton — used in tests when settings change."""
    global _verifier
    _verifier = None


# ── FastAPI dependencies ──────────────────────────────────────────────────────


def _parse_email_verified(val: bool | str | int | None) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() not in ("false", "0", "no", "")
    if isinstance(val, int):
        return val != 0
    return False


async def _fetch_workos_profile(workos_user_id: str) -> dict[str, Any]:
    """Fetch user profile from WorkOS API when JWT lacks email claim.

    WorkOS User Management tokens omit profile data by default.  The secret
    key (WORKOS_SECRET_KEY) must be set for this to succeed.
    """
    s = get_settings()
    if not s.workos_secret_key:
        raise HTTPException(
            status_code=503,
            detail="Auth not configured (WORKOS_SECRET_KEY missing — cannot fetch user profile)",
        )
    url = f"https://api.workos.com/user_management/users/{workos_user_id}"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {s.workos_secret_key}"})
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Could not retrieve user profile from WorkOS")
    return resp.json()  # type: ignore[no-any-return]


async def get_identity(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    verifier: JWTVerifier = Depends(get_jwt_verifier),
) -> Identity:
    """Validate JWT and return Identity.  Raises 401 on any failure."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    claims = await verifier.verify(credentials.credentials)

    workos_id: str = claims["sub"]

    # WorkOS User Management JWTs omit profile claims; fetch from API if needed.
    if claims.get("email"):
        email: str = claims["email"]
        email_verified = _parse_email_verified(claims.get("email_verified"))
        first_name: str | None = claims.get("first_name") or claims.get("given_name")
        last_name: str | None = claims.get("last_name") or claims.get("family_name")
    else:
        profile = await _fetch_workos_profile(workos_id)
        email = profile["email"]
        email_verified = bool(profile.get("email_verified", False))
        first_name = profile.get("first_name")
        last_name = profile.get("last_name")

    return Identity(
        workos_id=workos_id,
        email=email,
        email_verified=email_verified,
        first_name=first_name,
        last_name=last_name,
    )


async def get_principal(
    identity: Identity = Depends(get_identity),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> Principal:
    """Resolve Identity → Principal by looking up DB membership."""
    from app.modules.tenants.repo import resolve_principal

    return await resolve_principal(identity, x_tenant_id)


def require_role(min_role: Role) -> Any:
    """Dependency factory: 403 if principal's role is below min_role."""

    async def _check(principal: Principal = Depends(get_principal)) -> Principal:
        if _ROLE_RANK[principal.role] < _ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=403,
                detail=f"Requires {min_role} or above; you have {principal.role}",
            )
        return principal

    return Depends(_check)
