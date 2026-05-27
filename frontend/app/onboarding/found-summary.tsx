import React, { useEffect, useState } from 'react';
import { View, Text, StyleSheet, ActivityIndicator } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button, Pill } from '@/components/atoms';
import { OnboardingHeader } from '@/components/OnboardingHeader';
import { useAuth } from '@/auth/AuthContext';
import { API_BASE } from '@/auth/config';
import { T, TYPE } from '@/theme/tokens';

export default function FoundSummary() {
  const { connected } = useLocalSearchParams<{ connected?: string }>();
  const router = useRouter();
  const { token, me } = useAuth();
  const tenantId = me?.tenants?.[0]?.id;

  const [verified, setVerified] = useState<boolean | null>(null);
  const [merchantId, setMerchantId] = useState<string | null>(null);

  useEffect(() => {
    if (!token || !tenantId) {
      setVerified(connected === 'true');
      return;
    }
    fetch(`${API_BASE}/api/v1/pos/clover/status`, {
      headers: { Authorization: `Bearer ${token}`, 'X-Tenant-Id': tenantId },
    })
      .then(r => r.json())
      .then(d => {
        setVerified(d.connected === true);
        setMerchantId(d.merchant_id ?? null);
      })
      .catch(() => setVerified(connected === 'true'));
  }, [token, tenantId]);

  return (
    <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
      <OnboardingHeader step={6} totalSteps={14} onBack={() => router.back()} />
      <View style={styles.body}>
        {verified === null ? (
          <ActivityIndicator color={T.ac} />
        ) : verified ? (
          <Pill label="Clover connected" tone="green" iconLeft="shield" />
        ) : (
          <Pill label="Not connected" tone="amber" />
        )}
        <Text style={styles.h2}>Here's what we found</Text>
        <Text style={styles.sub}>
          {verified
            ? `Your Clover account is connected${merchantId ? ` (${merchantId})` : ''}. We'll pull your menu and recent orders to set up your inventory.`
            : 'Could not verify your Clover connection. Go back and try connecting again.'}
        </Text>
      </View>
      <View style={styles.cta}>
        {verified ? (
          <Button
            label="Continue"
            fullWidth
            iconRight="arrow-right"
            onPress={() => router.push('/onboarding/cleanup')}
          />
        ) : (
          <Button
            label="Go back"
            variant="secondary"
            fullWidth
            onPress={() => router.back()}
          />
        )}
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  body: { flex: 1, paddingHorizontal: T.pad, paddingTop: 24, gap: 16 },
  h2:   { ...TYPE.title1, color: T.text },
  sub:  { ...TYPE.body, color: T.sec },
  cta:  { paddingHorizontal: T.pad, paddingVertical: 8, borderTopWidth: 1, borderTopColor: T.hairline },
});
