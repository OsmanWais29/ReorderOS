import React, { useState } from 'react';
import { View, Text, StyleSheet, Pressable, KeyboardAvoidingView, Platform } from 'react-native';
import { useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import Svg, { Path } from 'react-native-svg';
import { Button, Field } from '@/components/atoms';
import { OnboardingHeader } from '@/components/OnboardingHeader';
import { T, TYPE } from '@/theme/tokens';
import { signInWithPassword } from '@/auth/api';
import { useAuth } from '@/auth/AuthContext';
import { WORKOS_CLIENT_ID, REDIRECT_URI } from '@/auth/config';

function GoogleLogo({ size = 18 }: { size?: number }) {
  return (
    <Svg width={size} height={size} viewBox="0 0 24 24">
      <Path
        d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"
        fill="#4285F4"
      />
      <Path
        d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
        fill="#34A853"
      />
      <Path
        d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l3.66-2.84z"
        fill="#FBBC05"
      />
      <Path
        d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"
        fill="#EA4335"
      />
    </Svg>
  );
}

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
      const tokens = await signInWithPassword(email.trim(), password);
      await signIn(tokens);
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
            <GoogleLogo />
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
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 10,
    borderWidth: 1,
    borderColor: T.sep,
    borderRadius: 12,
    paddingVertical: 14,
    backgroundColor: T.elev1,
  },
  googleLabel: { ...TYPE.body, color: T.text },
  backLink:  { alignItems: 'center', paddingVertical: 8 },
  backLabel: { ...TYPE.subhead, color: T.label },
});
