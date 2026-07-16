// Supplier invoices inbox — the day-to-day work queue (Sprint 6 UX pass).
//
// Four tabs: Needs review / Processing / Issues / Received. Cards are enriched
// with detail fetches (invoice number/date, line counts) capped per tab — pilot
// scale keeps this cheap, and the list endpoint stays untouched. The Issues tab
// also folds in filtered/errored inbound EMAILS (from the observability surface)
// so "my email never showed up" is answerable here, not in ops tooling.

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  View,
  Text,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { Icon } from '@/components/Icon';
import { useAuth } from '@/auth/AuthContext';
import { useLang } from '@/i18n/LangProvider';
import { T, TYPE } from '@/theme/tokens';
import {
  listReceipts,
  getReceipt,
  type ReceiptDetail,
  type ReceiptListItem,
  type ReceiptSource,
} from '@/api/receipts';
import { listInboundEmails, type InboundEmail } from '@/api/inboundEmails';
import type { StringKey } from '@/i18n/strings';

type Tab = 'needs_review' | 'processing' | 'issues' | 'received';
const TABS: { key: Tab; label: StringKey }[] = [
  { key: 'needs_review', label: 'invTabNeedsReview' },
  { key: 'processing', label: 'invTabProcessing' },
  { key: 'issues', label: 'invTabIssues' },
  { key: 'received', label: 'invTabReceived' },
];

const DETAIL_CAP = 25;

// filter-code → plain-language string key (shared vocabulary with inbound-emails).
const REASON: Record<string, StringKey> = {
  unknown_token: 'inbRsnUnknownToken',
  spam_score_exceeded: 'inbRsnSpam',
  no_qualifying_attachment: 'inbRsnNoQualifying',
  no_attachment: 'inbRsnNoAttachment',
  INBOUND_ATTACHMENT_TOO_SMALL: 'inbRsnTooSmall',
  INBOUND_ATTACHMENT_TOO_LARGE: 'inbRsnTooLarge',
  INBOUND_ATTACHMENT_DECODE_ERROR: 'inbRsnDecode',
  RECEIPT_UNSUPPORTED_TYPE: 'inbRsnUnsupported',
  RECEIPT_TOO_MANY_PAGES: 'inbRsnTooManyPages',
  RECEIPT_PDF_UNREADABLE: 'inbRsnPdfUnreadable',
  RECEIPT_HEIC_UNSUPPORTED: 'inbRsnHeic',
  RECEIPT_POLYGLOT_REJECTED: 'inbRsnPolyglot',
  RECEIPT_CORRUPT_IMAGE: 'inbRsnCorrupt',
  html_body_deferred: 'inbRsnHtmlDeferred',
  html_body_ignored: 'inbRsnHtmlIgnored',
  unknown_sender: 'inbRsnUnknownSender',
};

function bucketOf(r: ReceiptListItem): Tab {
  if (r.commit_state === 'committed') return 'received';
  if (r.extraction_status === 'pending' || r.extraction_status === 'processing') {
    return 'processing';
  }
  if (r.manual_entry_required || r.quota_blocked || r.extraction_status === 'failed') {
    return 'issues';
  }
  return 'needs_review';
}

function sourceLabelKey(source: ReceiptSource, mime: string | null): StringKey {
  if (source === 'email' || source === 'gmail') return 'invSrcEmail';
  if (mime === 'application/pdf') return 'invSrcPdf';
  if (source === 'mobile_photo') return 'invSrcPhoto';
  return 'invSrcManual';
}

