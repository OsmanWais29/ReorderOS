// Sign-in screen — opens WorkOS hosted login via PKCE redirect.

import React, { useState } from 'react';
import { View, Text, StyleSheet, Pressable } from 'react-native';
import { useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button } from '@/components/atoms';
import { OnboardingHeader } from '@/components/OnboardingHeader';
import { T, TYPE } from '@/theme/tokens';
import { generatePKCE } from '@/auth/pkce';
import { saveVerifier } from '@/auth/storage';
import { WORKOS_CLIENT_ID, WORKOS_AUTH_URL, REDIRECT_URI } from '@/auth/config';

export default function SignIn() {
  const router = useRouter();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSignIn = async () => {
    setLoading(true);
    setError(null);
    try {
      const { verifier, challenge } = await generatePKCE();
      saveVerifier(verifier);

      const params = new URLSearchParams({
        client_id: WORKOS_CLIENT_ID,
        redirect_uri: REDIRECT_URI,
        response_type: 'code',
        code_challenge: challenge,
        code_challenge_method: 'S256',
        provider: 'authkit',
      });

      window.location.href = `${WORKOS_AUTH_URL}?${params.toString()}`;
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Something went wrong');
      setLoading(false);
    }
  };

  return (
    <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
      <OnboardingHeader step={1} totalSteps={14} onBack={() => router.back()} />

      <View style={styles.body}>
        <Text style={styles.h2}>Welcome back</Text>
        <Text style={styles.sub}>Sign in to your ReOrderOS account.</Text>
        {error && <Text style={styles.error}>{error}</Text>}
      </View>

      <View style={styles.cta}>
        <Button
          label={loading ? 'Opening login…' : 'Sign in'}
          fullWidth
          iconRight="arrow-right"
          disabled={loading}
          onPress={handleSignIn}
        />
        <Pressable onPress={() => router.back()} style={styles.backLink}>
          <Text style={styles.backLabel}>Back to welcome</Text>
        </Pressable>
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  body: { flex: 1, paddingHorizontal: T.pad, paddingTop: 32, gap: 12 },
  h2: { ...TYPE.title1, color: T.text },
  sub: { ...TYPE.body, color: T.sec },
  error: { ...TYPE.subhead, color: T.red, marginTop: 8 },
  cta: {
    paddingHorizontal: T.pad,
    paddingVertical: 12,
    gap: 12,
    borderTopWidth: 1,
    borderTopColor: T.hairline,
  },
  backLink: { alignItems: 'center', paddingVertical: 8 },
  backLabel: { ...TYPE.subhead, color: T.label },
});
