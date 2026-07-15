// WorkOS redirects here after login: /auth/callback?code=xxx
// Exchanges the code for a JWT via the backend, then navigates to the app.

import React, { useEffect, useState } from 'react';
import { View, Text, ActivityIndicator, StyleSheet } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { exchangeCode } from '@/auth/api';
import { loadVerifier } from '@/auth/storage';
import { useAuth } from '@/auth/AuthContext';
import { T, TYPE } from '@/theme/tokens';

export default function Callback() {
  const { code, error, error_description } = useLocalSearchParams<{
    code?: string;
    error?: string;
    error_description?: string;
  }>();
  const router = useRouter();
  const { signIn } = useAuth();
  const [status, setStatus] = useState('Completing sign-in…');

  useEffect(() => {
    if (error) {
      setStatus(`Sign-in failed: ${error_description ?? error}`);
      setTimeout(() => router.replace('/onboarding/sign-in'), 3000);
      return;
    }
    if (!code) return;

    (async () => {
      try {
        const verifier = loadVerifier();
        if (!verifier) throw new Error('Session expired — please try again');

        setStatus('Verifying with WorkOS…');
        const tokens = await exchangeCode(code, verifier);

        setStatus('Loading your account…');
        await signIn(tokens);

        router.replace('/(app)/home');
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : 'Unknown error';
        setStatus(`Error: ${msg}`);
        setTimeout(() => router.replace('/onboarding/sign-in'), 3000);
      }
    })();
  }, [code, error]);

  return (
    <View style={styles.container}>
      <ActivityIndicator size="large" color={T.ac} />
      <Text style={styles.label}>{status}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: T.bg, alignItems: 'center', justifyContent: 'center', gap: 16 },
  label: { ...TYPE.body, color: T.sec, textAlign: 'center', paddingHorizontal: 32 },
});
