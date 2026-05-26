import React, { useState } from 'react';
import { View, Text, StyleSheet, Pressable, KeyboardAvoidingView, Platform } from 'react-native';
import { useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button, Field } from '@/components/atoms';
import { OnboardingHeader } from '@/components/OnboardingHeader';
import { T, TYPE } from '@/theme/tokens';
import { signInWithPassword } from '@/auth/api';
import { useAuth } from '@/auth/AuthContext';

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
  backLink:  { alignItems: 'center', paddingVertical: 8 },
  backLabel: { ...TYPE.subhead, color: T.label },
});
