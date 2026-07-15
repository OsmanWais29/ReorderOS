// Shared receipt review screen (spec §7 FE Phase B) — ONE UI for every source
// (photo, manual, and later email/Gmail/webhook drafts land here unchanged).
//
// Flow: poll while extraction runs (2s, max 30s) → review/match lines (edits
// blur-save through PUT — every mutation clears the affirmation server-side,
// D-606-25) → affirm → commit (manager; server-authoritative gate, D-606-22).
// Re-scan (reset-extraction) is explicitly confirmed — it discards all edits.

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { View, Text, ScrollView, Pressable, Switch, TextInput, StyleSheet } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button, Field } from '@/components/atoms';
import { Icon } from '@/components/Icon';
import { ReceiptSourceBadge, ExtractionStatusBanner, ReceiptPhotoPreview } from '@/components/ReceiptBits';
import { ReceiptLineRow } from '@/components/ReceiptLineRow';
import { InventoryItemPicker, type ItemChoice } from '@/components/InventoryItemPicker';
import { confirmDestructive, showSuccess } from '@/ui/dialogs';
import { useAuth } from '@/auth/AuthContext';
import { saveReturnTo } from '@/auth/returnTo';
import { useLang } from '@/i18n/LangProvider';
import { T, TYPE } from '@/theme/tokens';
import { CANONICAL_UNITS } from '@/api/units';
import {
  getReceipt,
  updateLine,
  addLine,
  commitReceipt,
  cancelReceipt,
  dismissReceipt,
  resetExtraction,
  lineNeedsConversion,
  ReceiptApiError,
  type LineUpdatePayload,
  type ReceiptDetail,
} from '@/api/receipts';

const POLL_MS = 2000;
const POLL_MAX_MS = 30_000;

