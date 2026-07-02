"""Unit conversion with three-tier (item -> tenant -> global) precedence.

``convert()`` resolves the most specific conversion available for a (from_unit,
to_unit) pair given an optional inventory item + tenant scope:

    item-specific   (inventory_item_id + tenant_id)   most specific
    tenant-specific (tenant_id, no item)
    global          (neither)                          least specific

The lookup uses an explicit three-branch WHERE that enumerates exactly those
three valid tiers and excludes the incoherent "item without tenant" combination.
The ORDER BY then ranks the surviving rows item > tenant > global. The WHERE and
ORDER BY are correct ONLY together: the WHERE guarantees every candidate row is a
valid tier, so the two-flag ranking orders them correctly. (The schema also
forbids the incoherent row via a CHECK in migration 0014, but the query does not
rely on that — defence in depth.)

Tenant isolation comes from the WHERE clause (tenant_id = :tid OR NULL), not only
from RLS, so an item/tenant override for one tenant can never be selected for
another even when the caller's connection bypasses RLS.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.inventory.depletion.units import is_canonical


class ConversionError(Exception):
    """A valid request for which no conversion path exists.

    Distinct from a caller-contract bug (which raises ValueError): the depletion
    walker maps ConversionError to depletion_reason='missing_conversion'.
    """


_LOOKUP = text(
    """
    SELECT factor
      FROM unit_conversions
     WHERE from_unit = :f AND to_unit = :t
       AND (
            (inventory_item_id = :iid AND tenant_id = :tid)
         OR (inventory_item_id IS NULL AND tenant_id = :tid)
         OR (inventory_item_id IS NULL AND tenant_id IS NULL)
       )
     ORDER BY (inventory_item_id IS NOT NULL) DESC,
              (tenant_id IS NOT NULL) DESC
     LIMIT 1
    """
)


async def convert(
    session: AsyncSession,
    qty: Decimal,
    from_unit: str,
    to_unit: str,
    *,
    inventory_item_id: UUID | None = None,
    tenant_id: UUID | None = None,
) -> Decimal:
    """Convert ``qty`` from ``from_unit`` to ``to_unit``, returning a Decimal.

    Resolution precedence: item-specific -> tenant-specific -> global.

    Raises:
        ValueError: caller-contract violation — ``inventory_item_id`` was given
            without ``tenant_id`` (an item conversion is inherently tenant-scoped).
            This is a programming bug and must surface loudly, NOT be absorbed into
            a missing_conversion data status.
        ConversionError: an unknown/non-canonical unit, or no conversion path for
            the (units, scope) requested.
    """
    # Caller contract: item scope requires a tenant. Loud ValueError, never
    # ConversionError — a malformed request is a bug, not a data condition.
    if inventory_item_id is not None and tenant_id is None:
        raise ValueError(
            "convert(): inventory_item_id requires tenant_id "
            "(item-specific conversions are tenant-scoped)"
        )

    if not is_canonical(from_unit):
        raise ConversionError(f"unknown unit: {from_unit!r}")
    if not is_canonical(to_unit):
        raise ConversionError(f"unknown unit: {to_unit!r}")

    # Identity never depends on a seeded row: 1 X = 1 X regardless of table state.
    # (Avoids a spurious missing_conversion if a 'X -> X = 1' row is absent.)
    if from_unit == to_unit:
        return qty

    row = (
        await session.execute(
            _LOOKUP,
            {"f": from_unit, "t": to_unit, "iid": inventory_item_id, "tid": tenant_id},
        )
    ).first()
    if row is None:
        raise ConversionError(
            f"no conversion path: {from_unit} -> {to_unit} "
            f"(inventory_item_id={inventory_item_id}, tenant_id={tenant_id})"
        )
    return qty * Decimal(str(row[0]))
