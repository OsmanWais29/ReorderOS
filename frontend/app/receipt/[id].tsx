// Shared receipt review screen (spec §7 FE Phase B) — ONE UI for every source
// (photo, manual, and later email/Gmail/webhook drafts land here unchanged).
//
// Flow: poll while extraction runs (2s, max 30s) → review/match lines (edits
// blur-save through PUT — every mutation clears the affirmation server-side,
// D-606-25) → affirm → commit (manager; server-authoritative gate, D-606-22).
// Re-scan (reset-extraction) is explicitly confirmed — it discards all edits.

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  View,
  Text,
  ScrollView,
  Pressable,
  Switch,
  TextInput,
  StyleSheet,
} from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button, Field } from '@/components/atoms';
import { Icon } from '@/components/Icon';
import { ReceiptSourceBadge, ExtractionStatusBanner, ReceiptPhotoPreview } from '@/components/ReceiptBits';
import { ReceiptLineRow } from '@/components/ReceiptLineRow';
import { InventoryItemPicker, type ItemChoice } from '@/components/InventoryItemPicker';
import { confirmDestructive } from '@/ui/dialogs';
import { useAuth } from '@/auth/AuthContext';
import { saveReturnTo } from '@/auth/returnTo';
import { useLang } from '@/i18n/LangProvider';
import { T, TYPE } from '@/theme/tokens';
import { CANONICAL_UNITS, dimensionOf, hintDimension } from '@/api/units';
import { updateItemStorageUnit, ItemsApiError } from '@/api/items';
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
  type ConversionBlocker,
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
  const [addCost, setAddCost] = useState(''); // unit cost in dollars; '' = not entered
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

  // Blocker → line scrolling: onLayout records each card's y; declared BEFORE
  // doCommit so the callbacks can list it as a dependency.
  const scrollRef = useRef<ScrollView | null>(null);
  const lineYs = useRef<Record<string, number>>({});
  const jumpToLine = useCallback((lineId: string | undefined) => {
    if (!lineId) return;
    const y = lineYs.current[lineId];
    if (y != null) scrollRef.current?.scrollTo({ y: Math.max(0, y - 90), animated: true });
  }, []);
  // Server-confirmed blockers from a rejected receive (defense-in-depth: the
  // local gates should prevent the submit; if the server still refuses, we mark
  // the EXACT lines, summarize above the button, and jump to the first).
  const [serverBlockers, setServerBlockers] = useState<Record<string, ConversionBlocker>>({});

  const patchLine = useCallback(
    async (lineId: string, patch: LineUpdatePayload) => {
      if (!token || !id) return;
      setBusyLineId(lineId);
      setCommitError(null);
      try {
        await updateLine(token, id, lineId, patch);
        setAffirmed(false); // the server cleared reviewed_affirmation — re-affirm after edits
        setServerBlockers({}); // the line set changed — stale blockers re-derive on retry
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

  const fixItemUnit = useCallback(
    async (itemId: string, unit: string) => {
      if (!token) return;
      setCommitError(null);
      try {
        await updateItemStorageUnit(token, itemId, unit);
        await refresh(); // suggestions + mismatch flags recompute server-side
      } catch (e: unknown) {
        if (e instanceof ItemsApiError && e.status === 401) setSessionDead(true);
        setCommitError(e instanceof Error ? e.message : t.rcptSaveError);
      }
    },
    [token, refresh, t],
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
    // Unit cost is optional at add-time (an operator may not know it yet), but when
    // present it must be a valid non-negative amount. Dollars → integer cents.
    const costTrimmed = addCost.trim();
    let costCents: number | null = null;
    if (costTrimmed !== '') {
      const c = Number(costTrimmed.replace(',', '.'));
      if (!Number.isFinite(c) || c < 0) return;
      costCents = Math.round(c * 100);
    }
    setCommitError(null);
    try {
      await addLine(token, id, {
        extracted_name: addName.trim(),
        received_quantity: n,
        extracted_unit: addUnit,
        unit_cost_cents: costCents,
      });
      setAddName('');
      setAddQty('');
      setAddUnit(null);
      setAddCost('');
      setAddOpen(false);
      setAffirmed(false);
      await refresh();
    } catch (e: unknown) {
      if (e instanceof ReceiptApiError && e.status === 401) setSessionDead(true);
      setCommitError(e instanceof ReceiptApiError ? (e.status === 401 ? t.sessionExpired : e.detail) : t.rcptSaveError);
    }
  }, [token, id, addName, addQty, addUnit, addCost, refresh, t]);

  // Live line total for the add form (quantity x unit cost), shown when both are valid.
  const addLineTotal = useMemo(() => {
    const q = Number(addQty.replace(',', '.'));
    const c = Number(addCost.replace(',', '.'));
    if (!Number.isFinite(q) || q <= 0 || addCost.trim() === '' || !Number.isFinite(c) || c < 0) {
      return null;
    }
    return (q * c).toFixed(2);
  }, [addQty, addCost]);

  const doCommit = useCallback(async () => {
    if (!token || !id) return;
    setCommitting(true);
    setCommitError(null);
    try {
      await commitReceipt(token, id, affirmed);
      // In-screen success summary (not a dialog): the operator sees exactly what
      // moved into stock, then chooses where to go.
      await refresh();
      setJustReceived(true);
    } catch (e: unknown) {
      if (e instanceof ReceiptApiError) {
        if (e.status === 401) {
          setSessionDead(true);
          setCommitError(t.sessionExpired);
        }
        else if (e.code === 'RECEIPT_REVIEW_REQUIRED') setCommitError(t.rcptNeedReview);
        else if (e.code === 'RECEIPT_LINES_UNMATCHED') setCommitError(t.rcptLinesUnmatched);
        else if (
          e.code === 'RECEIPT_UNIT_CONVERSION_REQUIRED' ||
          e.code === 'RECEIPT_CONVERSION_REQUIRED'
        ) {
          // Structured blockers: mark each offending line, jump to the first.
          const byLine: Record<string, ConversionBlocker> = {};
          for (const b of e.errors) byLine[b.receipt_line_id] = b;
          setServerBlockers(byLine);
          setCommitError(t.rcptConvNeeded);
          const first = e.errors[0]?.receipt_line_id;
          if (first) setTimeout(() => jumpToLine(first), 50);
        }
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
  }, [token, id, affirmed, refresh, jumpToLine, t]);

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

  // Receiving lines (not skipped) that will move stock but carry no unit cost — these
  // leave food cost incomplete for that item, so we warn before commit (D-606-09).
  const missingCostCount = useMemo(
    () =>
      (receipt?.lines ?? []).filter(
        (ln) => ln.match_status !== 'skipped' && ln.unit_cost_cents == null,
      ).length,
    [receipt?.lines],
  );

  // ── Guided-workflow derivations (presentation only — the server gates stay
  //    authoritative; these mirror them so the operator sees WHY, not just "no") ──
  const lines = receipt?.lines ?? [];
  const receivable = useMemo(() => lines.filter((l) => l.match_status !== 'skipped'), [lines]);
  // Machine-classified non-stock rows (discount/credit/backorder/fee) — visible
  // but inert; operator-skipped ITEM lines keep their editable card (restorable).
  const nonStockLines = useMemo(
    () => lines.filter((l) => l.match_status === 'skipped' && l.line_type !== 'item'),
    [lines],
  );
  const skippedLines = useMemo(
    () => lines.filter((l) => l.match_status === 'skipped' && l.line_type === 'item'),
    [lines],
  );
  const unmatchedLines = useMemo(
    () => receivable.filter((l) => !l.inventory_item_id),
    [receivable],
  );
  const convPending = useMemo(() => receivable.filter(lineNeedsConversion), [receivable]);
  const convConfirmed = useMemo(
    () => receivable.filter((l) => l.conversion_confirmed_at !== null).length,
    [receivable],
  );
  const warningLines = useMemo(
    () => convPending.filter((l) => l.unit_mismatch_warning),
    [convPending],
  );
  // Signed adjustment cents per ITEM line (operator-linked discount/credit rows).
  const adjustmentsByLine = useMemo(() => {
    const m: Record<string, number> = {};
    for (const l of lines) {
      if (
        (l.line_type === 'discount' || l.line_type === 'credit') &&
        l.match_status === 'skipped' &&
        l.adjusts_line_id &&
        l.line_total_cents != null
      ) {
        m[l.adjusts_line_id] = (m[l.adjusts_line_id] ?? 0) + l.line_total_cents;
      }
    }
    return m;
  }, [lines]);

  // "Safe" = linked item + backend suggestion + no mismatch warning + unambiguous.
  // AUDITED RULE for cross-dimension lines (ea → L): bulk-accept ONLY on explicit,
  // line-specific invoice evidence whose unit lives in the STORAGE dimension
  // (explicit package evidence, e.g. a printed pack size, for a volume-tracked
  // item). Remembered factors and weak inference are
  // NOT evidence — those lines stay in per-line review. Dimension-mismatch
  // overrides never bulk (already excluded via unit_mismatch_warning).
  const safeLines = useMemo(
    () =>
      convPending.filter((l) => {
        if (
          l.inventory_item_id === null ||
          l.unit_mismatch_warning ||
          l.suggested_quantity == null ||
          l.suggested_quantity <= 0 ||
          l.suggested_factor == null ||
          l.suggested_factor <= 0 ||
          l.item_storage_unit === null ||
          l.extracted_unit === null
        ) {
          return false;
        }
        const from = dimensionOf(l.extracted_unit);
        const to = dimensionOf(l.item_storage_unit);
        const crossDimension = from !== null && to !== null && from !== to;
        if (!crossDimension) return true;
        if (l.suggestion_source === 'remembered') return false;
        const evidenceUnit = l.actual_weight_qty
          ? l.actual_weight_unit
          : l.pack_size_qty
            ? l.pack_size_unit
            : null;
        return evidenceUnit !== null && hintDimension(evidenceUnit) === to;
      }),
    [convPending],
  );

  const step = committed
    ? 4
    : extracting || receipt === null
      ? 1
      : unmatchedLines.length > 0 || receivable.length === 0
        ? 2
        : convPending.length > 0
          ? 3
          : 4;

  const [bulkBusy, setBulkBusy] = useState(false);
  const acceptAllSafe = useCallback(async () => {
    if (!token || !id || bulkBusy) return;
    setBulkBusy(true);
    setCommitError(null);
    // Sequential on purpose: each PUT re-locks the receipt server-side, and a
    // failure stops the run instead of half-applying in parallel.
    for (const l of safeLines) {
      const pq = l.received_quantity;
      const gross =
        l.line_total_cents ?? (l.unit_cost_cents != null && pq ? l.unit_cost_cents * pq : null);
      const totalCents = gross != null ? gross + (adjustmentsByLine[l.id] ?? 0) : null;
      const q = l.suggested_quantity as number;
      try {
        await updateLine(token, id, l.id, {
          received_quantity: q,
          received_unit: l.item_storage_unit as string,
          conversion_factor: l.suggested_factor as number,
          ...(totalCents != null ? { unit_cost_cents: Math.round(totalCents / q) } : {}),
          remember_conversion: true,
        });
      } catch (e: unknown) {
        if (e instanceof ReceiptApiError && e.status === 401) setSessionDead(true);
        setCommitError(e instanceof ReceiptApiError ? e.detail : t.rcptSaveError);
        break;
      }
    }
    setAffirmed(false);
    await refresh();
    setBulkBusy(false);
  }, [token, id, bulkBusy, safeLines, adjustmentsByLine, refresh, t]);

  const [showSkipped, setShowSkipped] = useState(false);
  const [justReceived, setJustReceived] = useState(false);
  // Which discount/credit row is currently picking its target item line.
  const [linkingRowId, setLinkingRowId] = useState<string | null>(null);

  // Ready = would actually receive (mirrors the backend gate line-for-line).
  const readyCount = useMemo(
    () =>
      receivable.filter((l) => l.inventory_item_id !== null && !lineNeedsConversion(l)).length,
    [receivable],
  );

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
      ) : receipt === null ? null : justReceived ? (
        /* ── success summary: exactly what moved into stock ── */
        <ScrollView contentContainerStyle={styles.body}>
          <View style={styles.successHead}>
            <Icon name="check" size={28} color={T.green} />
            <Text style={styles.successTitle}>{t.rcptDoneTitle}</Text>
          </View>
          <View style={styles.lines}>
            {receivable.map((l) => (
              <View key={l.id} style={styles.successRow}>
                <Text style={styles.successQty}>
                  +{l.received_quantity} {l.received_unit ?? l.extracted_unit ?? ''}
                </Text>
                <View style={styles.successRowText}>
                  <Text style={styles.successItem} numberOfLines={1}>
                    {l.item_name ?? l.extracted_name ?? ''}
                  </Text>
                  {l.line_total_cents != null &&
                  l.received_quantity != null &&
                  l.received_quantity > 0 ? (
                    <Text style={styles.successCost}>
                      $
                      {(
                        (l.line_total_cents + (adjustmentsByLine[l.id] ?? 0)) /
                        100 /
                        l.received_quantity
                      ).toFixed(2)}
                      /{l.received_unit ?? l.extracted_unit ?? ''}
                    </Text>
                  ) : null}
                </View>
              </View>
            ))}
          </View>
          <Button label={t.rcptDoneViewStock} onPress={() => router.replace('/(app)/stock')} />
          <Button
            label={t.rcptDoneReceiveAnother}
            variant="secondary"
            onPress={() => router.replace('/(app)/stock')}
          />
        </ScrollView>
      ) : (
        <ScrollView
          ref={scrollRef}
          contentContainerStyle={styles.body}
          keyboardShouldPersistTaps="handled"
        >
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

          {/* step progress — where am I, what's next */}
          {editable ? (
            <View style={styles.steps}>
              {[t.rcptStep1, t.rcptStep2, t.rcptStep3, t.rcptStep4].map((label, i) => {
                const n = i + 1;
                const done = step > n;
                const active = step === n;
                return (
                  <View key={label} style={styles.step}>
                    <View
                      style={[
                        styles.stepDot,
                        done && styles.stepDotDone,
                        active && styles.stepDotActive,
                      ]}
                    >
                      <Text style={[styles.stepNum, (done || active) && styles.stepNumOn]}>
                        {done ? '✓' : n}
                      </Text>
                    </View>
                    <Text style={[styles.stepLabel, active && styles.stepLabelActive]}>
                      {label}
                    </Text>
                  </View>
                );
              })}
            </View>
          ) : null}

          {/* progress counts */}
          {editable && receivable.length > 0 ? (
            <View style={styles.counts}>
              <Text style={styles.countItem}>
                {t.rcptCountMatched
                  .replace('{x}', String(receivable.length - unmatchedLines.length))
                  .replace('{y}', String(receivable.length))}
              </Text>
              {convConfirmed + convPending.length > 0 ? (
                <Text style={styles.countItem}>
                  {t.rcptCountConfirmed
                    .replace('{x}', String(convConfirmed))
                    .replace('{y}', String(convConfirmed + convPending.length))}
                </Text>
              ) : null}
              {warningLines.length > 0 ? (
                <Text style={[styles.countItem, { color: T.amber }]}>
                  {t.rcptCountWarnings.replace('{x}', String(warningLines.length))}
                </Text>
              ) : null}
              {skippedLines.length > 0 ? (
                <Text style={styles.countItem}>
                  {t.rcptCountSkipped.replace('{x}', String(skippedLines.length))}
                </Text>
              ) : null}
            </View>
          ) : null}

          {/* bulk-accept: only provably-safe suggestions; warnings never bulk */}
          {editable && convPending.length > 0 ? (
            <View style={styles.bulkBox}>
              <Text style={styles.bulkText}>
                {t.rcptBulkSafe.replace('{x}', String(safeLines.length))}
                {convPending.length - safeLines.length > 0
                  ? `   ·   ${t.rcptBulkUnsafe.replace(
                      '{y}',
                      String(convPending.length - safeLines.length),
                    )}`
                  : ''}
              </Text>
              {safeLines.length > 0 ? (
                <Button
                  label={t.rcptBulkAccept.replace('{x}', String(safeLines.length))}
                  size="md"
                  loading={bulkBusy}
                  onPress={() => void acceptAllSafe()}
                />
              ) : null}
            </View>
          ) : null}

          {/* receivable lines */}
          <Text style={styles.section}>{t.rcptLines}</Text>
          {receipt.lines.length === 0 && !extracting ? (
            <Text style={styles.emptyLines}>{t.rcptNoLines}</Text>
          ) : null}
          <View style={styles.lines}>
            {receivable.map((line) => (
              <View
                key={line.id}
                onLayout={(e) => {
                  lineYs.current[line.id] = e.nativeEvent.layout.y;
                }}
              >
                <ReceiptLineRow
                  line={line}
                  busy={busyLineId === line.id || !editable || bulkBusy}
                  attention={!!serverBlockers[line.id] || lineNeedsConversion(line)}
                  adjustmentCents={adjustmentsByLine[line.id] ?? 0}
                  onFixItemUnit={(itemId, unit) => void fixItemUnit(itemId, unit)}
                  onPatch={(patch) => void patchLine(line.id, patch)}
                  onOpenPicker={() =>
                    setPickerForLine({ lineId: line.id, query: line.extracted_name ?? '' })
                  }
                />
              </View>
            ))}
          </View>

          {/* non-stock rows: machine-classified rows + skipped lines + invoice tax —
              never mixed with receivable items */}
          {nonStockLines.length > 0 || skippedLines.length > 0 || receipt.tax_cents != null ? (
            <View>
              <Pressable onPress={() => setShowSkipped((s) => !s)} style={styles.skippedHead}>
                <Text style={styles.skippedTitle}>
                  {t.rcptNotAdded.replace(
                    '{x}',
                    String(
                      nonStockLines.length +
                        skippedLines.length +
                        (receipt.tax_cents != null ? 1 : 0),
                    ),
                  )}{' '}
                  {showSkipped ? '▾' : '▸'}
                </Text>
              </Pressable>
              {showSkipped ? (
                <View style={styles.lines}>
                  {nonStockLines.map((l) => {
                    const linkable =
                      editable && (l.line_type === 'discount' || l.line_type === 'credit');
                    const target = l.adjusts_line_id
                      ? receivable.find((r) => r.id === l.adjusts_line_id)
                      : undefined;
                    return (
                      <View key={l.id} style={styles.skippedCard}>
                        <View style={styles.skippedRowInner}>
                          <View style={styles.skippedRowText}>
                            <Text style={styles.skippedType}>
                              {l.line_type === 'discount'
                                ? t.rcptTypeDiscount
                                : l.line_type === 'credit'
                                  ? t.rcptTypeCredit
                                  : l.line_type === 'backorder'
                                    ? t.rcptTypeBackorder
                                    : t.rcptTypeFee}
                            </Text>
                            <Text style={styles.skippedName} numberOfLines={1}>
                              {l.extracted_name ?? '—'}
                            </Text>
                          </View>
                          {l.line_total_cents != null ? (
                            <Text style={styles.skippedAmt}>
                              {l.line_total_cents < 0 ? '−' : ''}$
                              {Math.abs(l.line_total_cents / 100).toFixed(2)}
                            </Text>
                          ) : null}
                        </View>
                        {/* Explicit cost-adjustment link — never allocated silently */}
                        {linkable ? (
                          target ? (
                            <View style={styles.adjLinkRow}>
                              <Text style={styles.adjLinkedText} numberOfLines={1}>
                                {t.rcptAdjAppliedTo}{' '}
                                {target.item_name ?? target.extracted_name ?? '?'}
                              </Text>
                              <Pressable
                                onPress={() => void patchLine(l.id, { adjusts_line_id: null })}
                              >
                                <Text style={styles.adjAction}>{t.rcptAdjRemove}</Text>
                              </Pressable>
                            </View>
                          ) : linkingRowId === l.id ? (
                            <View style={styles.adjPickWrap}>
                              {receivable.map((r) => (
                                <Pressable
                                  key={r.id}
                                  style={styles.adjPickChip}
                                  onPress={() => {
                                    setLinkingRowId(null);
                                    void patchLine(l.id, { adjusts_line_id: r.id });
                                  }}
                                >
                                  <Text style={styles.adjPickLabel} numberOfLines={1}>
                                    {r.item_name ?? r.extracted_name ?? '?'}
                                  </Text>
                                </Pressable>
                              ))}
                            </View>
                          ) : (
                            <Pressable onPress={() => setLinkingRowId(l.id)}>
                              <Text style={styles.adjAction}>{t.rcptApplyToItem}</Text>
                            </Pressable>
                          )
                        ) : null}
                      </View>
                    );
                  })}
                  {receipt.tax_cents != null ? (
                    <View style={styles.skippedRow}>
                      <Text style={styles.skippedName}>{t.rcptTaxRow}</Text>
                      <Text style={styles.skippedAmt}>
                        ${(receipt.tax_cents / 100).toFixed(2)}
                      </Text>
                    </View>
                  ) : null}
                  {skippedLines.map((line) => (
                    <ReceiptLineRow
                      key={line.id}
                      line={line}
                      busy={busyLineId === line.id || !editable || bulkBusy}
                      onFixItemUnit={(itemId, unit) => void fixItemUnit(itemId, unit)}
                      onPatch={(patch) => void patchLine(line.id, patch)}
                      onOpenPicker={() =>
                        setPickerForLine({ lineId: line.id, query: line.extracted_name ?? '' })
                      }
                    />
                  ))}
                </View>
              ) : null}
            </View>
          ) : null}

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
                <Field
                  label={t.rcptAddCost}
                  hint={addCost.trim() === '' ? t.rcptCostMissing : undefined}
                  value={addCost}
                  onChangeText={setAddCost}
                  keyboardType="decimal-pad"
                  placeholder="0.00"
                />
                {addLineTotal !== null ? (
                  <Text style={styles.addTotal}>
                    {t.rcptLineTotal}: ${addLineTotal}
                  </Text>
                ) : null}
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
              {/* Food-cost honesty (D-606-09 warn-not-block): if any receiving line has
                  no unit cost, say so plainly rather than let the operator assume the
                  cost is captured. A warning, not a hard gate — a legit receipt without
                  a known cost must still be committable. */}
              {missingCostCount > 0 ? (
                <Text style={styles.costWarn}>
                  {t.rcptCostWarn.replace('{n}', String(missingCostCount))}
                </Text>
              ) : null}
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
              {/* server-confirmed blockers (structured 422): item names, plain
                  explanation, each row jumps to its line */}
              {Object.keys(serverBlockers).length > 0 ? (
                <View style={styles.blockerBox}>
                  <Text style={styles.blockerTitle}>{t.rcptConvBlockTitle}</Text>
                  {Object.values(serverBlockers).map((b) => (
                    <Pressable key={b.receipt_line_id} onPress={() => jumpToLine(b.receipt_line_id)}>
                      <Text style={styles.blocker}>
                        ▸{' '}
                        {t.rcptConvBlockLine
                          .replace('{unit}', b.purchase_unit ?? '?')
                          .replace('{item}', b.inventory_item_name ?? b.invoice_name ?? '?')
                          .replace('{su}', b.storage_unit ?? '?')}
                      </Text>
                    </Pressable>
                  ))}
                </View>
              ) : null}
              {/* exact blockers, each a jump to the first offending line */}
              {unmatchedLines.length > 0 ? (
                <Pressable onPress={() => jumpToLine(unmatchedLines[0]?.id)}>
                  <Text style={styles.blocker}>
                    ▸ {t.rcptBlockUnmatched.replace('{x}', String(unmatchedLines.length))}
                  </Text>
                </Pressable>
              ) : null}
              {convPending.length > 0 ? (
                <Pressable onPress={() => jumpToLine(convPending[0]?.id)}>
                  <Text style={styles.blocker}>
                    ▸ {t.rcptBlockConfirm.replace('{x}', String(convPending.length))}
                  </Text>
                </Pressable>
              ) : null}
              {warningLines.length > 0 ? (
                <Pressable onPress={() => jumpToLine(warningLines[0]?.id)}>
                  <Text style={styles.blocker}>
                    ▸ {t.rcptBlockWarning.replace('{x}', String(warningLines.length))}
                  </Text>
                </Pressable>
              ) : null}
              {/* Commit is ATOMIC — the CTA never advertises a partial receive.
                  Blocked: "Resolve N issues to receive Y items" (disabled).
                  Ready:   "Receive Y items into stock". */}
              {receivable.length > 0 ? (
                <Text style={styles.readyLine}>
                  {t.rcptReadyCount
                    .replace('{x}', String(readyCount))
                    .replace('{y}', String(receivable.length))}
                </Text>
              ) : null}
              <Button
                label={
                  readyCount < receivable.length
                    ? t.rcptResolveCta
                        .replace('{n}', String(receivable.length - readyCount))
                        .replace('{x}', String(receivable.length))
                    : t.rcptReceiveCta.replace('{x}', String(receivable.length))
                }
                loading={committing}
                disabled={
                  extracting ||
                  sessionDead ||
                  !affirmed ||
                  receivable.length === 0 ||
                  readyCount < receivable.length
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
  steps: { flexDirection: 'row', justifyContent: 'space-between', gap: 4 },
  step: { alignItems: 'center', flex: 1, gap: 4 },
  stepDot: {
    width: 26,
    height: 26,
    borderRadius: 13,
    backgroundColor: T.elev2,
    alignItems: 'center',
    justifyContent: 'center',
  },
  stepDotDone: { backgroundColor: T.greenSoft },
  stepDotActive: { backgroundColor: T.acSoft, borderWidth: 1, borderColor: T.acBorder },
  stepNum: { ...TYPE.caption1, color: T.sec, fontWeight: '600' },
  stepNumOn: { color: T.ac },
  stepLabel: { ...TYPE.caption2, color: T.sec, textAlign: 'center' },
  stepLabelActive: { color: T.text, fontWeight: '600' },
  counts: { flexDirection: 'row', flexWrap: 'wrap', gap: 12 },
  countItem: { ...TYPE.footnote, color: T.sec },
  bulkBox: { backgroundColor: T.elev1, borderRadius: 12, padding: 12, gap: 8 },
  bulkText: { ...TYPE.footnote, color: T.label },
  blocker: { ...TYPE.subhead, color: T.amber },
  blockerBox: {
    backgroundColor: T.redSoft,
    borderRadius: 12,
    padding: 12,
    gap: 6,
    borderWidth: 1,
    borderColor: T.red,
  },
  blockerTitle: { ...TYPE.headline, color: T.red },
  readyLine: { ...TYPE.subhead, color: T.sec, textAlign: 'center' },
  skippedHead: { paddingVertical: 6 },
  skippedTitle: { ...TYPE.headline, color: T.sec },
  skippedRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    backgroundColor: T.elev1,
    borderRadius: 12,
    padding: 12,
  },
  skippedName: { ...TYPE.subhead, color: T.sec },
  skippedAmt: { ...TYPE.subhead, color: T.sec },
  skippedRowText: { flex: 1, gap: 2 },
  skippedCard: {
    backgroundColor: T.elev1,
    borderRadius: 12,
    padding: 12,
    gap: 8,
  },
  skippedRowInner: { flexDirection: 'row', justifyContent: 'space-between', gap: 8 },
  adjLinkRow: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: 8 },
  adjLinkedText: { ...TYPE.footnote, color: T.ac, flexShrink: 1 },
  adjAction: { ...TYPE.subhead, color: T.ac },
  adjPickWrap: { flexDirection: 'row', flexWrap: 'wrap', gap: 6 },
  adjPickChip: {
    backgroundColor: T.elev2,
    borderRadius: 12,
    paddingHorizontal: 10,
    paddingVertical: 6,
    maxWidth: 220,
  },
  adjPickLabel: { ...TYPE.footnote, color: T.label },
  skippedType: { ...TYPE.caption1, color: T.ter, textTransform: 'uppercase' },
  successHead: { alignItems: 'center', gap: 8, marginVertical: 12 },
  successTitle: { ...TYPE.title2, color: T.text },
  successRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 12,
    backgroundColor: T.elev1,
    borderRadius: 12,
    padding: 12,
  },
  successQty: { ...TYPE.headline, color: T.green, minWidth: 82 },
  successRowText: { flex: 1 },
  successItem: { ...TYPE.body, color: T.text },
  successCost: { ...TYPE.footnote, color: T.sec },
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
  addTotal: { ...TYPE.subhead, color: T.sec },
  costWarn: { ...TYPE.subhead, color: T.amber },
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
