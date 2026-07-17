// Canonical unit allowlist — PINNED to the backend source of truth:
//   backend/app/modules/inventory/depletion/units.py  (CANONICAL_UNITS)
//   + the DB CHECK constraints (migrations 0014 / 0016).
//
// Drift is NOT a judgment call: the backend test
// `backend/tests/test_frontend_units_sync.py` parses THIS file and FAILS CI if its
// set ever diverges from units.py. The picker must offer exactly what the API
// accepts — otherwise an operator picks a unit the UI offered and the API rejects it
// with a confusing 400 (client validation is UX; the DB CHECK + API 400 is enforcement).
//
// 'oz' is intentionally absent — use 'oz_weight' (mass) or 'fl_oz' (volume).

export type Dimension = 'weight' | 'volume' | 'count';

export const CANONICAL_UNITS_BY_DIMENSION: Record<Dimension, readonly string[]> = {
  weight: ['g', 'kg', 'oz_weight', 'lb'],
  volume: ['ml', 'L', 'fl_oz', 'cup', 'tsp', 'tbsp'],
  count: ['ea', 'dozen'],
};

export const CANONICAL_UNITS: readonly string[] = [
  ...CANONICAL_UNITS_BY_DIMENSION.weight,
  ...CANONICAL_UNITS_BY_DIMENSION.volume,
  ...CANONICAL_UNITS_BY_DIMENSION.count,
];

export function isCanonical(unit: string): boolean {
  return CANONICAL_UNITS.includes(unit);
}

/** Dimension of a canonical unit; null for non-canonical strings (CS, SAC, ...). */
export function dimensionOf(unit: string): Dimension | null {
  for (const d of Object.keys(CANONICAL_UNITS_BY_DIMENSION) as Dimension[]) {
    if (CANONICAL_UNITS_BY_DIMENSION[d].includes(unit)) return d;
  }
  return null;
}
