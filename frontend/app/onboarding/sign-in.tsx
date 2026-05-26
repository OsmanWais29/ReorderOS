import React, { useState } from 'react';
import { View, Text, StyleSheet, Pressable, KeyboardAvoidingView, Platform } from 'react-native';
import { useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button, Field } from '@/components/atoms';
import { OnboardingHeader } from '@/components/OnboardingHeader';
import { T, TYPE } from '@/theme/tokens';
import { signInWithPassword } from '@/auth/api';
import { useAuth } from '@/auth/AuthContext';
import { WORKOS_CLIENT_ID, REDIRECT_URI } from '@/auth/config';

const GOOGLE_AUTH_URL =
  `https://api.workos.com/user_management/authorize?` +
  new URLSearchParams({
    client_id: WORKOS_CLIENT_ID,
    redirect_uri: REDIRECT_URI,
    response_type: 'code',
    provider: 'GoogleOAuth',
  }).toString();

export default function SignIn() {
  const router = useRouter();
  const { signIn } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSignIn = async () => {
    if (!email.trim() || !password) {
      setError('Please enter your email and password.');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const token = await signInWithPassword(email.trim(), password);
      await signIn(token);
      router.replace('/(app)/home');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Sign in failed');
      setLoading(false);
    }
  };

  const handleGoogle = () => {
    if (typeof window !== 'undefined') {
      window.location.href = GOOGLE_AUTH_URL;
    }
  };

  return (
    <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
      <OnboardingHeader step={1} totalSteps={14} onBack={() => router.back()} />

      <KeyboardAvoidingView
        style={styles.flex}
        behavior={Platform.OS === 'ios' ? 'padding' : 'height'}
      >
        <View style={styles.body}>
          <Text style={styles.h2}>Welcome back</Text>
          <Text style={styles.sub}>Sign in to your ReOrderOS account.</Text>

          <View style={styles.fields}>
            <Field
              label="Email"
              value={email}
              onChangeText={setEmail}
              keyboardType="email-address"
              autoCapitalize="none"
              autoComplete="email"
              autoCorrect={false}
              placeholder="you@restaurant.com"
              placeholderTextColor={T.ter}
              editable={!loading}
            />
            <Field
              label="Password"
              value={password}
              onChangeText={setPassword}
              secureTextEntry
              autoComplete="current-password"
              placeholder="••••••••"
              placeholderTextColor={T.ter}
              editable={!loading}
              onSubmitEditing={handleSignIn}
              returnKeyType="go"
            />
          </View>

          {error && <Text style={styles.error}>{error}</Text>}
        </View>

        <View style={styles.cta}>
          <Button
            label={loading ? 'Signing in…' : 'Sign in'}
            fullWidth
            iconRight="arrow-right"
            disabled={loading || !email || !password}
            onPress={handleSignIn}
          />

          <View style={styles.divider}>
            <View style={styles.dividerLine} />
            <Text style={styles.dividerLabel}>or</Text>
            <View style={styles.dividerLine} />
          </View>

          <Pressable style={styles.googleBtn} onPress={handleGoogle} disabled={loading}>
            <Text style={styles.googleLabel}>Continue with Google</Text>
          </Pressable>

          <Pressable onPress={() => router.back()} style={styles.backLink}>
            <Text style={styles.backLabel}>Back to welcome</Text>
          </Pressable>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe:   { flex: 1, backgroundColor: T.bg },
  flex:   { flex: 1 },
  body:   { flex: 1, paddingHorizontal: T.pad, paddingTop: 32, gap: 12 },
  h2:     { ...TYPE.title1, color: T.text },
  sub:    { ...TYPE.body, color: T.sec },
  fields: { gap: 16, marginTop: 8 },
  error:  { ...TYPE.subhead, color: T.red, marginTop: 4 },
  cta: {
    paddingHorizontal: T.pad,
    paddingVertical: 12,
    gap: 12,
    borderTopWidth: 1,
    borderTopColor: T.hairline,
  },
  divider:      { flexDirection: 'row', alignItems: 'center', gap: 12 },
  dividerLine:  { flex: 1, height: 1, backgroundColor: T.hairline },
  dividerLabel: { ...TYPE.subhead, color: T.ter },
  googleBtn: {
    borderWidth: 1,
    borderColor: T.sep,
    borderRadius: 12,
    paddingVertical: 14,
    alignItems: 'center',
    backgroundColor: T.elev1,
  },
  googleLabel: { ...TYPE.body, color: T.text },
  backLink:  { alignItems: 'center', paddingVertical: 8 },
  backLabel: { ...TYPE.subhead, color: T.label },
});
