// Small receipt-review building blocks (spec §7 FE Phase B):
// ReceiptSourceBadge — where the draft came from (one review UI for all sources);
// ExtractionStatusBanner — pending/processing/failed/manual/quota states;
// ReceiptPhotoPreview — the signed-GET photo, expandable.

import React, { useState } from 'react';
import { View, Text, Image, Pressable, ActivityIndicator, StyleSheet, Linking } from 'react-native';
import { Pill } from '@/components/atoms';
import { useLang } from '@/i18n/LangProvider';
import { T, TYPE } from '@/theme/tokens';
import type { ExtractionStatus, ReceiptSource } from '@/api/receipts';

// ── source badge ──────────────────────────────────────────────────────────────

const SOURCE_TONE: Record<ReceiptSource, 'accent' | 'blue' | 'neutral'> = {
  mobile_photo: 'accent',
  manual: 'neutral',
  gmail: 'blue',
  email: 'blue',
  webhook: 'blue',
  pos: 'neutral',
};

export function ReceiptSourceBadge({ source }: { source: ReceiptSource }) {
  const { t } = useLang();
  const label: Record<ReceiptSource, string> = {
    mobile_photo: t.rcptSourcePhoto,
    manual: t.rcptSourceManual,
    gmail: 'Gmail',
    email: t.rcptSourceEmail,
    webhook: 'Webhook',
    pos: 'POS',
  };
  return <Pill label={label[source]} tone={SOURCE_TONE[source]} />;
}

// ── extraction status banner ──────────────────────────────────────────────────

export function ExtractionStatusBanner({
  status,
  manualEntryRequired,
  quotaBlocked,
  polling,
}: {
  status: ExtractionStatus;
  manualEntryRequired: boolean;
  quotaBlocked: boolean;
  polling: boolean;
}) {
  const { t } = useLang();

  if (quotaBlocked) {
    return <Banner tone="amber" text={t.rcptQuotaBlocked} />;
  }
  if (status === 'pending' || status === 'processing') {
    return (
      <Banner
        tone="neutral"
        text={polling ? t.rcptExtracting : t.rcptExtractingSlow}
        spinner={polling}
      />
    );
  }
  if (status === 'failed') {
    return <Banner tone="red" text={t.rcptExtractFailed} />;
  }
  if (status === 'manual_required' || manualEntryRequired) {
    return <Banner tone="amber" text={t.rcptManualNeeded} />;
  }
  return null; // complete / none / superseded need no banner
}

function Banner({
  tone,
  text,
  spinner,
}: {
  tone: 'neutral' | 'amber' | 'red';
  text: string;
  spinner?: boolean;
}) {
  const bg = tone === 'amber' ? T.amberSoft : tone === 'red' ? T.redSoft : T.elev1;
  const fg = tone === 'amber' ? T.amber : tone === 'red' ? T.red : T.sec;
  return (
    <View style={[styles.banner, { backgroundColor: bg }]}>
      {spinner ? <ActivityIndicator size="small" color={fg} /> : null}
      <Text style={[styles.bannerText, { color: fg }]}>{text}</Text>
    </View>
  );
}

// ── photo preview ─────────────────────────────────────────────────────────────

function PdfCard({ url }: { url: string }) {
  const { t } = useLang();
  return (
    <Pressable onPress={() => void Linking.openURL(url)}>
      <View style={styles.pdfCard}>
        <Text style={styles.pdfLabel}>{t.rcptPdfDoc}</Text>
      </View>
    </Pressable>
  );
}

export function ReceiptPhotoPreview({
  url,
  mimeType,
}: {
  url: string | null;
  mimeType?: string | null;
}) {
  const [expanded, setExpanded] = useState(false);
  if (!url) return null;
  if (mimeType === 'application/pdf') {
    // A PDF can't render in <Image>; open the signed URL in the platform viewer.
    return <PdfCard url={url} />;
  }
  return (
    <Pressable onPress={() => setExpanded((e) => !e)}>
      <Image
        source={{ uri: url }}
        style={[styles.photo, expanded && styles.photoExpanded]}
        resizeMode={expanded ? 'contain' : 'cover'}
      />
    </Pressable>
  );
}

const styles = StyleSheet.create({
  pdfCard: {
    backgroundColor: T.elev1,
    borderRadius: 12,
    paddingVertical: 18,
    alignItems: 'center',
  },
  pdfLabel: { ...TYPE.headline, color: T.ac },
  banner: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 10,
    borderRadius: 12,
    padding: 12,
  },
  bannerText: { ...TYPE.subhead, flex: 1 },
  photo: { width: '100%', height: 140, borderRadius: 12, backgroundColor: T.elev1 },
  photoExpanded: { height: 420 },
});
