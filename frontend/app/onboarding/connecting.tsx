import React, { useState } from 'react';
import { View, Text, StyleSheet, Platform } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import * as WebBrowser from 'expo-web-browser';
import * as Linking from 'expo-linking';
import { Button } from '@/components/atoms';
import { OnboardingHeader } from '@/components/OnboardingHeader';
import { useAuth } from '@/auth/AuthContext';
import { API_BASE } from '@/auth/config';
import { T, TYPE } from '@/theme/tokens';

const PROVIDER_LABELS: Record<string, string> = {
  clover: 'Clover',
  square: 'Square',
  lightspeed: 'Lightspeed',
  touchbistro: 'TouchBistro',
  maitred: "Maitre'D",
  toast: 'Toast',
  veloce: 'Veloce',
};

export default function Connecting() {
  const { provider } = useLocalSearchParams<{ provider?: string }>();
  const { token, me } = useAuth();
  const router = useRouter();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const providerLabel = PROVIDER_LABELS[provider ?? ''] ?? provider ?? 'This POS';

  const handleConnect = async () => {
    if (!token || !me) {
      setError('Not signed in — please go back and sign in first.');
      return;
    }
    const tenantId = me.tenants?.[0]?.id;
    if (!tenantId) {
      setError('No tenant found — please complete account setup first.');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/v1/pos/clover/connect-url`, {
        headers: {
          Authorization: `Bearer ${token}`,
          'X-Tenant-Id': tenantId,
        },
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? `Server error ${res.status}`);
      }
      const { url } = await res.json();

      if (Platform.OS === 'web') {
        window.location.href = url;
      } else {
        const redirectUrl = Linking.createURL('/onboarding/found-summary');
        const result = await WebBrowser.openAuthSessionAsync(url, redirectUrl);
        if (result.type === 'success') {
          router.replace({
            pathname: '/onboarding/found-summary',
            params: { connected: 'true' },
          });
        } else {
          setLoading(false);
        }
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Something went wrong');
      setLoading(false);
    }
  };

  if (provider !== 'clover') {
    return (
      <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
        <OnboardingHeader step={5} totalSteps={14} onBack={() => router.back()} />
        <View style={styles.body}>
          <Text style={styles.h2}>{providerLabel} coming soon</Text>
          <Text style={styles.sub}>
            We're adding more integrations. Clover is available now {'—'} go back and
            select Clover to connect your POS.
          </Text>
        </View>
        <View style={styles.cta}>
          <Button label="Go back" variant="secondary" fullWidth onPress={() => router.back()} />
        </View>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
      <OnboardingHeader step={5} totalSteps={14} onBack={() => router.back()} />
      <View style={styles.body}>
        <Text style={styles.h2}>Connect Clover</Text>
        <Text style={styles.sub}>
          You'll be redirected to Clover to authorise ReorderOS. We read your
          orders and menu {'—'} we never write to your POS.
        </Text>
        {error && <Text style={styles.error}>{error}</Text>}
      </View>
      <View style={styles.cta}>
        <Button
          label={loading ? 'Opening Clover…' : 'Connect Clover'}
          fullWidth
          iconRight={loading ? undefined : 'arrow-right'}
          loading={loading}
          onPress={handleConnect}
        />
        <Button
          label="Go back"
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
  body: { flex: 1, paddingHorizontal: T.pad, paddingTop: 32, gap: 12 },
  h2:   { ...TYPE.title1, color: T.text },
  sub:  { ...TYPE.body, color: T.sec },
  error: { ...TYPE.subhead, color: T.red, marginTop: 8 },
  cta: {
    paddingHorizontal: T.pad,
    paddingVertical: 12,
    gap: 12,
    borderTopWidth: 1,
    borderTopColor: T.hairline,
  },
});
