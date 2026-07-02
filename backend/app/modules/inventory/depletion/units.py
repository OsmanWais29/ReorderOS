"""Canonical unit allowlist — the single Python source of truth.

Mirrors the DB CHECK constraints on recipe_ingredients.unit / modifier_ingredients.unit
/ unit_conversions.from_unit/to_unit (migrations 0014, 0016). API and UI validators
import from here so the allowlist is defined in exactly one place in Python.

'oz' is intentionally absent — use 'oz_weight' (mass) or 'fl_oz' (volume).
"""

from __future__ import annotations

_WEIGHT: tuple[str, ...] = ("g", "kg", "oz_weight", "lb")
_VOLUME: tuple[str, ...] = ("ml", "L", "fl_oz", "cup", "tsp", "tbsp")
_COUNT: tuple[str, ...] = ("ea", "dozen")

# unit -> dimension. A conversion is only ever defined within one dimension at
# the global tier; cross-dimension (e.g. cup -> g) requires a per-item density
# override (see unit_conversions item tier).
DIMENSION_OF: dict[str, str] = {
    **{u: "weight" for u in _WEIGHT},
    **{u: "volume" for u in _VOLUME},
    **{u: "count" for u in _COUNT},
}

CANONICAL_UNITS: frozenset[str] = frozenset(DIMENSION_OF)


def is_canonical(unit: str) -> bool:
    """True if ``unit`` is in the canonical allowlist."""
    return unit in CANONICAL_UNITS
