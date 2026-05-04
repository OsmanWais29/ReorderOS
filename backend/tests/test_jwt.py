"""JWT verifier unit tests — all use fake RSA keys, no real WorkOS credentials."""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.security import JWTVerifier, _parse_email_verified

# ── email_verified parsing ────────────────────────────────────────────────────


def test_email_verified_true_bool() -> None:
    assert _parse_email_verified(True) is True


def test_email_verified_false_bool() -> None:
    assert _parse_email_verified(False) is False


def test_email_verified_string_true() -> None:
    assert _parse_email_verified("true") is True


def test_email_verified_string_false() -> None:
    assert _parse_email_verified("false") is False


def test_email_verified_string_False_capital() -> None:
    assert _parse_email_verified("False") is False


def test_email_verified_none() -> None:
    assert _parse_email_verified(None) is False


def test_email_verified_int_zero() -> None:
    assert _parse_email_verified(0) is False


def test_email_verified_int_one() -> None:
    assert _parse_email_verified(1) is True


# ── Valid RS256 token accepted ────────────────────────────────────────────────


async def test_valid_token_accepted(verifier: JWTVerifier, make_token: Any) -> None:
    token = make_token()
    claims = await verifier.verify(token)
    assert claims["sub"] == "user_workos_test"
    assert claims["email"] == "test@example.com"


# ── Algorithm attack vectors ──────────────────────────────────────────────────


async def test_alg_none_rejected(verifier: JWTVerifier) -> None:
    # Craft a raw alg:none token — PyJWT won't sign with RSA key when alg=none,
    # so we build the three-part token manually.
    import base64

    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT","kid":"test-key-1"}').rstrip(b"=")
    payload = base64.urlsafe_b64encode(
        b'{"sub":"u","email":"a@b.com","iss":"https://api.workos.com","exp":9999999999}'
    ).rstrip(b"=")
    token = f"{header.decode()}.{payload.decode()}."
    with pytest.raises(Exception) as exc_info:
        await verifier.verify(token)
    assert exc_info.value.status_code == 401  # type: ignore[attr-defined]


async def test_hs256_rejected(verifier: JWTVerifier) -> None:
    import jwt as _jwt

    token = _jwt.encode(
        {
            "sub": "u1",
            "email": "a@b.com",
            "iss": "https://api.workos.com",
            "exp": int(time.time()) + 3600,
        },
        "secret",
        algorithm="HS256",
        headers={"kid": "test-key-1"},
    )
    with pytest.raises(Exception) as exc_info:
        await verifier.verify(token)
    assert exc_info.value.status_code == 401  # type: ignore[attr-defined]


# ── Header validation ─────────────────────────────────────────────────────────


async def test_missing_kid_rejected(verifier: JWTVerifier, rsa_private_key: Any) -> None:
    import jwt as _jwt

    token = _jwt.encode(
        {
            "sub": "u1",
            "email": "a@b.com",
            "iss": "https://api.workos.com",
            "exp": int(time.time()) + 3600,
        },
        rsa_private_key,
        algorithm="RS256",
        # No kid header
    )
    with pytest.raises(Exception) as exc_info:
        await verifier.verify(token)
    assert exc_info.value.status_code == 401  # type: ignore[attr-defined]


# ── Claims validation ─────────────────────────────────────────────────────────


async def test_wrong_issuer_rejected(verifier: JWTVerifier, make_token: Any) -> None:
    token = make_token(iss="https://evil.com")
    with pytest.raises(Exception) as exc_info:
        await verifier.verify(token)
    assert exc_info.value.status_code == 401  # type: ignore[attr-defined]


async def test_expired_token_rejected(verifier: JWTVerifier, make_token: Any) -> None:
    token = make_token(exp_delta=-1)
    with pytest.raises(Exception) as exc_info:
        await verifier.verify(token)
    assert exc_info.value.status_code == 401  # type: ignore[attr-defined]


