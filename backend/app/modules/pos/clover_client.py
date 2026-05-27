"""Clover REST API client with token-bucket rate limiting.

Rate limits (Clover docs, 2024):
  - Application-level: 50 requests/second across all tokens
  - Per-token:         16 requests/second

Both buckets must have capacity before a request is sent.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.logging import get_logger

log = get_logger(__name__)

_ENV_API_BASES: dict[str, str] = {
    "sandbox": "https://apisandbox.dev.clover.com",
    "production": "https://api.clover.com",
    "eu": "https://api.eu.clover.com",
    "latam": "https://api.la.clover.com",
}


class OrderNotFoundError(Exception):
    """Clover returned 404 for the requested order ID."""


class TokenExpiredError(Exception):
    """Clover returned 401 — access token is expired or revoked."""


class RateLimitedError(Exception):
    """Clover returned 429 — application is rate limited."""


@dataclass
class TokenBucket:
    """Leaky-bucket rate limiter; async-safe within a single event loop."""

    rate: float
    capacity: float
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)

    def __post_init__(self) -> None:
        self._tokens = self.capacity
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last_refill = now

    async def acquire(self) -> None:
        while True:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            wait = (1.0 - self._tokens) / self.rate
            await asyncio.sleep(wait)


# Module-level buckets shared across all CloverClient instances in one process.
_app_bucket: TokenBucket = TokenBucket(rate=50.0, capacity=50.0)
_per_token_buckets: dict[str, TokenBucket] = {}


def _get_per_token_bucket(access_token: str) -> TokenBucket:
    key = access_token[:16]
    if key not in _per_token_buckets:
        _per_token_buckets[key] = TokenBucket(rate=16.0, capacity=16.0)
    return _per_token_buckets[key]


class CloverClient:
    """Thin async wrapper around the Clover v3 REST API."""

    def __init__(
        self,
        access_token: str,
        merchant_id: str,
        environment: str = "sandbox",
    ) -> None:
        self.access_token = access_token
        self.merchant_id = merchant_id
        self.base_url = _ENV_API_BASES.get(environment, _ENV_API_BASES["sandbox"])
        self._token_bucket = _get_per_token_bucket(access_token)

    async def list_orders(
        self,
        since_ms: int,
        offset: int = 0,
        limit: int = 100,
        max_retries: int = 3,
    ) -> list[dict[str, Any]]:
        """Fetch a page of orders modified at or after since_ms.

        Retries automatically on 429 using the Retry-After header.
        Returns the ``elements`` list; empty list = end-of-results.

        Raises:
            TokenExpiredError: 401 from Clover.
            RateLimitedError: still 429 after max_retries attempts.
            httpx.HTTPStatusError: any other 4xx / 5xx.
        """
        url = (
            f"{self.base_url}/v3/merchants/{self.merchant_id}/orders"
            f"?filter=modifiedTime>={since_ms}"
            f"&orderBy=modifiedTime+ASC"
            f"&expand=lineItems,payments"
            f"&limit={limit}"
            f"&offset={offset}"
        )

        for attempt in range(max_retries):
            await _app_bucket.acquire()
            await self._token_bucket.acquire()

            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.get(
                    url,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                )

            if resp.status_code == 401:
                raise TokenExpiredError(f"401 listing orders for merchant {self.merchant_id}")
            if resp.status_code == 429:
                if attempt < max_retries - 1:
                    retry_after = max(0, int(resp.headers.get("Retry-After", "1")))
                    await asyncio.sleep(retry_after)
                    continue
                raise RateLimitedError(f"429 listing orders for merchant {self.merchant_id}")

            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data.get("elements") or []

        raise RateLimitedError(  # pragma: no cover
            f"429 listing orders for merchant {self.merchant_id}"
        )

    async def get_merchant(self) -> dict[str, Any]:
        """Fetch merchant details (name, address, phone, etc.).

        Raises:
            TokenExpiredError: 401 from Clover.
            RateLimitedError: 429 from Clover.
            httpx.HTTPStatusError: any other 4xx / 5xx.
        """
        await _app_bucket.acquire()
        await self._token_bucket.acquire()

        url = f"{self.base_url}/v3/merchants/{self.merchant_id}"

        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(
                url,
                headers={"Authorization": f"Bearer {self.access_token}"},
                params={"expand": "address"},
            )

        if resp.status_code == 401:
            raise TokenExpiredError(f"401 fetching merchant {self.merchant_id}")
        if resp.status_code == 429:
            raise RateLimitedError(f"429 fetching merchant {self.merchant_id}")

        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """Fetch a single order with its line items expanded.

        Raises:
            OrderNotFoundError: 404 from Clover.
            TokenExpiredError: 401 from Clover.
            RateLimitedError: 429 from Clover.
            httpx.HTTPStatusError: any other 4xx / 5xx.
        """
        await _app_bucket.acquire()
        await self._token_bucket.acquire()

        url = f"{self.base_url}/v3/merchants/{self.merchant_id}/orders/{order_id}"

        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(
                url,
                headers={"Authorization": f"Bearer {self.access_token}"},
                params={"expand": "lineItems,payments"},
            )

        if resp.status_code == 401:
            raise TokenExpiredError(f"401 fetching order {order_id}")
        if resp.status_code == 404:
            raise OrderNotFoundError(f"Order {order_id} not found on merchant {self.merchant_id}")
        if resp.status_code == 429:
            raise RateLimitedError(f"429 fetching order {order_id}")

        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
