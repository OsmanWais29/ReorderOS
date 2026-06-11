// IngredientRow — ingredient name + numeric quantity + a unit picker restricted to the
// canonical allowlist (Appendix F.2 / §6). Client-side validation mirrors fail-gate #5
// (reject invalid units, negative/zero quantities, empty names) — this is UX; the DB CHECK
// + API 400 remain the enforcement. Gate 36 (canonical units at the UI layer) lives here.

import React from 'react';
import { View, Text, TextInput, Pressable, StyleSheet, ScrollView } from 'react-native';
import { T } from '../theme/tokens';
import {
  CANONICAL_UNITS_BY_DIMENSION,
  isCanonical,
  type Dimension,
} from '../api/units';

export type IngredientDraft = { name: string; quantity: string; unit: string };

export type IngredientError = 'empty_name' | 'bad_quantity' | 'non_canonical_unit' | null;

/** The fail-gate-#5 / FE-4 rule in ONE place: empty name, qty<=0/NaN, or a non-canonical
 * unit is invalid. Returned (not thrown) so the screen can gate "Confirm" on it (FE-2). */
export function validateIngredient(i: IngredientDraft): IngredientError {
  if (!i.name.trim()) return 'empty_name';
  const qty = Number(i.quantity);
  if (!Number.isFinite(qty) || qty <= 0) return 'bad_quantity';
  if (!isCanonical(i.unit)) return 'non_canonical_unit';
  return null;
}

export type DraftError = 'no_ingredients' | 'duplicate_name' | null;

/** Normalize a name the SAME way the backend's uniqueness index does — lower(btrim(name))
 * (Phase 4). If the client normalized differently, client and server would disagree about
 * what a duplicate is — the normalization-mismatch class flagged in Phase 4. */
export function normalizeName(name: string): string {
  return name.trim().toLowerCase();
}

/** Draft-LEVEL rules for FE-2 Confirm-gating: a draft needs ≥1 ingredient and no duplicate
 * normalized names (the backend confirm 400s on lower(btrim) duplicates). Combine with a
 * per-row validateIngredient pass: Confirm is enabled iff validateDraft===null AND every row
 * validateIngredient===null. */
export function validateDraft(rows: IngredientDraft[]): DraftError {
  if (rows.length === 0) return 'no_ingredients';
  const seen = new Set<string>();
  for (const r of rows) {
    const key = normalizeName(r.name);
    if (key && seen.has(key)) return 'duplicate_name';
    if (key) seen.add(key);
  }
  return null;
}

const ERROR_TEXT: Record<NonNullable<IngredientError>, string> = {
  empty_name: 'Name required',
  bad_quantity: 'Quantity must be greater than 0',
  non_canonical_unit: 'Pick a unit',
};

type Props = {
  value: IngredientDraft;
  onChange: (next: IngredientDraft) => void;
  onRemove?: () => void;
  editable?: boolean;
};

export function IngredientRow({ value, onChange, onRemove, editable = true }: Props) {
  const error = validateIngredient(value);

  return (
    <View style={styles.row}>
      <View style={styles.topLine}>
        <TextInput
          value={value.name}
          onChangeText={(name) => onChange({ ...value, name })}
          placeholder="Ingredient"
          placeholderTextColor={T.ter}
          editable={editable}
          style={[styles.name, error === 'empty_name' && styles.invalid]}
        />
        <TextInput
          value={value.quantity}
          onChangeText={(quantity) => onChange({ ...value, quantity })}
          placeholder="0"
          placeholderTextColor={T.ter}
          editable={editable}
          keyboardType="decimal-pad"
          style={[styles.qty, error === 'bad_quantity' && styles.invalid]}
        />
        {onRemove ? (
          <Pressable onPress={onRemove} hitSlop={8} accessibilityLabel="Remove ingredient">
            <Text style={styles.remove}>×</Text>
          </Pressable>
        ) : null}
      </View>

      {/* Unit picker — ONLY canonical units, grouped by dimension. A non-canonical unit can
          never be selected here, so it can never reach the API (gate 36, UI layer). */}
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        contentContainerStyle={styles.units}
      >
        {(Object.keys(CANONICAL_UNITS_BY_DIMENSION) as Dimension[]).map((dim) => (
          <View key={dim} style={styles.dimGroup}>
            {CANONICAL_UNITS_BY_DIMENSION[dim].map((u) => {
              const selected = value.unit === u;
              return (
                <Pressable
                  key={u}
                  disabled={!editable}
                  onPress={() => onChange({ ...value, unit: u })}
                  style={[styles.chip, selected && styles.chipOn]}
                >
                  <Text style={[styles.chipText, selected && styles.chipTextOn]}>{u}</Text>
                </Pressable>
              );
            })}
          </View>
        ))}
      </ScrollView>

      {error && error !== 'empty_name' && error !== 'bad_quantity' ? (
        <Text style={styles.errText}>{ERROR_TEXT[error]}</Text>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  row: { paddingVertical: 10, gap: 8, borderBottomWidth: 1, borderBottomColor: T.hairline },
  topLine: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  name: {
    flex: 1, color: T.text, fontSize: 16, paddingVertical: 8, paddingHorizontal: 10,
    backgroundColor: T.elev1, borderRadius: 10,
  },
  qty: {
    width: 72, color: T.text, fontSize: 16, paddingVertical: 8, paddingHorizontal: 10,
    backgroundColor: T.elev1, borderRadius: 10, textAlign: 'right',
  },
  invalid: { borderWidth: 1, borderColor: T.red },
  remove: { color: T.sec, fontSize: 22, paddingHorizontal: 4 },
  units: { gap: 12, paddingVertical: 2 },
  dimGroup: { flexDirection: 'row', gap: 6 },
  chip: {
    paddingVertical: 6, paddingHorizontal: 12, borderRadius: 999,
    backgroundColor: T.elev2, borderWidth: 1, borderColor: T.sep,
  },
  chipOn: { backgroundColor: T.acSoft, borderColor: T.acBorder },
  chipText: { color: T.sec, fontSize: 13 },
  chipTextOn: { color: T.ac, fontWeight: '600' },
  errText: { color: T.red, fontSize: 12 },
});