async def test_missing_email_rejected(verifier: JWTVerifier, rsa_private_key: Any) -> None:
    import jwt as _jwt

    token = _jwt.encode(
        {
            "sub": "u1",
            "iss": "https://api.workos.com",
            "exp": int(time.time()) + 3600,
            # No email claim
        },
        rsa_private_key,
        algorithm="RS256",
        headers={"kid": "test-key-1"},
    )
    with pytest.raises(Exception) as exc_info:
        await verifier.verify(token)
    assert exc_info.value.status_code == 401  # type: ignore[attr-defined]


async def test_audience_rejected_when_enabled(fake_jwks: dict[str, Any], make_token: Any) -> None:
    """When verify_audience=True, wrong audience is rejected."""
    v = JWTVerifier(
        jwks_url="https://fake.jwks/",
        issuer="https://api.workos.com",
        client_id="correct_client",
        verify_audience=True,
    )
    import jwt.algorithms

    pub_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(fake_jwks["keys"][0]))
    v._keys = {"test-key-1": pub_key}
    v._fetched_at = time.monotonic()

    token = make_token(aud="wrong_client")
    with pytest.raises(Exception) as exc_info:
        await v.verify(token)
    assert exc_info.value.status_code == 401  # type: ignore[attr-defined]


# ── JWKS rotation / caching edge cases ───────────────────────────────────────


async def test_unknown_kid_forces_refresh(fake_jwks: dict[str, Any], make_token: Any) -> None:
    """Unknown kid inside TTL triggers a refresh and then succeeds."""
    v = JWTVerifier(
        jwks_url="https://fake.jwks/",
        issuer="https://api.workos.com",
        client_id=None,
        verify_audience=False,
        ttl=9999.0,
    )
    # Cache has a DIFFERENT kid — "old-key", not "test-key-1"
    import jwt.algorithms

    pub_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(fake_jwks["keys"][0]))
    v._keys = {"old-key": pub_key}
    v._fetched_at = time.monotonic()

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = fake_jwks

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        token = make_token(kid="test-key-1")
        claims = await v.verify(token)

    assert claims["sub"] == "user_workos_test"


async def test_empty_cache_outage_returns_503(make_token: Any) -> None:
    """JWKS endpoint down + empty cache → 503."""
    v = JWTVerifier(
        jwks_url="https://down.jwks/",
        issuer="https://api.workos.com",
        client_id=None,
        verify_audience=False,
    )
    # No keys cached, endpoint is down.
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        token = make_token()
        with pytest.raises(Exception) as exc_info:
            await v.verify(token)

    assert exc_info.value.status_code == 503  # type: ignore[attr-defined]


async def test_stale_populated_cache_serves_valid_token(
    fake_jwks: dict[str, Any], make_token: Any
) -> None:
    """JWKS down but stale cache populated → token accepted, warning logged."""
    v = JWTVerifier(
        jwks_url="https://down.jwks/",
        issuer="https://api.workos.com",
        client_id=None,
        verify_audience=False,
        ttl=0.0,  # Force stale immediately
    )
    import jwt.algorithms

    pub_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(fake_jwks["keys"][0]))
    v._keys = {"test-key-1": pub_key}
    v._fetched_at = 0.0  # already stale

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        token = make_token()
        claims = await v.verify(token)

    assert claims["email"] == "test@example.com"


async def test_malformed_jwks_does_not_wipe_good_cache(
    fake_jwks: dict[str, Any], make_token: Any
) -> None:
    """Malformed JWKS response keeps existing cache intact."""
    v = JWTVerifier(
        jwks_url="https://fake.jwks/",
        issuer="https://api.workos.com",
        client_id=None,
        verify_audience=False,
        ttl=0.0,
    )
    import jwt.algorithms

    pub_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(fake_jwks["keys"][0]))
    v._keys = {"test-key-1": pub_key}
    v._fetched_at = 0.0  # stale so refresh is triggered

    malformed_response = MagicMock()
    malformed_response.raise_for_status = MagicMock()
    malformed_response.json.return_value = {"keys": [{"kid": "bad", "kty": "INVALID"}]}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=malformed_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        token = make_token()
        # Should still succeed using stale cache
        claims = await v.verify(token)

    assert claims["sub"] == "user_workos_test"
