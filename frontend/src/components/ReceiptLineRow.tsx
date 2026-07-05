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
            <Text style={styles.unit}>{line.extracted_unit ?? '—'}</Text>
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
  unit: { ...TYPE.footnote, color: T.sec },
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
