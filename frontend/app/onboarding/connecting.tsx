import React, { useState } from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import * as WebBrowser from 'expo-web-browser';
import { Button } from '@/components/atoms';
import { OnboardingHeader } from '@/components/OnboardingHeader';
import { useAuth } from '@/auth/AuthContext';
import { API_BASE } from '@/auth/config';
import { T, TYPE } from '@/theme/tokens';

// Required so iOS dismisses the auth session cleanly on return.
WebBrowser.maybeCompleteAuthSession();

const REDIRECT_URL = 'https://reorderos.com/onboarding/found-summary';

export default function Connecting() {
  const { provider } = useLocalSearchParams<{ provider?: string }>();
  const { token, me } = useAuth();
  const router = useRouter();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

      // Opens Clover OAuth in an in-app browser on native, or a popup on web.
      // Monitors for navigation to REDIRECT_URL and closes the browser when it lands there.
      const result = await WebBrowser.openAuthSessionAsync(url, REDIRECT_URL);

      if (result.type === 'success') {
        const params = new URL(result.url).searchParams;
        router.replace(`/onboarding/found-summary?connected=${params.get('connected') ?? 'true'}`);
      } else if (result.type === 'cancel') {
        setError('Connection cancelled. Tap below to try again.');
      } else {
        setError('Could not complete the Clover connection. Try again.');
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Something went wrong');
    } finally {
      setLoading(false);
    }
  };

  if (provider !== 'clover') {
    return (
      <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
        <OnboardingHeader step={5} totalSteps={14} onBack={() => router.back()} />
        <View style={styles.body}>
          <Text style={styles.h2}>
            {provider ? provider.charAt(0).toUpperCase() + provider.slice(1) : 'This POS'} coming soon
          </Text>
          <Text style={styles.sub}>
            We're adding more integrations. Clover is available now — go back and
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
          orders and menu — we never write to your POS.
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
