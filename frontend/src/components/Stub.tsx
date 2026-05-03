// Stub screens for the remaining 10 onboarding steps. Each is a placeholder
// "TODO" page with a Continue button so the flow is navigable end-to-end.
// Replace each with a full port of the matching web step (see app/Onboarding.jsx).

import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { useRouter, useLocalSearchParams } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button, Pill } from '@/components/atoms';
import { OnboardingHeader } from '@/components/OnboardingHeader';
import { T, TYPE } from '@/theme/tokens';

type StubProps = { title: string; step: number; nextHref: string; nextLabel?: string; note?: string };

export function Stub({ title, step, nextHref, nextLabel = 'Continue', note }: StubProps) {
  const router = useRouter();
  return (
    <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
      <OnboardingHeader step={step} totalSteps={14} onBack={() => router.back()} />
      <View style={styles.body}>
        <Pill label="STUB" tone="amber" />
        <Text style={styles.h2}>{title}</Text>
        <Text style={styles.sub}>{note ?? 'Port this screen from the web prototype next. The flow is navigable end-to-end so you can demo on device.'}</Text>
      </View>
      <View style={styles.cta}>
        <Button label={nextLabel} fullWidth iconRight="arrow-right" onPress={() => router.push(nextHref as any)} />
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  body: { flex: 1, paddingHorizontal: T.pad, paddingTop: 24, gap: 16 },
  h2: { ...TYPE.title1, color: T.text },
  sub: { ...TYPE.body, color: T.sec },
  cta: { paddingHorizontal: T.pad, paddingVertical: 8, borderTopWidth: 1, borderTopColor: T.hairline },
});

// ── One file per route below ─────────────────────────────────────────────
// (Each route in /onboarding/ imports Stub and configures it.)
