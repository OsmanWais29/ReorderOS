import React, { useEffect, useState } from 'react';
import { View, Text, StyleSheet, ActivityIndicator } from 'react-native';
import { useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button, Card } from '@/components/atoms';
import { OnboardingHeader } from '@/components/OnboardingHeader';
import { useAuth } from '@/auth/AuthContext';
import { API_BASE } from '@/auth/config';
import { T, TYPE } from '@/theme/tokens';

type TopItem = { name: string; quantity: number };

type Summary = {
  connected: boolean;
  merchant_id?: string;
  merchant_name?: string;
  merchant_location?: string;
  menu_item_count: number;
  net_sales_30d_cents: number;
  top_items: TopItem[];
};

function formatSales(cents: number): string {
  if (cents >= 100_000_00) return `$${(cents / 100_000).toFixed(1)}k`;
  if (cents >= 10_000_00) return `$${(cents / 100_000).toFixed(1)}k`;
  if (cents >= 1_000_00) return `$${(cents / 100).toLocaleString('en-US', { maximumFractionDigits: 0 })}`;
  return `$${(cents / 100).toFixed(0)}`;
}

export default function FoundSummary() {
  const router = useRouter();
  const { token, me } = useAuth();
  const tenantId = me?.tenants?.[0]?.id;
  const tenantName = me?.tenants?.[0]?.name;

  const [loading, setLoading] = useState(true);
  const [summary, setSummary] = useState<Summary | null>(null);

  useEffect(() => {
    if (!token || !tenantId) {
      setLoading(false);
      return;
    }
    fetch(`${API_BASE}/api/v1/pos/clover/sync-summary`, {
      headers: { Authorization: `Bearer ${token}`, 'X-Tenant-Id': tenantId },
    })
      .then(r => r.json())
      .then(d => setSummary(d))
      .catch(() => setSummary(null))
      .finally(() => setLoading(false));
  }, [token, tenantId]);

  if (loading) {
    return (
      <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
        <OnboardingHeader step={6} totalSteps={14} onBack={() => router.back()} />
        <View style={styles.center}>
          <ActivityIndicator color={T.ac} size="large" />
        </View>
      </SafeAreaView>
    );
  }

  if (!summary?.connected) {
    return (
      <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
        <OnboardingHeader step={6} totalSteps={14} onBack={() => router.back()} />
        <View style={styles.body}>
          <Text style={styles.h2}>Could not verify connection</Text>
          <Text style={styles.sub}>
            Go back and try connecting again.
          </Text>
        </View>
        <View style={styles.cta}>
          <Button label="Go back" variant="secondary" fullWidth onPress={() => router.back()} />
        </View>
      </SafeAreaView>
    );
  }

  const displayName = summary.merchant_name || tenantName || 'Your restaurant';
  const displayLocation = summary.merchant_location || '';

  return (
    <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
      <OnboardingHeader step={6} totalSteps={14} onBack={() => router.back()} />
      <View style={styles.body}>
        <Text style={styles.h2}>Here's what we found</Text>

        <Card style={styles.importCard}>
          <Text style={styles.importLabel}>IMPORTED FROM</Text>
          <Text style={styles.importValue}>
            {displayName}{displayLocation ? ` · ${displayLocation}` : ''}
          </Text>
        </Card>

        <View style={styles.statRow}>
          <Card style={styles.statCard}>
            <Text style={styles.statNum}>{summary.menu_item_count}</Text>
            <Text style={styles.statLabel}>Menu items</Text>
          </Card>
          <Card style={styles.statCard}>
            <Text style={styles.statNum}>{formatSales(summary.net_sales_30d_cents)}</Text>
            <Text style={styles.statLabel}>Net sales · 30d</Text>
          </Card>
        </View>

        {summary.top_items.length > 0 && (
          <Card style={styles.topCard}>
            <Text style={styles.topHeader}>TOP {summary.top_items.length} — LAST 30 DAYS</Text>
            {summary.top_items.map((item, i) => (
              <View key={item.name} style={styles.topRow}>
                <Text style={styles.topRank}>{i + 1}</Text>
                <Text style={styles.topName}>{item.name}</Text>
                <Text style={styles.topQty}>
                  {item.quantity.toLocaleString()}{' '}
                  <Text style={styles.topQtyUnit}>sold</Text>
                </Text>
              </View>
            ))}
          </Card>
        )}
      </View>

      <View style={styles.cta}>
        <Button
          label="Let's clean this up — about 5–10 minutes"
          fullWidth
          iconRight="arrow-right"
          onPress={() => router.push('/onboarding/cleanup')}
        />
        <Button
          label="This isn't my restaurant"
          variant="ghost"
          fullWidth
          onPress={() => router.back()}
        />
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  body: { flex: 1, paddingHorizontal: T.pad, paddingTop: 16, gap: 14 },
  h2: { ...TYPE.title1, color: T.text, marginBottom: 4 },
  sub: { ...TYPE.body, color: T.sec },
  cta: {
    paddingHorizontal: T.pad,
    paddingVertical: 12,
    gap: 12,
    borderTopWidth: 1,
    borderTopColor: T.hairline,
  },
  importCard: { paddingVertical: 14 },
  importLabel: {
    fontSize: 11,
    fontWeight: '600',
    color: T.sec,
    letterSpacing: 0.8,
    textTransform: 'uppercase',
    marginBottom: 4,
  },
  importValue: { ...TYPE.body, color: T.text },
  statRow: { flexDirection: 'row', gap: 12 },
  statCard: { flex: 1 },
  statNum: { fontSize: 28, fontWeight: '700', color: T.ac, marginBottom: 2 },
  statLabel: { ...TYPE.caption1, color: T.sec },
  topCard: { gap: 0 },
  topHeader: {
    fontSize: 11,
    fontWeight: '600',
    color: T.sec,
    letterSpacing: 0.8,
    textTransform: 'uppercase',
    marginBottom: 14,
  },
  topRow: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingVertical: 8,
    gap: 12,
  },
  topRank: { ...TYPE.headline, color: T.ac, width: 20, textAlign: 'center' },
  topName: { ...TYPE.body, color: T.text, flex: 1 },
  topQty: { ...TYPE.body, color: T.ac, fontVariant: ['tabular-nums'] },
  topQtyUnit: { color: T.sec, fontSize: 13 },
});