export default function ReceiptReview() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const { token } = useAuth();
  const { t } = useLang();
  const router = useRouter();

  const [receipt, setReceipt] = useState<ReceiptDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyLineId, setBusyLineId] = useState<string | null>(null);
  const [affirmed, setAffirmed] = useState(false);
  const [committing, setCommitting] = useState(false);
  const [commitError, setCommitError] = useState<string | null>(null);
  const [sessionDead, setSessionDead] = useState(false);
  const [pickerForLine, setPickerForLine] = useState<{ lineId: string; query: string } | null>(null);
  const [dismissReason, setDismissReason] = useState<string | null>(null); // null = hidden
  const [addOpen, setAddOpen] = useState(false);
  const [addName, setAddName] = useState('');
  const [addQty, setAddQty] = useState('');
  const [addUnit, setAddUnit] = useState<string | null>(null);
  const pollStarted = useRef<number | null>(null);
  const [pollTick, setPollTick] = useState(0);

  const refresh = useCallback(async () => {
    if (!token || !id) return;
    try {
      const r = await getReceipt(token, id);
      setReceipt(r);
      setError(null);
      setSessionDead(false);
      // The server clears the affirmation on every edit (D-606-25) — mirror it
      // locally so the operator re-affirms against what they now see.
      if (!r.reviewed_affirmation) setAffirmed((a) => (r.review_started_at ? a : false));
    } catch (e: unknown) {
      if (e instanceof ReceiptApiError && e.status === 401) {
        setSessionDead(true);
        setError(t.sessionExpired);
      } else {
        setError(e instanceof ReceiptApiError ? e.detail : t.rcptLoadError);
      }
    }
  }, [token, id, t]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Extraction polling: 2s cadence, stop after 30s (spec §7 receive flow).
  const extracting =
    receipt?.extraction_status === 'pending' || receipt?.extraction_status === 'processing';
  const withinWindow =
    pollStarted.current === null || Date.now() - pollStarted.current < POLL_MAX_MS;
  useEffect(() => {
    if (!extracting) {
      pollStarted.current = null;
      return;
    }
    if (pollStarted.current === null) pollStarted.current = Date.now();
    if (!withinWindow) return;
    const timer = setTimeout(() => {
      void refresh().then(() => setPollTick((n) => n + 1));
    }, POLL_MS);
    return () => clearTimeout(timer);
  }, [extracting, withinWindow, refresh, pollTick]);

  const patchLine = useCallback(
    async (lineId: string, patch: LineUpdatePayload) => {
      if (!token || !id) return;
      setBusyLineId(lineId);
      setCommitError(null);
      try {
        await updateLine(token, id, lineId, patch);
        setAffirmed(false); // the server cleared reviewed_affirmation — re-affirm after edits
        await refresh();
      } catch (e: unknown) {
        if (e instanceof ReceiptApiError && e.status === 401) setSessionDead(true);
        setCommitError(e instanceof ReceiptApiError ? (e.status === 401 ? t.sessionExpired : e.detail) : t.rcptSaveError);
      } finally {
        setBusyLineId(null);
      }
    },
    [token, id, refresh, t],
  );

  const onPick = useCallback(
    (choice: ItemChoice) => {
      const target = pickerForLine;
      setPickerForLine(null);
      if (!target) return;
      void patchLine(
        target.lineId,
        choice.kind === 'existing'
          ? { inventory_item_id: choice.id }
          : { new_item_name: choice.name, new_item_unit: choice.unit },
      );
    },
    [pickerForLine, patchLine],
  );

  const submitAddLine = useCallback(async () => {
    if (!token || !id || !addUnit) return;
    const n = Number(addQty.replace(',', '.'));
    if (!addName.trim() || !Number.isFinite(n) || n <= 0) return;
    setCommitError(null);
    try {
      await addLine(token, id, {
        extracted_name: addName.trim(),
        received_quantity: n,
        extracted_unit: addUnit,
      });
      setAddName('');
      setAddQty('');
      setAddUnit(null);
      setAddOpen(false);
      setAffirmed(false);
      await refresh();
    } catch (e: unknown) {
      if (e instanceof ReceiptApiError && e.status === 401) setSessionDead(true);
      setCommitError(e instanceof ReceiptApiError ? (e.status === 401 ? t.sessionExpired : e.detail) : t.rcptSaveError);
    }
  }, [token, id, addName, addQty, addUnit, refresh, t]);

  const doCommit = useCallback(async () => {
    if (!token || !id) return;
    setCommitting(true);
    setCommitError(null);
    try {
      await commitReceipt(token, id, affirmed);
      showSuccess(t.rcptCommitDoneTitle, t.rcptCommitDoneBody, () => router.back());
    } catch (e: unknown) {
      if (e instanceof ReceiptApiError) {
        if (e.status === 401) {
          setSessionDead(true);
          setCommitError(t.sessionExpired);
        }
        else if (e.code === 'RECEIPT_REVIEW_REQUIRED') setCommitError(t.rcptNeedReview);
        else if (e.code === 'RECEIPT_LINES_UNMATCHED') setCommitError(t.rcptLinesUnmatched);
        else if (e.code === 'RECEIPT_CONVERSION_REQUIRED') setCommitError(t.rcptConvNeeded);
        else if (e.code === 'RECEIPT_CONVERSION_INCONSISTENT')
          setCommitError(t.rcptConvInconsistent);
        else if (e.code === 'RECEIPT_NOTHING_TO_COMMIT') setCommitError(t.rcptNothingToCommit);
        else if (e.status === 403) setCommitError(t.rcptManagerOnly);
        else setCommitError(e.detail);
      } else {
        setCommitError(t.rcptSaveError);
      }
    } finally {
      setCommitting(false);
    }
  }, [token, id, affirmed, router, t]);

  const doRescan = useCallback(() => {
    confirmDestructive({
      title: t.rcptRescanTitle,
      message: t.rcptRescanBody,
      confirmLabel: t.rcptRescanConfirm,
      cancelLabel: t.rcptRescanCancel,
      onConfirm: () => {
        if (!token || !id) return;
        void resetExtraction(token, id, true)
          .then(() => {
            pollStarted.current = null;
            setAffirmed(false);
            return refresh();
          })
          .catch((e: unknown) => {
            if (e instanceof ReceiptApiError && e.status === 401) setSessionDead(true);
            setCommitError(e instanceof ReceiptApiError ? (e.status === 401 ? t.sessionExpired : e.detail) : t.rcptSaveError);
          });
      },
    });
  }, [token, id, refresh, t]);

  const doDismiss = useCallback(async () => {
    if (!token || !id || !dismissReason?.trim()) return;
    try {
      await dismissReceipt(token, id, dismissReason.trim());
      router.back();
    } catch (e: unknown) {
      if (e instanceof ReceiptApiError && e.status === 401) setSessionDead(true);
      setCommitError(e instanceof ReceiptApiError ? (e.status === 401 ? t.sessionExpired : e.detail) : t.rcptSaveError);
    }
  }, [token, id, dismissReason, router, t.rcptSaveError]);

  const doCancel = useCallback(() => {
    confirmDestructive({
      title: t.rcptCancelTitle,
      confirmLabel: t.rcptCancelConfirm,
      cancelLabel: t.rcptRescanCancel,
      onConfirm: () => {
        if (!token || !id) return;
        void cancelReceipt(token, id)
          .then(() => router.back())
          .catch((e: unknown) => {
            if (e instanceof ReceiptApiError && e.status === 401) setSessionDead(true);
            setCommitError(e instanceof ReceiptApiError ? (e.status === 401 ? t.sessionExpired : e.detail) : t.rcptSaveError);
          });
      },
    });
  }, [token, id, router, t]);

  const editable = receipt?.commit_state === 'draft' || receipt?.commit_state === 'pending_review';
  const committed = receipt?.commit_state === 'committed';

  return (
    <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
      <View style={styles.header}>
        <Pressable onPress={() => router.back()} hitSlop={12} style={styles.back}>
          <Icon name="chevron-left" size={22} color={T.text} />
        </Pressable>
        <Text style={styles.h1}>{t.rcptTitle}</Text>
        {receipt ? <ReceiptSourceBadge source={receipt.source} /> : <View style={styles.back} />}
      </View>

      {error ? (
        <View style={styles.center}>
          <Text style={styles.err}>{error}</Text>
          <Button label={t.stockRetry} variant="secondary" onPress={() => void refresh()} />
        </View>
      ) : receipt === null ? null : (
        <ScrollView contentContainerStyle={styles.body} keyboardShouldPersistTaps="handled">
          <ReceiptPhotoPreview url={receipt.photo_url} mimeType={receipt.mime_type} />
          <ExtractionStatusBanner
            status={receipt.extraction_status}
            manualEntryRequired={receipt.manual_entry_required}
            quotaBlocked={receipt.quota_blocked}
            polling={extracting && withinWindow}
          />

          {/* header meta */}
          <View style={styles.meta}>
            {receipt.supplier_name ? (
              <Text style={styles.metaMain}>{receipt.supplier_name}</Text>
            ) : null}
            <Text style={styles.metaSub}>
              {[
                receipt.invoice_number ? `#${receipt.invoice_number}` : null,
                receipt.invoice_date,
                receipt.total_cents != null ? `$${(receipt.total_cents / 100).toFixed(2)}` : null,
              ]
                .filter(Boolean)
                .join('  ·  ') || t.rcptNoMeta}
            </Text>
            {receipt.sender_email ? (
              <Text style={styles.metaSub}>{receipt.sender_email}</Text>
            ) : null}
          </View>

          {/* lines */}
          <Text style={styles.section}>{t.rcptLines}</Text>
          {receipt.lines.length === 0 && !extracting ? (
            <Text style={styles.emptyLines}>{t.rcptNoLines}</Text>
          ) : null}
          <View style={styles.lines}>
            {receipt.lines.map((line) => (
              <ReceiptLineRow
                key={line.id}
                line={line}
                busy={busyLineId === line.id || !editable}
                onPatch={(patch) => void patchLine(line.id, patch)}
                onOpenPicker={() =>
                  setPickerForLine({ lineId: line.id, query: line.extracted_name ?? '' })
                }
              />
            ))}
          </View>

          {/* add line */}
          {editable ? (
            addOpen ? (
              <View style={styles.addBox}>
                <Field label={t.rcptAddName} value={addName} onChangeText={setAddName} />
                <Field
                  label={t.rcptAddQty}
                  value={addQty}
                  onChangeText={setAddQty}
                  keyboardType="decimal-pad"
                />
                <View style={styles.units}>
                  {CANONICAL_UNITS.map((u) => (
                    <Pressable
                      key={u}
                      onPress={() => setAddUnit(u)}
                      style={[styles.unitChip, addUnit === u && styles.unitChipOn]}
                    >
                      <Text style={[styles.unitLabel, addUnit === u && styles.unitLabelOn]}>
                        {u}
                      </Text>
                    </Pressable>
                  ))}
                </View>
                <Button label={t.rcptAddSave} size="md" onPress={() => void submitAddLine()} />
              </View>
            ) : (
              <Pressable onPress={() => setAddOpen(true)}>
                <Text style={styles.addLink}>{t.rcptAddLine}</Text>
              </Pressable>
            )
          ) : null}

          {/* commit block */}
          {editable ? (
            <View style={styles.commitBox}>
              <View style={styles.affirmRow}>
                <Text style={styles.affirmText}>{t.rcptAffirm}</Text>
                <Switch
                  value={affirmed}
                  onValueChange={setAffirmed}
                  trackColor={{ true: T.acDeep, false: T.elev2 }}
                />
              </View>
              {commitError ? <Text style={styles.err}>{commitError}</Text> : null}
              {sessionDead ? (
                <Button
                  label={t.signInAgain}
                  size="md"
                  onPress={() => {
                    saveReturnTo(`/receipt/${id}`);
                    router.replace('/onboarding/sign-in');
                  }}
                />
              ) : null}
              {receipt.lines.some(
                (l) => l.match_status !== 'skipped' && !l.inventory_item_id,
              ) ? (
                <Text style={styles.err}>{t.rcptLinesUnmatched}</Text>
              ) : receipt.lines.some(lineNeedsConversion) ? (
                <Text style={styles.err}>{t.rcptConvNeeded}</Text>
              ) : null}
              <Button
                label={t.rcptCommit}
                loading={committing}
                disabled={
                  extracting ||
                  sessionDead ||
                  !affirmed ||
                  receipt.lines.some(
                    (l) => l.match_status !== 'skipped' && !l.inventory_item_id,
                  ) ||
                  receipt.lines.some(lineNeedsConversion)
                }
                onPress={() => void doCommit()}
              />
              <View style={styles.secondary}>
                <Pressable onPress={doRescan}>
                  <Text style={styles.secondaryText}>{t.rcptRescan}</Text>
                </Pressable>
                <Pressable onPress={() => setDismissReason(dismissReason === null ? '' : null)}>
                  <Text style={styles.secondaryText}>{t.rcptDismiss}</Text>
                </Pressable>
                <Pressable onPress={doCancel}>
                  <Text style={styles.secondaryText}>{t.rcptCancel}</Text>
                </Pressable>
              </View>
              {dismissReason !== null ? (
                <View style={styles.dismissBox}>
                  <TextInput
                    style={styles.dismissInput}
                    value={dismissReason}
                    onChangeText={setDismissReason}
                    placeholder={t.rcptDismissReason}
                    placeholderTextColor={T.ter}
                  />
                  <Button
                    label={t.rcptDismissConfirm}
                    size="md"
                    variant="secondary"
                    disabled={!dismissReason.trim()}
                    onPress={() => void doDismiss()}
                  />
                </View>
              ) : null}
            </View>
          ) : (
            <Text style={styles.terminal}>
              {committed ? t.rcptCommitted : t.rcptClosed}
            </Text>
          )}
        </ScrollView>
      )}

      <InventoryItemPicker
        visible={pickerForLine !== null}
        initialQuery={pickerForLine?.query ?? ''}
        onPick={onPick}
        onClose={() => setPickerForLine(null)}
      />
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: T.pad,
    paddingVertical: 10,
  },
  back: { width: 40 },
  h1: { ...TYPE.title3, color: T.text },
  body: { padding: T.pad, gap: 14, paddingBottom: 40 },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: T.pad, gap: 16 },
  err: { ...TYPE.subhead, color: T.red },
  meta: { gap: 2 },
  metaMain: { ...TYPE.title3, color: T.text },
  metaSub: { ...TYPE.subhead, color: T.sec },
  section: { ...TYPE.headline, color: T.text, marginTop: 4 },
  emptyLines: { ...TYPE.body, color: T.sec },
  lines: { gap: 10 },
  addLink: { ...TYPE.body, color: T.ac },
  addBox: { backgroundColor: T.elev1, borderRadius: 14, padding: 14, gap: 10 },
  units: { flexDirection: 'row', flexWrap: 'wrap', gap: 6 },
  unitChip: {
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 12,
    backgroundColor: T.elev2,
  },
  unitChipOn: { backgroundColor: T.acSoft },
  unitLabel: { ...TYPE.footnote, color: T.sec },
  unitLabelOn: { color: T.ac },
  commitBox: { gap: 12, marginTop: 8 },
  affirmRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 12,
  },
  affirmText: { ...TYPE.subhead, color: T.label, flex: 1 },
  secondary: { flexDirection: 'row', justifyContent: 'space-around' },
  secondaryText: { ...TYPE.subhead, color: T.sec },
  dismissBox: { gap: 8 },
  dismissInput: {
    ...TYPE.body,
    color: T.text,
    backgroundColor: T.elev1,
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 8,
  },
  terminal: { ...TYPE.body, color: T.sec, textAlign: 'center', marginTop: 12 },
});
