"""Idempotency primitives.

Sprint 3 (inventory ledger) implements the actual store. Sprint 1 reserves
the dependency name so handlers can be authored against it.
"""

from __future__ import annotations

from fastapi import Header


async def idempotency_key(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str | None:
    """Returns the client-supplied Idempotency-Key header, if any.

    Replay enforcement lives in the inventory module from Sprint 3.
    """
    return idempotency_key
