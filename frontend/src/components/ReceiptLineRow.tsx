// ReceiptLineRow — one extracted/operator line on the shared review screen.
// Displays name, editable qty + cost (blur-save), match state, suggestion chips
// (tap to link — a suggestion is never auto-applied, D-606-26), an item-picker
// entry point, and skip/unskip. All mutations are reported up — the screen owns
// the PUT and the resulting refresh, so the D-606-25 side-effects (affirmation
// cleared) are always reflected.

import React, { useEffect, useState } from 'react';
import { View, Text, TextInput, Pressable, StyleSheet } from 'react-native';
import { Pill } from '@/components/atoms';
import { useLang } from '@/i18n/LangProvider';
import { T, TYPE } from '@/theme/tokens';
import { CANONICAL_UNITS } from '@/api/units';
import type { LineUpdatePayload, ReceiptLine } from '@/api/receipts';

export function ReceiptLineRow({
  line,
  busy,
  onPatch,
  onOpenPicker,
}: {
  line: ReceiptLine;
  busy: boolean;
  onPatch: (patch: LineUpdatePayload) => void;
  onOpenPicker: () => void;
}) {
  const { t } = useLang();
  const [qty, setQty] = useState(line.received_quantity != null ? String(line.received_quantity) : '');
  const [cost, setCost] = useState(
    line.unit_cost_cents != null ? (line.unit_cost_cents / 100).toFixed(2) : '',
  );
  const [unitOpen, setUnitOpen] = useState(false);

  // Server state is the draft owner — resync local fields when the line changes.
  useEffect(() => {
    setQty(line.received_quantity != null ? String(line.received_quantity) : '');
    setCost(line.unit_cost_cents != null ? (line.unit_cost_cents / 100).toFixed(2) : '');
  }, [line.received_quantity, line.unit_cost_cents]);

  const skipped = line.match_status === 'skipped';

  const matchTone =
    line.match_status === 'matched' || line.match_status === 'created'
      ? 'green'
      : skipped
        ? 'neutral'
        : 'amber';
  const matchLabel: Record<ReceiptLine['match_status'], string> = {
    matched: t.rcptLineMatched,
    created: t.rcptLineCreated,
    unmatched: t.rcptLineUnmatched,
    skipped: t.rcptLineSkipped,
  };

  const saveQty = () => {
    const n = Number(qty.replace(',', '.'));
    if (Number.isFinite(n) && n > 0 && n !== line.received_quantity) {
      onPatch({ received_quantity: n });
    }
  };
  const saveCost = () => {
    const trimmed = cost.trim();
    const cents = trimmed === '' ? null : Math.round(Number(trimmed.replace(',', '.')) * 100);
    if (cents === null || (Number.isFinite(cents) && cents >= 0)) {
      if (cents !== line.unit_cost_cents) onPatch({ unit_cost_cents: cents });
    }
  };

  return (
    <View style={[styles.card, skipped && styles.cardSkipped]}>
      <View style={styles.top}>
        <Text style={[styles.name, skipped && styles.nameSkipped]} numberOfLines={2}>
          {line.extracted_name ?? t.rcptLineUnnamed}
        </Text>
        <Pill label={matchLabel[line.match_status]} tone={matchTone} />
      </View>
      {!skipped && line.item_name ? (
        <Text style={styles.linkedItem} numberOfLines={1}>
          → {line.item_name}
        </Text>
      ) : null}

      {!skipped ? (
        <View style={styles.fields}>
          <View style={styles.field}>
            <Text style={styles.fieldLabel}>{t.rcptLineQty}</Text>
            <TextInput
              style={styles.input}
              value={qty}
              onChangeText={setQty}
              onBlur={saveQty}
              keyboardType="decimal-pad"
              editable={!busy}
            />
            {/* the unit is EDITABLE — extraction may guess wrong or return no unit,
                and commit converts purchase→storage from this string. Tap to pick. */}
            <Pressable
              disabled={busy}
              onPress={() => setUnitOpen((o) => !o)}
              hitSlop={6}
              accessibilityLabel={t.rcptLineUnitEdit}
            >
              <Text style={styles.unitEditable}>{line.extracted_unit ?? t.rcptLineUnitNone}</Text>
            </Pressable>
          </View>
          <View style={styles.field}>
            <Text style={styles.fieldLabel}>{t.rcptLineCost}</Text>
            <TextInput
              style={styles.input}
              value={cost}
              onChangeText={setCost}
              onBlur={saveCost}
              keyboardType="decimal-pad"
              placeholder="0.00"
              placeholderTextColor={T.ter}
              editable={!busy}
            />
          </View>
        </View>
      ) : null}

      {!skipped && unitOpen ? (
        <View style={styles.unitChips}>
          {CANONICAL_UNITS.map((u) => (
            <Pressable
              key={u}
              disabled={busy}
              onPress={() => {
                setUnitOpen(false);
                if (u !== line.extracted_unit) onPatch({ extracted_unit: u });
              }}
              style={[styles.unitChip, line.extracted_unit === u && styles.unitChipOn]}
            >
              <Text
                style={[styles.unitChipLabel, line.extracted_unit === u && styles.unitChipLabelOn]}
              >
                {u}
              </Text>
            </Pressable>
          ))}
        </View>
      ) : null}

      {/* Suggestions (unmatched only) + picker + skip actions */}
      {!skipped && line.match_status === 'unmatched' ? (
        <View style={styles.suggestions}>
          {line.suggestions.map((sg) => (
            <Pressable
              key={sg.id}
              disabled={busy}
              onPress={() => onPatch({ inventory_item_id: sg.id })}
              style={styles.suggestionChip}
            >
              <Text style={styles.suggestionLabel}>{sg.name}</Text>
            </Pressable>
          ))}
          <Pressable disabled={busy} onPress={onOpenPicker} style={styles.suggestionChipAlt}>
            <Text style={styles.suggestionLabelAlt}>{t.rcptLinePick}</Text>
          </Pressable>
        </View>
      ) : null}

      <View style={styles.actions}>
        {!skipped && (line.match_status === 'matched' || line.match_status === 'created') ? (
          <Pressable disabled={busy} onPress={() => onPatch({ inventory_item_id: null })}>
            <Text style={styles.actionText}>{t.rcptLineUnlink}</Text>
          </Pressable>
        ) : null}
        <Pressable disabled={busy} onPress={() => onPatch({ skipped: !skipped })}>
          <Text style={styles.actionText}>{skipped ? t.rcptLineUnskip : t.rcptLineSkip}</Text>
        </Pressable>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  card: { backgroundColor: T.elev1, borderRadius: 14, padding: 14, gap: 10 },
  cardSkipped: { opacity: 0.55 },
  top: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: 10 },
  name: { ...TYPE.headline, color: T.text, flex: 1 },
  linkedItem: { ...TYPE.subhead, color: T.ac },
  nameSkipped: { textDecorationLine: 'line-through', color: T.sec },
  fields: { flexDirection: 'row', gap: 12 },
  field: { flexDirection: 'row', alignItems: 'center', gap: 6, flex: 1 },
  fieldLabel: { ...TYPE.footnote, color: T.sec },
  input: {
    ...TYPE.body,
    color: T.text,
    backgroundColor: T.elev2,
    borderRadius: 8,
    paddingHorizontal: 10,
    paddingVertical: 6,
    minWidth: 64,
    textAlign: 'right',
  },
  unitEditable: {
    ...TYPE.footnote,
    color: T.ac,
    backgroundColor: T.acSoft,
    borderRadius: 8,
    paddingHorizontal: 8,
    paddingVertical: 5,
    overflow: 'hidden',
  },
  unitChips: { flexDirection: 'row', flexWrap: 'wrap', gap: 6 },
  unitChip: {
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 12,
    backgroundColor: T.elev2,
  },
  unitChipOn: { backgroundColor: T.acSoft },
  unitChipLabel: { ...TYPE.footnote, color: T.sec },
  unitChipLabelOn: { color: T.ac },
  suggestions: { flexDirection: 'row', flexWrap: 'wrap', gap: 6 },
  suggestionChip: {
    backgroundColor: T.acSoft,
    borderRadius: 12,
    paddingHorizontal: 10,
    paddingVertical: 6,
  },
  suggestionLabel: { ...TYPE.footnote, color: T.ac },
  suggestionChipAlt: {
    backgroundColor: T.elev2,
    borderRadius: 12,
    paddingHorizontal: 10,
    paddingVertical: 6,
  },
  suggestionLabelAlt: { ...TYPE.footnote, color: T.label },
  actions: { flexDirection: 'row', gap: 18 },
  actionText: { ...TYPE.subhead, color: T.ac },
});
