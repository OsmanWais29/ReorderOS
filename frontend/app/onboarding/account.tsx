// Account creation step — name, restaurant, email, password.

import React, { useState } from 'react';
import { View, Text, ScrollView, StyleSheet, KeyboardAvoidingView, Platform } from 'react-native';
import { useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button, Field } from '@/components/atoms';
import { OnboardingHeader } from '@/components/OnboardingHeader';
import { useLang } from '@/i18n/LangProvider';
import { T, TYPE } from '@/theme/tokens';

export default function Account() {
  const router = useRouter();
  const { t } = useLang();

  const [firstName, setFirstName] = useState('');
  const [bizName, setBizName] = useState('');
  const [email, setEmail] = useState('');
  const [pw, setPw] = useState('');

  const valid =
    firstName.trim().length > 0 &&
    bizName.trim().length > 0 &&
    /^\S+@\S+\.\S+$/.test(email) &&
    pw.length >= 8;

  return (
    <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
      <OnboardingHeader step={2} totalSteps={14} onBack={() => router.back()} />
      <KeyboardAvoidingView
        style={{ flex: 1 }}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        keyboardVerticalOffset={20}
      >
        <ScrollView
          contentContainerStyle={styles.scroll}
          keyboardShouldPersistTaps="handled"
          showsVerticalScrollIndicator={false}
        >
          <Text style={styles.h2}>{t.create}</Text>
          <Text style={styles.sub}>{t.createSub}</Text>

          <View style={styles.fields}>
            <Field
              label={t.firstName}
              value={firstName}
              onChangeText={setFirstName}
              autoCapitalize="words"
              autoComplete="name-given"
              iconLeft="user"
            />
            <Field
              label={t.bizName}
              value={bizName}
              onChangeText={setBizName}
              autoCapitalize="words"
              iconLeft="building"
            />
            <Field
              label={t.email}
              value={email}
              onChangeText={setEmail}
              autoCapitalize="none"
              keyboardType="email-address"
              autoComplete="email"
              iconLeft="mail"
            />
            <Field
              label={t.pw}
              value={pw}
              onChangeText={setPw}
              secureTextEntry
              autoComplete="password-new"
              iconLeft="lock"
              hint={t.pwHint}
            />
          </View>
        </ScrollView>

        <View style={styles.cta}>
          <Button
            label={t.cont}
            fullWidth
            iconRight="arrow-right"
            disabled={!valid}
            onPress={() => router.push('/onboarding/push')}
          />
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe:   { flex: 1, backgroundColor: T.bg },
  scroll: { paddingHorizontal: T.pad, paddingTop: 16, paddingBottom: 24 },
  h2:     { ...TYPE.title1, color: T.text, marginBottom: 6 },
  sub:    { ...TYPE.body, color: T.sec, marginBottom: 24 },
  fields: { gap: 16 },
  cta: {
    paddingHorizontal: T.pad,
    paddingVertical: 8,
    borderTopWidth: 1,
    borderTopColor: T.hairline,
  },
});
