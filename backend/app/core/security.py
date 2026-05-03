"""Security primitives. Sprint 2 fills this in with Clerk JWT + JWKS validation.

Sprint 1 keeps the surface so other modules can import the type without circular
deps. The middleware is a no-op until Sprint 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Role = Literal["owner", "manager", "staff"]


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated request principal."""

    user_id: str
    tenant_id: str
    role: Role