export default function SupplierInvoices() {
  const router = useRouter();
  const { t } = useLang();
  const { token } = useAuth();
  const [tab, setTab] = useState<Tab>('needs_review');
  const [receipts, setReceipts] = useState<ReceiptListItem[] | null>(null);
  const [details, setDetails] = useState<Record<string, ReceiptDetail>>({});
  const [filteredEmails, setFilteredEmails] = useState<InboundEmail[]>([]);
  const [error, setError] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    if (!token) return;
    setError(false);
    try {
      const [drafts, committed] = await Promise.all([
        listReceipts(token, { commit_state: 'draft' }),
        listReceipts(token, { commit_state: 'committed' }),
      ]);
      setReceipts([...drafts, ...committed]);
    } catch {
      setError(true);
      return;
    }
    try {
      const emails = await listInboundEmails(token);
      setFilteredEmails(
        emails.inbound_emails.filter(
          (e) => e.display_status === 'filtered' || e.display_status === 'error',
        ),
      );
    } catch {
      // staff / unconfigured — receipts-only Issues tab still works
      setFilteredEmails([]);
    }
  }, [token]);

  useEffect(() => {
    void load();
  }, [load]);

  const buckets = useMemo(() => {
    const b: Record<Tab, ReceiptListItem[]> = {
      needs_review: [],
      processing: [],
      issues: [],
      received: [],
    };
    for (const r of receipts ?? []) b[bucketOf(r)].push(r);
    b.received.sort((x, y) => (y.created_at < x.created_at ? -1 : 1));
    return b;
  }, [receipts]);

  // Enrich the ACTIVE tab's cards with detail (invoice number/date, line count).
  const current = buckets[tab];
  useEffect(() => {
    if (!token) return;
    const missing = current.filter((r) => !details[r.id]).slice(0, DETAIL_CAP);
    if (missing.length === 0) return;
    let cancelled = false;
    void Promise.all(
      missing.map((r) => getReceipt(token, r.id).catch(() => null)),
    ).then((fetched) => {
      if (cancelled) return;
      setDetails((prev) => {
        const next = { ...prev };
        for (const d of fetched) if (d) next[d.id] = d;
        return next;
      });
    });
    return () => {
      cancelled = true;
    };
  }, [token, current, details]);

  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    setDetails({});
    await load();
    setRefreshing(false);
  }, [load]);

  const statusLabel = (r: ReceiptListItem): string => {
    const b = bucketOf(r);
    if (b === 'received') return t.invStReceived;
    if (b === 'processing') return t.invStReading;
    if (b === 'issues') return t.invStNeedsAttention;
    return t.invStReadyToReview;
  };
  const statusTone = (r: ReceiptListItem): { bg: string; fg: string } => {
    const b = bucketOf(r);
    if (b === 'received') return { bg: T.greenSoft, fg: T.green };
    if (b === 'processing') return { bg: T.blueSoft, fg: T.blue };
    if (b === 'issues') return { bg: T.redSoft, fg: T.red };
    return { bg: T.amberSoft, fg: T.amber };
  };

  const renderReceipt = ({ item }: { item: ReceiptListItem }) => {
    const d = details[item.id];
    const tone = statusTone(item);
    const receivable = d ? d.lines.filter((l) => l.match_status !== 'skipped').length : null;
    return (
      <Pressable
        style={({ pressed }) => [styles.card, pressed && styles.cardPressed]}
        onPress={() => router.push(`/receipt/${item.id}`)}
      >
        <View style={styles.cardTop}>
          <Text style={styles.supplier} numberOfLines={1}>
            {item.supplier_name ?? t.invNoSupplier}
          </Text>
          <View style={[styles.badge, { backgroundColor: tone.bg }]}>
            <Text style={[styles.badgeText, { color: tone.fg }]}>{statusLabel(item)}</Text>
          </View>
        </View>
        <Text style={styles.meta}>
          {[
            d?.invoice_number ? `#${d.invoice_number}` : null,
            d?.invoice_date ?? null,
            item.total_cents != null ? `$${(item.total_cents / 100).toFixed(2)}` : null,
          ]
            .filter(Boolean)
            .join('  ·  ') || '—'}
        </Text>
        <Text style={styles.metaSub}>
          {t[sourceLabelKey(item.source, d?.mime_type ?? null)]}
          {receivable != null
            ? `  ·  ${receivable} ${t.invStockItems}`
            : ''}
          {'  ·  '}
          {new Date(item.created_at).toLocaleString()}
        </Text>
      </Pressable>
    );
  };

  const isIssuesTab = tab === 'issues';

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <View style={styles.header}>
        <Pressable onPress={() => router.back()} hitSlop={12} style={styles.back}>
          <Icon name="chevron-left" size={22} color={T.text} />
        </Pressable>
        <Text style={styles.h1}>{t.invInboxTitle}</Text>
        <View style={styles.back} />
      </View>

      <View style={styles.tabs}>
        {TABS.map(({ key, label }) => {
          const count =
            key === 'issues' ? buckets.issues.length + filteredEmails.length : buckets[key].length;
          return (
            <Pressable
              key={key}
              onPress={() => setTab(key)}
              style={[styles.tab, tab === key && styles.tabOn]}
            >
              <Text style={[styles.tabLabel, tab === key && styles.tabLabelOn]}>
                {t[label]}
                {count > 0 ? ` (${count})` : ''}
              </Text>
            </Pressable>
          );
        })}
      </View>

      {error ? (
        <View style={styles.center}>
          <Text style={styles.err}>{t.invLoadError}</Text>
          <Pressable onPress={() => void load()}>
            <Text style={styles.retry}>{t.stockRetry}</Text>
          </Pressable>
        </View>
      ) : (
        <FlatList
          data={current}
          keyExtractor={(r) => r.id}
          renderItem={renderReceipt}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} />}
          contentContainerStyle={styles.listContent}
          ListHeaderComponent={
            isIssuesTab && filteredEmails.length > 0 ? (
              <View style={styles.emailIssues}>
                <Text style={styles.section}>{t.invEmailIssues}</Text>
                {filteredEmails.map((e) => {
                  const codes = [e.skip_reason ?? '', ...e.filter_flags]
                    .filter(Boolean)
                    .map((f) => (f.includes(':') ? f.split(':').pop()! : f));
                  const code = [...new Set(codes)][0];
                  return (
                    <View key={e.id} style={styles.card}>
                      <View style={styles.cardTop}>
                        <Text style={styles.supplier} numberOfLines={1}>
                          {e.from_email ?? '—'}
                        </Text>
                        <View style={[styles.badge, { backgroundColor: T.redSoft }]}>
                          <Text style={[styles.badgeText, { color: T.red }]}>
                            {t.invStNotProcessed}
                          </Text>
                        </View>
                      </View>
                      <Text style={styles.metaSub}>
                        {code && REASON[code] ? t[REASON[code]] : (code ?? '')}
                      </Text>
                      <Text style={styles.metaSub}>
                        {new Date(e.received_at ?? e.created_at).toLocaleString()}
                      </Text>
                    </View>
                  );
                })}
                {current.length > 0 ? (
                  <Text style={styles.section}>{t.invInvoiceIssues}</Text>
                ) : null}
              </View>
            ) : null
          }
          ListEmptyComponent={
            !isIssuesTab || filteredEmails.length === 0 ? (
              <Text style={styles.empty}>{t.invTabEmpty}</Text>
            ) : null
          }
        />
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 16,
    paddingVertical: 10,
  },
  back: { width: 40 },
  h1: { ...TYPE.title3, color: T.text },
  tabs: { flexDirection: 'row', gap: 6, paddingHorizontal: 16, paddingBottom: 10 },
  tab: {
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 12,
    backgroundColor: T.elev1,
  },
  tabOn: { backgroundColor: T.acSoft },
  tabLabel: { ...TYPE.footnote, color: T.sec },
  tabLabelOn: { color: T.ac, fontWeight: '600' },
  listContent: { padding: 16, paddingTop: 4, paddingBottom: 48, gap: 10 },
  card: {
    backgroundColor: T.elev1,
    borderRadius: 12,
    padding: 14,
    gap: 4,
    marginBottom: 10,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: T.sep,
  },
  cardPressed: { backgroundColor: T.elev2 },
  cardTop: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: 8 },
  supplier: { ...TYPE.headline, color: T.text, flexShrink: 1 },
  badge: { borderRadius: 999, paddingHorizontal: 10, paddingVertical: 3 },
  badgeText: { ...TYPE.caption1, fontWeight: '600' },
  meta: { ...TYPE.subhead, color: T.label },
  metaSub: { ...TYPE.footnote, color: T.sec },
  section: { ...TYPE.headline, color: T.text, marginBottom: 8, marginTop: 4 },
  emailIssues: { marginBottom: 4 },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: 12 },
  err: { ...TYPE.subhead, color: T.red },
  retry: { ...TYPE.subhead, color: T.ac },
  empty: { ...TYPE.body, color: T.sec, textAlign: 'center', marginTop: 24 },
});
