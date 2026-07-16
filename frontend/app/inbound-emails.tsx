// Email invoices — inbound observability screen (Sprint 6 3b follow-up).
//
// A manager can verify an emailed invoice end-to-end from here without any DB
// access: the tenant forwarding address, every recent inbound email with a
// server-derived status badge, plain-English filter reasons, and a jump into
// the receipt review screen when a draft exists.

import React, { useCallback, useEffect, useState } from 'react';
import {
  View,
  Text,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Platform,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { useAuth } from '@/auth/AuthContext';
import { useLang } from '@/i18n/LangProvider';
import { T, TYPE } from '@/theme/tokens';
import {
  getInboundAddress,
  listInboundEmails,
  type InboundEmail,
  type InboundDisplayStatus,
} from '@/api/inboundEmails';
import type { StringKey } from '@/i18n/strings';

const BADGE: Record<InboundDisplayStatus, { key: StringKey; bg: string; fg: string }> = {
  received: { key: 'inbStReceived', bg: T.elev2, fg: T.sec },
  processing: { key: 'inbStProcessing', bg: T.blueSoft, fg: T.blue },
  filtered: { key: 'inbStFiltered', bg: T.amberSoft, fg: T.amber },
  error: { key: 'inbStError', bg: T.redSoft, fg: T.red },
  draft_created: { key: 'inbStDraft', bg: T.blueSoft, fg: T.blue },
  needs_review: { key: 'inbStNeedsReview', bg: T.amberSoft, fg: T.amber },
  committed: { key: 'inbStCommitted', bg: T.greenSoft, fg: T.green },
};

// filter-code → plain-language string key. Per-attachment codes arrive as
// "attachment_<n>:<CODE>" — match on the CODE part; unknown codes show raw.
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

function reasonCodes(email: InboundEmail): string[] {
  const raw = [email.skip_reason ?? '', ...email.filter_flags].filter(Boolean);
  const codes = raw.map((f) => (f.includes(':') ? f.split(':').pop()! : f));
  return [...new Set(codes)];
}

export default function InboundEmails() {
  const router = useRouter();
  const { t } = useLang();
  const { token } = useAuth();
  const [address, setAddress] = useState<string | null>(null);
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [emails, setEmails] = useState<InboundEmail[] | null>(null);
  const [error, setError] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    if (!token) return;
    setError(false);
    try {
      const [addr, list] = await Promise.all([
        getInboundAddress(token),
        listInboundEmails(token),
      ]);
      setConfigured(addr.configured);
      setAddress(addr.address);
      setEmails(list.inbound_emails);
    } catch {
      setError(true);
    }
  }, [token]);

  useEffect(() => {
    load();
  }, [load]);

  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  }, [load]);

  // Web-only copy (no expo-clipboard dep); native shows a selectable address.
  const canCopy = Platform.OS === 'web' && typeof navigator !== 'undefined';
  const copyAddress = useCallback(() => {
    if (!address || !canCopy) return;
    navigator.clipboard?.writeText(address);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [address, canCopy]);

  const renderEmail = ({ item }: { item: InboundEmail }) => {
    const badge = BADGE[item.display_status] ?? BADGE.received;
    const draft = item.receipts[0];
    const showReasons = item.display_status === 'filtered' || item.display_status === 'error';
    return (
      <View style={s.card}>
        <View style={s.rowBetween}>
          <Text style={s.sender} numberOfLines={1}>
            {item.from_email ?? '—'}
          </Text>
          <View style={[s.badge, { backgroundColor: badge.bg }]}>
            <Text style={[s.badgeText, { color: badge.fg }]}>{t[badge.key]}</Text>
          </View>
        </View>
        {item.subject ? (
          <Text style={s.subject} numberOfLines={1}>
            {item.subject}
          </Text>
        ) : null}
        <Text style={s.meta}>
          {new Date(item.received_at ?? item.created_at).toLocaleString()} ·{' '}
          {item.attachment_count} {t.inbAttachments} · {item.qualified_attachment_count}{' '}
          {t.inbQualified}
        </Text>
        {showReasons
          ? reasonCodes(item).map((code) => (
              <Text key={code} style={s.reason}>
                {REASON[code] ? t[REASON[code]] : code}
              </Text>
            ))
          : null}
        {draft ? (
          <Pressable
            style={s.actionBtn}
            onPress={() => router.push(`/receipt/${draft.receipt_id}`)}
          >
            <Text style={s.actionBtnText}>{t.inbOpenDraft}</Text>
          </Pressable>
        ) : null}
      </View>
    );
  };

  return (
    <SafeAreaView style={s.safe} edges={['top']}>
      <FlatList
        data={emails ?? []}
        keyExtractor={(e) => e.id}
        renderItem={renderEmail}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} />}
        contentContainerStyle={s.listContent}
        ListHeaderComponent={
          <View>
            <Text style={s.title}>{t.inbTitle}</Text>
            <Text style={s.sub}>{t.inbSub}</Text>
            <View style={s.addressCard}>
              <Text style={s.addressLabel}>{t.inbAddress}</Text>
              {configured === false ? (
                <Text style={s.meta}>{t.inbNotConfigured}</Text>
              ) : (
                <>
                  <Text style={s.address} selectable>
                    {address ?? '…'}
                  </Text>
                  <Text style={s.meta}>{t.inbAddressHint}</Text>
                  {address && canCopy ? (
                    <Pressable style={s.actionBtn} onPress={copyAddress}>
                      <Text style={s.actionBtnText}>{copied ? t.inbCopied : t.inbCopy}</Text>
                    </Pressable>
                  ) : null}
                </>
              )}
            </View>
            <Text style={s.section}>{t.inbRecent}</Text>
            {error ? (
              <View>
                <Text style={s.meta}>{t.inbError}</Text>
                <Pressable style={s.actionBtn} onPress={load}>
                  <Text style={s.actionBtnText}>{t.inbRetry}</Text>
                </Pressable>
              </View>
            ) : null}
          </View>
        }
        ListEmptyComponent={
          !error && emails !== null ? <Text style={s.meta}>{t.inbEmpty}</Text> : null
        }
      />
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  listContent: { padding: 16, paddingBottom: 48 },
  title: { ...TYPE.title1, color: T.text, marginBottom: 4 },
  sub: { ...TYPE.subhead, color: T.sec, marginBottom: 16 },
  section: { ...TYPE.title3, color: T.text, marginTop: 8, marginBottom: 8 },
  addressCard: {
    backgroundColor: T.elev1,
    borderRadius: 12,
    padding: 16,
    marginBottom: 16,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: T.sep,
  },
  addressLabel: { ...TYPE.headline, color: T.label, marginBottom: 6 },
  address: {
    ...TYPE.body,
    color: T.text,
    fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace',
    marginBottom: 6,
  },
  card: {
    backgroundColor: T.elev1,
    borderRadius: 12,
    padding: 14,
    marginBottom: 10,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: T.sep,
  },
  rowBetween: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  sender: { ...TYPE.headline, color: T.text, flexShrink: 1, marginRight: 8 },
  subject: { ...TYPE.subhead, color: T.label, marginTop: 2 },
  meta: { ...TYPE.footnote, color: T.sec, marginTop: 4 },
  reason: { ...TYPE.footnote, color: T.amber, marginTop: 4 },
  badge: { borderRadius: 999, paddingHorizontal: 10, paddingVertical: 3 },
  badgeText: { ...TYPE.caption1, fontWeight: '600' },
  actionBtn: {
    alignSelf: 'flex-start',
    marginTop: 10,
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 8,
    backgroundColor: T.acSoft,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: T.acBorder,
  },
  actionBtnText: { color: T.ac, fontWeight: '600' },
});
