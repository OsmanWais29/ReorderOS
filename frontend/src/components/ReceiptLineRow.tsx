// ReceiptLineRow — one extracted/operator line on the shared review screen.
// Displays name, editable qty + cost (blur-save), match state, suggestion chips
// (tap to link — a suggestion is never auto-applied, D-606-26), an item-picker
// entry point, and skip/unskip. All mutations are reported up — the screen owns
// the PUT and the resulting refresh, so the D-606-25 side-effects (affirmation
// cleared) are always reflected.

import React, { useEffect, useState } from 'react';
import { View, Text, TextInput, Pressable, StyleSheet } from 'react-native';
import { Button, Pill } from '@/components/atoms';
import { useLang } from '@/i18n/LangProvider';
import { T, TYPE } from '@/theme/tokens';
import { CANONICAL_UNITS } from '@/api/units';
import { lineNeedsConversion } from '@/api/receipts';
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
  // Conversion panel state — prefilled from the server suggestion, editable,
  // qty and factor stay mutually consistent (qty = purchase_qty x factor).
  const [convQty, setConvQty] = useState('');
  const [convFactor, setConvFactor] = useState('');

  // Server state is the draft owner — resync local fields when the line changes.
  useEffect(() => {
    setQty(line.received_quantity != null ? String(line.received_quantity) : '');
    setCost(line.unit_cost_cents != null ? (line.unit_cost_cents / 100).toFixed(2) : '');
  }, [line.received_quantity, line.unit_cost_cents]);
  useEffect(() => {
    setConvQty(line.suggested_quantity != null ? String(line.suggested_quantity) : '');
    setConvFactor(line.suggested_factor != null ? String(line.suggested_factor) : '');
  }, [line.id, line.suggested_quantity, line.suggested_factor, line.conversion_confirmed_at]);

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

      {/* Confirmed conversion summary: 3 CS → 48 L (1 CS = 16 L) */}
      {!skipped && line.conversion_confirmed_at && line.purchase_unit ? (
        <Text style={styles.convDone}>
          ✓ {line.purchase_quantity} {line.purchase_unit} → {line.received_quantity}{' '}
          {line.received_unit} (1 {line.purchase_unit} = {line.conversion_factor}{' '}
          {line.received_unit})
        </Text>
      ) : null}

      {/* Conversion panel: invoice U/M (CS/SAC) → storage unit, operator-confirmed */}
      {!skipped && lineNeedsConversion(line) ? (
        <ConversionPanel
          line={line}
          busy={busy}
          convQty={convQty}
          convFactor={convFactor}
          onQty={(v) => {
            setConvQty(v);
            const q = Number(v.replace(',', '.'));
            const pq = line.received_quantity;
            if (Number.isFinite(q) && q > 0 && pq && pq > 0) {
              setConvFactor(String(Number((q / pq).toFixed(4))));
            }
          }}
          onFactor={(v) => {
            setConvFactor(v);
            const f = Number(v.replace(',', '.'));
            const pq = line.received_quantity;
            if (Number.isFinite(f) && f > 0 && pq && pq > 0) {
              setConvQty(String(Number((pq * f).toFixed(4))));
            }
          }}
          onConfirm={() => {
            const q = Number(convQty.replace(',', '.'));
            const f = Number(convFactor.replace(',', '.'));
            if (!Number.isFinite(q) || q <= 0 || !Number.isFinite(f) || f <= 0) return;
            const pq = line.received_quantity;
            const totalCents =
              line.unit_cost_cents != null && pq ? line.unit_cost_cents * pq : null;
            onPatch({
              received_quantity: q,
              received_unit: line.item_storage_unit as string,
              conversion_factor: f,
              ...(totalCents != null ? { unit_cost_cents: Math.round(totalCents / q) } : {}),
              remember_conversion: true,
            });
          }}
        />
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

function ConversionPanel({
  line,
  busy,
  convQty,
  convFactor,
  onQty,
  onFactor,
  onConfirm,
}: {
  line: ReceiptLine;
  busy: boolean;
  convQty: string;
  convFactor: string;
  onQty: (v: string) => void;
  onFactor: (v: string) => void;
  onConfirm: () => void;
}) {
  const { t } = useLang();
  // Accept/Edit: with a server suggestion the operator just approves — no
  // mental arithmetic. Editing (or no suggestion) exposes the inputs.
  const hasSuggestion = line.suggested_quantity != null && line.suggested_factor != null;
  const [editing, setEditing] = useState(!hasSuggestion);
  useEffect(() => setEditing(!hasSuggestion), [line.id, hasSuggestion]);

  const su = line.item_storage_unit ?? '';
  const pq = line.received_quantity; // pre-confirm, this IS the invoice qty
  const q = Number(convQty.replace(',', '.'));
  // Printed line total is the costing truth (weight-priced lines make
  // unit_cost x purchase_qty wrong); fall back to the product only without it.
  const totalCents =
    line.line_total_cents ??
    (line.unit_cost_cents != null && pq ? line.unit_cost_cents * pq : null);
  const perUnit =
    totalCents != null && Number.isFinite(q) && q > 0 ? totalCents / q / 100 : null;
  const valid =
    Number.isFinite(q) && q > 0 && Number.isFinite(Number(convFactor.replace(',', '.')));

  // Package clue exactly as the invoice printed it.
  const clue = line.actual_weight_qty
    ? `${t.rcptConvClue} ${line.actual_weight_qty} ${line.actual_weight_unit ?? ''} (${t.rcptConvActual})`
    : line.pack_size_qty
      ? `${t.rcptConvClue} ${line.pack_count ? `${line.pack_count} x ` : ''}${line.pack_size_qty} ${line.pack_size_unit ?? ''}`
      : null;

  // Calculation explanation for the suggestion, in the invoice's own terms.
  const explain = !hasSuggestion
    ? null
    : line.suggestion_source === 'remembered'
      ? `1 ${line.extracted_unit} = ${line.suggested_factor} ${su} (${t.rcptConvRemembered})`
      : line.actual_weight_qty
        ? `${t.rcptConvActual} ${line.actual_weight_qty} ${line.actual_weight_unit ?? ''} = ${line.suggested_quantity} ${su}`
        : line.pack_size_qty
          ? `${pq} ${line.extracted_unit} x ${line.pack_count ? `${line.pack_count} x ` : ''}${line.pack_size_qty} ${line.pack_size_unit ?? ''} = ${line.suggested_quantity} ${su}`
          : `${pq} ${line.extracted_unit} = ${line.suggested_quantity} ${su}`;

  const costPreview =
    perUnit != null && totalCents != null
      ? `${(totalCents / 100).toFixed(2)} $ / ${convQty} ${su} = ${perUnit.toFixed(4)} $ / ${su}`
      : null;

  return (
    <View style={styles.convBox}>
      <Text style={styles.convTitle}>
        {t.rcptConvInvoiceSays} {pq} {line.extracted_unit}
      </Text>
      {clue ? <Text style={styles.convCost}>{clue}</Text> : null}
      {line.unit_mismatch_warning ? (
        <Text style={styles.convWarn}>
          {t.rcptConvDimWarn.replace('{unit}', su)}
        </Text>
      ) : null}

      {!editing && hasSuggestion ? (
        <>
          <Text style={styles.convSuggest}>
            {t.rcptConvReceiveAs} {line.suggested_quantity} {su}
          </Text>
          {explain ? <Text style={styles.convCost}>{explain}</Text> : null}
          {costPreview ? <Text style={styles.convCost}>{costPreview}</Text> : null}
          <View style={styles.convRow}>
            <Button
              label={t.rcptConvAccept}
              size="md"
              disabled={busy || !valid}
              onPress={onConfirm}
            />
            <Pressable disabled={busy} onPress={() => setEditing(true)}>
              <Text style={styles.actionText}>{t.rcptConvEdit}</Text>
            </Pressable>
          </View>
        </>
      ) : (
        <>
          <View style={styles.convRow}>
            <Text style={styles.convLabel}>{t.rcptConvReceiveAs}</Text>
            <TextInput
              style={styles.convInput}
              value={convQty}
              onChangeText={onQty}
              keyboardType="decimal-pad"
              editable={!busy}
            />
            <Text style={styles.convUnit}>{su}</Text>
          </View>
          <View style={styles.convRow}>
            <Text style={styles.convLabel}>1 {line.extracted_unit} =</Text>
            <TextInput
              style={styles.convInput}
              value={convFactor}
              onChangeText={onFactor}
              keyboardType="decimal-pad"
              editable={!busy}
            />
            <Text style={styles.convUnit}>{su}</Text>
          </View>
          {costPreview ? <Text style={styles.convCost}>{costPreview}</Text> : null}
          <Button
            label={t.rcptConvConfirm}
            size="md"
            disabled={busy || !valid}
            onPress={onConfirm}
          />
        </>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  card: { backgroundColor: T.elev1, borderRadius: 14, padding: 14, gap: 10 },
  cardSkipped: { opacity: 0.55 },
  top: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: 10 },
  name: { ...TYPE.headline, color: T.text, flex: 1 },
  linkedItem: { ...TYPE.subhead, color: T.ac },
  convDone: { ...TYPE.footnote, color: T.ac },
  convBox: { backgroundColor: T.elev2, borderRadius: 12, padding: 12, gap: 8 },
  convTitle: { ...TYPE.subhead, color: T.text },
  convRow: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  convLabel: { ...TYPE.footnote, color: T.sec, minWidth: 82 },
  convInput: {
    ...TYPE.body,
    color: T.text,
    backgroundColor: T.elev1,
    borderRadius: 8,
    paddingHorizontal: 10,
    paddingVertical: 6,
    minWidth: 84,
  },
  convUnit: { ...TYPE.subhead, color: T.sec },
  convCost: { ...TYPE.footnote, color: T.ter },
  convSuggest: { ...TYPE.headline, color: T.text },
  convWarn: { ...TYPE.footnote, color: T.amber },
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
