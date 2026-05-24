"""Token refresh background job.

Scans tenant_pos_connections for access tokens expiring within 10 minutes,
refreshes them via Clover's OAuth refresh endpoint, and increments
refresh_failure_count on failure. After 3 consecutive failures, the
connection transitions to state='error'.

Also cleans up expired oauth_states rows (single DELETE, runs in same job).

Recovery via /oauth/v2/recovery is explicitly deferred to Sprint 7+.
prev_refresh_token_enc is stored on every refresh as a safety net.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from sqlalchemy import text

from app.core.config import get_settings
from app.core.encryption import TokenEncryption
from app.core.service_db import get_service_sessionmaker

_ENV_API_BASES = {
    "sandbox": "https://apisandbox.dev.clover.com",
    "production": "https://api.clover.com",
    "eu": "https://api.eu.clover.com",
    "latam": "https://api.la.clover.com",
}


def _api_base(environment: str) -> str:
    return _ENV_API_BASES.get(environment, _ENV_API_BASES["sandbox"])


def _from_unix_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


async def refresh_expiring_tokens() -> None:
    """Refresh all active connections whose access tokens expire in <10 minutes.

    Runs under FOR UPDATE SKIP LOCKED so multiple workers don't race on the
    same connection (Sprint 4 has one worker; the lock is forward-compatible).

    Failure counter logic:
      - success: refresh_failure_count reset to 0
      - failure: refresh_failure_count += 1
      - 3rd failure: state → 'error' (webhook handler still routes via lookup)
    """
    settings = get_settings()
    enc = TokenEncryption()
    sm = get_service_sessionmaker()

    async with sm() as session:
        async with session.begin():
            result = await session.execute(text("""
                SELECT connection_id, tenant_id, environment,
                       refresh_token_enc, refresh_failure_count
                FROM tenant_pos_connections
                WHERE state = 'active'
                  AND access_token_expires_at < now() + interval '10 minutes'
                FOR UPDATE SKIP LOCKED
            """))
            connections = result.mappings().all()

            for conn in connections:
                try:
                    old_refresh = enc.decrypt(conn["refresh_token_enc"])
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.post(
                            f"{_api_base(conn['environment'])}/oauth/v2/refresh",
                            json={
                                "client_id": settings.clover_app_id,
                                "refresh_token": old_refresh,
                            },
                        )
                    resp.raise_for_status()
                    tokens = resp.json()

                    await session.execute(text("""
                        UPDATE tenant_pos_connections
                        SET prev_refresh_token_enc    = refresh_token_enc,
                            access_token_enc          = :at_enc,
                            access_token_expires_at   = :at_exp,
                            refresh_token_enc         = :rt_enc,
                            refresh_token_expires_at  = :rt_exp,
                            last_token_refresh_at     = now(),
                            refresh_failure_count     = 0,
                            state_reason              = NULL,
                            updated_at                = now()
                        WHERE connection_id = :cid
                    """), {
                        "at_enc": enc.encrypt(tokens["access_token"]),
                        "at_exp": _from_unix_ms(tokens["access_token_expiration"]),
                        "rt_enc": enc.encrypt(tokens["refresh_token"]),
                        "rt_exp": _from_unix_ms(tokens["refresh_token_expiration"]),
                        "cid": str(conn["connection_id"]),
                    })

                except Exception as exc:
                    new_count = conn["refresh_failure_count"] + 1
                    new_state = "error" if new_count >= 3 else "active"
                    await session.execute(text("""
                        UPDATE tenant_pos_connections
                        SET refresh_failure_count = :cnt,
                            state                 = :st,
                            state_reason          = :err,
                            updated_at            = now()
                        WHERE connection_id = :cid
                    """), {
                        "cnt": new_count,
                        "st": new_state,
                        "err": str(exc)[:500],
                        "cid": str(conn["connection_id"]),
                    })

    # Cleanup expired OAuth states — separate transaction, best-effort
    async with sm() as session:
        await session.execute(text("DELETE FROM oauth_states WHERE expires_at < now()"))
        await session.commit()
