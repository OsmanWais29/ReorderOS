"""Purchase-unit → storage-unit conversion SUGGESTIONS (Sprint 6, supplier smoke test).

Invoices price in purchase units (CS, SAC, EA, BOX); inventory and depletion run
in canonical storage units (L, ml, kg, g, ea). This module turns extraction's
packaging hints ("4x4L", "ACTUAL WT 10.18 KG") and remembered per-item factors
into a PREFILL for the operator's conversion panel.

A suggestion is never authority: commit refuses non-canonical units until the
operator confirms (services.update_line stamps conversion_confirmed_at). This
module is pure math — no DB, no logging of line content.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.modules.inventory.depletion.units import DIMENSION_OF

# Invoice hint spellings → canonical units. Deliberately conservative: anything
# not listed yields no suggestion (operator fills the panel by hand). 'oz' is
# ambiguous (weight vs fluid) and intentionally absent, like the canonical list.
_HINT_UNIT_NORM: dict[str, str] = {
    "l": "L",
    "lt": "L",
    "litre": "L",
    "liter": "L",
    "ml": "ml",
    "kg": "kg",
    "g": "g",
    "gr": "g",
    "lb": "lb",
    "lbs": "lb",
    "ea": "ea",
    "each": "ea",
    "un": "ea",
    "unit": "ea",
    "unité": "ea",
    "ct": "ea",
    "count": "ea",
    "dz": "dozen",
    "dozen": "dozen",
}

# Same-dimension factors to the dimension's base unit (g / ml / ea) — mirrors
# the 0014-seeded global tier; used only to denominate suggestions.
_TO_BASE: dict[str, Decimal] = {
    "g": Decimal(1),
    "kg": Decimal(1000),
    "oz_weight": Decimal("28.349523125"),
    "lb": Decimal("453.59237"),
    "ml": Decimal(1),
    "L": Decimal(1000),
    "fl_oz": Decimal("29.5735295625"),
    "cup": Decimal("236.5882365"),
    "tsp": Decimal("4.92892159375"),
    "tbsp": Decimal("14.78676478125"),
    "ea": Decimal(1),
    "dozen": Decimal(12),
}

_QUANT = Decimal("0.0001")


@dataclass(frozen=True)
class ConversionSuggestion:
    quantity: Decimal  # total suggested receive quantity, in storage units
    factor: Decimal  # storage units per 1 purchase unit
    source: str  # 'remembered' | 'extracted_suggestion' | 'identity'


def normalize_hint_unit(raw: str | None) -> str | None:
    """Invoice unit spelling → canonical unit, or None if unrecognized."""
    if not raw:
        return None
    key = raw.strip().lower()
    if raw.strip() in DIMENSION_OF:  # already canonical, case-exact (e.g. 'L')
        return raw.strip()
    return _HINT_UNIT_NORM.get(key)


def _to_storage(qty: Decimal, unit: str, storage_unit: str) -> Decimal | None:
    """Same-dimension canonical conversion, or None when dimensions differ."""
    if DIMENSION_OF.get(unit) != DIMENSION_OF.get(storage_unit):
        return None
    return qty * _TO_BASE[unit] / _TO_BASE[storage_unit]


def suggest_conversion(
    *,
    purchase_qty: Decimal,
    purchase_unit: str | None,
    storage_unit: str,
    pack_count: Decimal | None = None,
    pack_size_qty: Decimal | None = None,
    pack_size_unit: str | None = None,
    actual_weight_qty: Decimal | None = None,
    actual_weight_unit: str | None = None,
    remembered_factor: Decimal | None = None,
) -> ConversionSuggestion | None:
    """Best prefill for 'receive purchase_qty x purchase_unit as N x storage_unit'.

    Precedence: remembered (operator confirmed it before) → actual/catch weight
    (the printed truth for weight-priced goods) → packaging math (4x4L) →
    identity when the purchase unit itself is canonical/convertible. None when
    nothing trustworthy exists — the panel opens empty, never guesses.
    """
    if storage_unit not in DIMENSION_OF or purchase_qty <= 0:
        return None

    if remembered_factor is not None and remembered_factor > 0:
        qty = (purchase_qty * remembered_factor).quantize(_QUANT)
        return ConversionSuggestion(qty, remembered_factor.quantize(_QUANT), "remembered")

    aw_unit = normalize_hint_unit(actual_weight_unit)
    if actual_weight_qty and actual_weight_qty > 0 and aw_unit:
        total = _to_storage(actual_weight_qty, aw_unit, storage_unit)
        if total is not None:
            factor = (total / purchase_qty).quantize(_QUANT)
            return ConversionSuggestion(total.quantize(_QUANT), factor, "extracted_suggestion")

    ps_unit = normalize_hint_unit(pack_size_unit)
    if pack_size_qty and pack_size_qty > 0 and ps_unit:
        per_purchase = pack_size_qty * (pack_count if pack_count and pack_count > 0 else 1)
        content = _to_storage(per_purchase, ps_unit, storage_unit)
        if content is not None:
            qty = (purchase_qty * content).quantize(_QUANT)
            return ConversionSuggestion(qty, content.quantize(_QUANT), "extracted_suggestion")

    pu = normalize_hint_unit(purchase_unit)
    if pu:
        unit_factor = _to_storage(Decimal(1), pu, storage_unit)
        if unit_factor is not None:
            qty = (purchase_qty * unit_factor).quantize(_QUANT)
            src = "identity" if pu == storage_unit else "extracted_suggestion"
            return ConversionSuggestion(qty, unit_factor.quantize(_QUANT), src)

    return None


def hint_dimension(
    pack_size_unit: str | None,
    actual_weight_unit: str | None,
    purchase_unit: str | None,
) -> str | None:
    """The dimension (weight/volume/count) the INVOICE evidence points at —
    used to flag a linked item whose storage unit lives in a different
    dimension (live smoke: a 1000CT goblet case linked to an oz_weight item)."""
    for raw in (pack_size_unit, actual_weight_unit, purchase_unit):
        norm = normalize_hint_unit(raw)
        if norm is not None:
            return DIMENSION_OF[norm]
    return None


def round_unit_cost_cents(line_total_cents: int, storage_qty: Decimal) -> int | None:
    """Per-storage-unit cost from the printed line total (8244 / 48 L → 172)."""
    if storage_qty <= 0:
        return None
    return int(
        (Decimal(line_total_cents) / storage_qty).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
